#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
record_usage.py — Stop 钩子入口：增量聚合 db.sqlite 的已完成模型调用，
将本轮统计追加为一行 JSONL 记录，并推进每会话游标。

设计要点：
  - 只读访问 db.sqlite（`mode=ro` + `PRAGMA query_only=ON`），绝不写库。
  - 幂等（v2，rowid 游标）：每个 session_id 独立游标 = 已聚合的最大 rowid；
    聚合窗口为 `rowid > 游标 AND rowid <= 本次边界`。行一落库即终态
    （实测 error/cancelled 也带 completed_at/duration_ms，无原地状态转化、
    无回填），故 rowid 窗口天然不漏不重：纯失败轮的 error 行计入后游标即
    越过它，下轮不会重复计入；迟到行（started_at 更小但 rowid 更大）仍会
    被捞进来。
  - 兼容旧版 state.json（时间戳游标 v1）：读取时一次性迁移为 rowid 游标
    （按 started_at <= 旧值 映射最大 rowid；查不到时保守落到全局 MAX(rowid)），
    迁移与读写全程持锁。
  - 并发安全：跨进程 `<state>.lock` 锁文件 + msvcrt.locking 排他锁包裹
    "读游标→聚合→append→写游标"全程；临时文件名掺 pid+纳秒，杜绝多窗口
    同时 Stop 时互相截断 tmp / 后写覆盖先写。
  - 失败容错：任何异常吞掉、写入 stats.log，stdout 恒输出 `{}`，exit 0，
    绝不使 Stop 钩子失败。

调用：python record_usage.py [session_id]
  - session_id 可为空字符串；空时 fallback 查最新 session。
"""
import sqlite3
import json
import os
import sys
import time

try:
    import msvcrt  # Windows 跨进程文件锁；缺失时退化为无锁（当前部署环境恒有）
except ImportError:
    msvcrt = None

# 从本文件位置推导插件根目录（scripts/ 的上一级），插件升级换版本目录不断链
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(os.path.expanduser(r"~/.zcode/cli/plugins/data"), "local", "zcode-token-stats")
DB_PATH = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
STATE_LOCK_FILE = STATE_FILE + ".lock"
LOG_FILE = os.path.join(DATA_DIR, "token-stats.jsonl")
STATS_LOG = os.path.join(DATA_DIR, "stats.log")

STATE_VERSION = 2  # v1=时间戳游标(旧)，v2=rowid 游标(当前)

sys.path.insert(0, os.path.join(PLUGIN_ROOT, "scripts"))
# 延迟容错导入：升级瞬间 scripts 目录可能被替换/移动，绝不因 import 失败
# 让 Stop 钩子挂掉（record() 内会检查 ds 为 None 时记日志并跳过）
try:
    import db_stats as ds
except Exception:
    ds = None

# 与 db_stats.SOURCE_COND 同口径：行级一律排除子代理调用
SOURCE_COND = "COALESCE(query_source,'') <> 'subagent'"


def _is_subagent_sid(sid):
    """session id 是否属于 subagent 会话（sess_subagent_* 前缀或含 subagent）。

    子代理的 Stop 也会触发本钩子；子代理会话一律不写 jsonl、不推游标。
    """
    if not sid or not isinstance(sid, str):
        return False
    s = sid.strip().lower()
    return s.startswith("sess_subagent_") or "subagent" in s


def _log(msg):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATS_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


def _acquire_state_lock():
    """跨进程排他锁：对 <state>.lock 文件 offset 0 加锁（LK_LOCK 最长阻塞约10秒）。

    非 Windows（msvcrt 缺失）返回 None，退化为无锁——正确性依赖单进程场景。
    """
    if msvcrt is None:
        return None
    os.makedirs(DATA_DIR, exist_ok=True)
    f = open(STATE_LOCK_FILE, "a+b")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        return f
    except Exception:
        f.close()
        raise


def _release_state_lock(f):
    """解锁并清理锁文件；全部 try/except 包住，容忍残留（他人持句柄时删除会失败）。"""
    if f is None:
        return
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        f.close()
    except Exception:
        pass
    try:
        os.remove(STATE_LOCK_FILE)
    except Exception:
        pass


def _read_state():
    """读 state.json，返回 (is_v2, cursors)。损坏/缺文件按 (False, {}) 处理。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cursors = data.get("cursors")
            if isinstance(cursors, dict):
                return data.get("version") == STATE_VERSION, dict(cursors)
    except Exception:
        pass
    return False, {}


