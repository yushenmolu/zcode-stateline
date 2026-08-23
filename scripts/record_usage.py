#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
record_usage.py — Stop 钩子入口：增量聚合 db.sqlite 的已完成模型调用，
将本轮统计追加为一行 JSONL 记录，并推进每会话游标。

设计要点：
  - 只读访问 db.sqlite（`mode=ro` + `PRAGMA query_only=ON`），绝不写库。
  - 幂等：每个 session_id 独立游标 lastRecordedTs；仅当该会话存在
    started_at > lastRecordedTs 的 completed 行时才追加新记录。
    重复调用同一 session_id 且无新数据时不产生重复行。
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

# 从本文件位置推导插件根目录（scripts/ 的上一级），插件升级换版本目录不断链
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(os.path.expanduser(r"~/.zcode/cli/plugins/data"), "local", "zcode-token-stats")
DB_PATH = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "token-stats.jsonl")
STATS_LOG = os.path.join(DATA_DIR, "stats.log")

sys.path.insert(0, os.path.join(PLUGIN_ROOT, "scripts"))
# 延迟容错导入：升级瞬间 scripts 目录可能被替换/移动，绝不因 import 失败
# 让 Stop 钩子挂掉（record() 内会检查 ds 为 None 时记日志并跳过）
try:
    import db_stats as ds
except Exception:
    ds = None


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


def _read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cursors = data.get("cursors")
            if isinstance(cursors, dict):
                return dict(cursors)
        return {}
    except Exception:
        return {}


def _write_state(cursors):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    payload = {"cursors": cursors, "updatedAt": int(time.time() * 1000)}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _append_line(record):
    os.makedirs(DATA_DIR, exist_ok=True)
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


def _last_turn_id(session_id, since_ts):
    """该会话 started_at > since_ts 的 completed 行的最新 turn_id（取整数后缀或原串）。"""
    try:
        conn = ds._connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT turn_id FROM model_usage WHERE session_id = ? AND status='completed' "
                "AND started_at > ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (session_id, since_ts),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None


def _num_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


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

    cursors = _read_state()
    last_ts = cursors.get(session_id, 0)

    # strict=True：时间过滤用 `started_at > last_ts`，与游标推进严格一致，
    # 保证边界毫秒撞行不会重复聚合，实现真正幂等。
    agg = ds.query_session_stats(DB_PATH, session_id=sid_or_none(session_id),
                                 since_ts=last_ts or None, strict=True)
    if agg["model_request_count"] == 0:
        _log("no new completed model rows for session %s since ts=%s; skip" % (session_id, last_ts))
        return

    slug, project_id = _session_meta(session_id)
    # 推进游标：本次覆盖行（started_at > last_ts 且 completed）的最大 started_at
    new_ts = _max_started_at(session_id, last_ts)

    record = {
        "schemaVersion": 1,
        "ts": int(time.time() * 1000),
        "sessionId": session_id,
        "slug": slug,
        "projectId": project_id,
        "lastTurnId": _last_turn_id(session_id, last_ts),
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
    cursors[session_id] = new_ts
    _write_state(cursors)
    _log("appended record for session %s (ts=%s) total rows now=%s" % (
        session_id, new_ts, _line_count()))


def sid_or_none(session_id):
    return session_id or None


def _max_started_at(session_id, since_ts):
    """本次覆盖行的最大 started_at（started_at > since_ts 且 completed）。
    无新行时返回 since_ts（游标不推进，保幂等）；绝不 fallback 到当前时间。"""
    try:
        conn = ds._connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT MAX(started_at) FROM model_usage WHERE session_id = ? AND status='completed' AND started_at > ?",
                (session_id, since_ts),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    return since_ts


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