def _migrate_legacy_cursor(conn, session_id, legacy_ts):
    """旧时间戳游标 → rowid 游标：取该会话 started_at <= 旧值 的最大 rowid。

    查不到匹配行（会话数据已不在/全部晚于旧值）时保守落到全局 MAX(rowid)，
    避免把历史从头重算造成重复记账。
    """
    try:
        row = conn.execute(
            "SELECT MAX(rowid) FROM model_usage WHERE session_id = ? AND started_at <= ?",
            (session_id, int(legacy_ts)),
        ).fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    try:
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM model_usage").fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    return 0


def _migrate_cursors(cursors):
    """v1 时间戳游标批量迁移为 v2 rowid 游标。db 打不开时原样返回上层再试。"""
    conn = ds._connect(DB_PATH)
    try:
        out = {}
        for sid, ts in cursors.items():
            try:
                ts_val = int(ts)
            except Exception:
                continue
            out[sid] = _migrate_legacy_cursor(conn, sid, ts_val)
        return out
    finally:
        conn.close()


def _atomic_replace(src, dst):
    """os.replace 带界重试：Windows 上目标被他人瞬时读句柄占用时报 WinError 5，
    写者之间已由 <state>.lock 串行化，冲突窗口为毫秒级，短退避必成。"""
    for _ in range(60):
        try:
            os.replace(src, dst)
            return
        except OSError:
            time.sleep(0.02)
    os.replace(src, dst)


def _write_state(cursors):
    """原子写 state.json（v2 格式）；临时名掺 pid+纳秒，防多进程互截 tmp。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = "%s.tmp.%d.%d" % (STATE_FILE, os.getpid(), time.time_ns())
    payload = {"version": STATE_VERSION, "cursors": cursors,
               "updatedAt": int(time.time() * 1000)}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    _atomic_replace(tmp, STATE_FILE)


def _append_line(record):
    os.makedirs(DATA_DIR, exist_ok=True)
    # 直连 'a' 追加，无中间 tmp——Windows 上 'a' 模式写入到文件尾，天然抗并发追加
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _latest_session_id():
    conn = ds._connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT id FROM session WHERE id NOT LIKE 'sess_subagent_%' "
            "ORDER BY time_updated DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _session_meta(session_id):
    """从 session 表取 slug / project_id。失败置 None。"""
    try:
        conn = ds._connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT slug, project_id FROM session WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return row[0], row[1]
    except Exception:
        pass
    return None, None


def _num_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _query_window(session_id, after_rowid):
    """聚合本会话 rowid 在 (after_rowid, boundary] 窗口内的行。

    先定边界 boundary=该会话 MAX(rowid)（任意 status/source）——行落库即终态，
    边界外的迟到插入留给下一轮，窗口闭合保证不多不少恰好覆盖一次。
    Q1 计数不限 status；Q2 仅 completed 聚合 token/耗时——字段口径与
    db_stats._aggregate 完全一致；两者都排除子代理来源行。
    返回 None 表示窗口内没有新行。
    """
    cond_base = "session_id = ? AND COALESCE(query_source,'') <> 'subagent'"
    win_all = "rowid > ? AND rowid <= ?"
    conn = ds._connect(DB_PATH)
    try:
        brow = conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM model_usage WHERE session_id = ? AND rowid > ?",
            (session_id, after_rowid),
        ).fetchone()
        upper = int(brow[0]) if brow and brow[0] is not None else 0
        if upper <= after_rowid:
            return None
        args = (session_id, after_rowid, upper)
        q1 = (
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='running' THEN 1 ELSE 0 END),0) "
            "FROM model_usage WHERE " + cond_base + " AND " + win_all
        )
        total, err, canc, run = conn.execute(q1, args).fetchone()
        q2 = (
            "SELECT COUNT(*), "
            "COALESCE(SUM(tool_call_count),0), "
            "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(reasoning_tokens),0), "
            "COALESCE(SUM(cache_creation_input_tokens),0), "
            "COALESCE(SUM(cache_read_input_tokens),0), "
            "COALESCE(SUM(duration_ms),0), AVG(time_to_first_token_ms) "
            "FROM model_usage WHERE status='completed' AND " + cond_base + " AND " + win_all
        )
        done_cnt, tool_calls, inp, outp, reas, cache_cre, cache_rd, dur, ttft = \
            conn.execute(q2, args).fetchone()
        q3 = (
            "SELECT turn_id FROM model_usage WHERE status='completed' AND " + cond_base +
            " AND " + win_all + " ORDER BY started_at DESC, rowid DESC LIMIT 1"
        )
        trow = conn.execute(q3, args).fetchone()
    finally:
        conn.close()
    denom = inp
    hit = round(cache_rd / denom, 4) if denom > 0 else 0.0
    return {
        "model_request_count": int(total),
        "error_count": int(err),
        "cancelled_count": int(canc),
        "running_count": int(run),
        "tool_call_count": int(tool_calls),
        "input_tokens": int(inp),
        "output_tokens": int(outp),
        "reasoning_tokens": int(reas),
        "cache_creation_input_tokens": int(cache_cre),
        "cache_read_input_tokens": int(cache_rd),
        "cache_hit_rate": hit,
        "total_duration_ms": int(dur),
        "avg_duration_ms": int(dur) // int(done_cnt) if done_cnt else 0,
        "avg_ttft_ms": float(ttft) if ttft is not None else 0.0,
        "_boundary": upper,
        "_last_turn_id": (trow[0] if trow and trow[0] else None),
    }


def record(session_id=None):
    # subagent 会话过滤：子代理的 Stop 一律不写 jsonl、不推游标、不查库
    if session_id and _is_subagent_sid(session_id):
        _log("subagent session %r skipped" % (session_id,))
        return
    if ds is None:
        _log("db_stats module unavailable (plugin dir moved/upgraded?); skip")
        return
    if not session_id:
        session_id = _latest_session_id()
    if not session_id:
        _log("no session available, skip")
        return

    lock = None
    try:
        lock = _acquire_state_lock()
        _record_locked(session_id)
    finally:
        _release_state_lock(lock)


def _record_locked(session_id):
    """读游标→聚合→append→写游标 全程持有 <state>.lock 排他锁调用。"""
    is_v2, cursors = _read_state()
    if not is_v2 and cursors:
        # 旧版 state.json：时间戳游标一次性迁移为 rowid 游标并立即落盘
        cursors = _migrate_cursors(cursors)
        _write_state(cursors)
        _log("migrated legacy timestamp cursors to rowid cursors (%d sessions)" % len(cursors))
    last_rowid = cursors.get(session_id, 0)

    agg = _query_window(session_id, last_rowid)
    if agg is None or agg["model_request_count"] == 0:
        _log("no new model rows for session %s after rowid=%s; skip" % (session_id, last_rowid))
        return

    new_rowid = agg["_boundary"]
    slug, project_id = _session_meta(session_id)

    record = {
        "schemaVersion": 1,
        "ts": int(time.time() * 1000),
        "sessionId": session_id,
        "slug": slug,
        "projectId": project_id,
        "lastTurnId": agg["_last_turn_id"],
        "modelRequestCount": agg["model_request_count"],
        "errorCount": agg["error_count"],
        "cancelledCount": agg["cancelled_count"],
        "runningCount": agg["running_count"],
        "toolCallCount": agg["tool_call_count"],
        "inputTokens": agg["input_tokens"],
        "outputTokens": agg["output_tokens"],
        "reasoningTokens": agg["reasoning_tokens"],
        "cacheCreationTokens": agg["cache_creation_input_tokens"],
        "cacheReadTokens": agg["cache_read_input_tokens"],
        "cacheHitRate": agg["cache_hit_rate"],
        "totalDurationMs": agg["total_duration_ms"],
        "avgDurationMs": agg["avg_duration_ms"],
        "avgTtftMs": agg["avg_ttft_ms"],
    }
    # 去重键保护：若同会话最近一条记录与本次哈希相同则不追加
    last_line = _last_line_session(session_id)
    if last_line is not None and _dedup_key(last_line) == _dedup_key(record):
        _log("duplicate record skipped for session %s" % session_id)
        return

    _append_line(record)
    cursors[session_id] = new_rowid
    _write_state(cursors)
    _log("appended record for session %s (rowid<=%s) total rows now=%s" % (
        session_id, new_rowid, _line_count()))


def _line_count():
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _last_line_session(session_id):
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("sessionId") == session_id:
                    return obj
    except Exception:
        return None
    return None


def _dedup_key(record):
    return (record.get("sessionId"), record.get("inputTokens"),
            record.get("outputTokens"), record.get("cacheReadTokens"),
            record.get("modelRequestCount"))


def main():
    session_id = None
    if len(sys.argv) > 1:
        session_id = sys.argv[1] or None
    try:
        record(session_id)
    except Exception as e:
        import traceback
        _log("ERROR: %s\n%s" % (e, traceback.format_exc()))
    # stdout 恒为合法空 JSON
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
