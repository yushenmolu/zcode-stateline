#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
inject_context.py — 对话内 token 统计行注入脚本（替换原独立小窗方案）。
被两个钩子调用，通过 hook stdout 输出「严格单对象 JSON」，向模型注入统计
上下文：
  - SessionStart 钩子（--event session-start）：
        注入「会话指令 + 当前统计一行」。指令约定模型在每次回复末尾另起一行
        附带固定格式的统计行。
  - UserPromptSubmit 钩子（--event user-prompt-submit）：
        注入「当前统计一行」（无指令），让模型本轮回复末尾能附上最新统计。

设计要点：
  - 纯标准库（json / os / sys / time / argparse），无第三方依赖。
  - 「当前会话」判定优先级（fail-closed，绝不猜；与 GUI 侧约定一致）：
      1. data_dir/current-session.json 新鲜标记（< 30 秒）→ 用标记会话；
      2. db 唯一活跃主会话：最近 60 秒内有模型调用的、sess_ 开头非
         subagent 的最新会话 → 用它；
      3. jsonl 里 ts 最大且 sessionId 非 subagent 的主会话 → 用它，
         统计行末尾追加标注「（最近会话累计）」；
      4. 都没有 → 输出「（token 统计暂不可用）」。
  - 会话确定后的取数（三种来源一律 db 优先）：db 只读聚合该会话
    completed 且非 subagent 行（input/output/cacheRead/cacheCreation 累计、
    cacheHitRate 重算、avgDuration/avgTtft 取 db 行级均值）；db 无数据时退
    jsonl 增量行求和（avgDurationMs 取最近一条）；仍无数据且会话来自 1/2 →
    「本轮结束后更新」。
  - 兜底标注（如「（最近会话累计）」）拼在统计行末尾，session-start 与
    user-prompt-submit 两种事件都保留。
  - 失败容错（hook 绝不失败）：
      * 任何异常、空文件、无线索，都输出合法 JSON 且 exit 0；
      * stdout 永远是**恰好一行** JSON，且只有一个 additionalContext key
        （多余 key 会导致 ZCode hook 校验失败）。

调用：python inject_context.py [--event session-start|user-prompt-submit]
                                      [--data-dir <path>] [--once]
  - 数据目录优先级：--data-dir > 默认插件数据目录（固定，钩子与 GUI 同目录）。
    ZCODE_PLUGIN_DATA 不再参与解析（该变量在钩子环境指向另一套空目录）。
  - --event 默认 session-start。
  - --once：单次注入即退出（脚本本就单次执行；该开关仅为自测可读性）。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

# 插件根目录：本脚本实际所在目录的上一级（scripts/ -> 0.1.0/）
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
LOG_FILE_NAME = "token-stats.jsonl"
MARK_FILE_NAME = "current-session.json"   # mark_session.py 写入的当前会话标记
MARK_FRESH_MS = 30 * 1000                 # 标记新鲜窗口（30 秒）
DB_ACTIVE_WINDOW_MS = 60 * 1000           # db「活跃主会话」判定窗口（60 秒）

INSTRUCTION_TEXT = (
    u"\u672c\u4f1a\u8bdd\u542f\u7528 token \u7edf\u8ba1\u3002\u8bf7\u5728\u6bcf\u6b21\u56de\u590d\u7684\u672b\u5c3e\uff0c"
    u"\u5355\u72ec\u53e6\u8d77\u4e00\u884c\u9644\u4e0a\u7edf\u8ba1\uff0c\u683c\u5f0f\u56fa\u5b9a\u4e3a\uff1a"
    u"`\u23f1<\u5e73\u5747\u8017\u65f6s> \u00b7 in <\u8f93\u5165k/M> \u00b7 out <\u8f93\u51fak/M> "
    u"\u00b7 cache hit <\u547d\u4e2d\u7387%>`\uff08\u6570\u636e\u4ee5<\u6700\u65b0\u7edf\u8ba1>\u4e3a\u51c6\uff0c"
    u"\u4fdd\u6301\u7b80\u6d01\uff0c\u4e0d\u8d85\u8fc7\u4e00\u884c\uff09\u3002"
)

STATS_UNAVAILABLE_TEXT = u"\uff08token \u7edf\u8ba1\u6682\u4e0d\u53ef\u7528\uff09"
STATS_PENDING_TEXT = u"\uff08\u672c\u8f6e\u7ed3\u675f\u540e\u66f4\u65b0\uff09"
# jsonl 兜底路径的标注（统计行末尾追加，两种事件均保留）
RECENT_FALLBACK_TAG = u"\uff08\u6700\u8fd1\u4f1a\u8bdd\u7d2f\u8ba1\uff09"
DB_DEFAULT = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")


# ----------------------------------------------------------------------------
# 解析与聚合
# ----------------------------------------------------------------------------

def _num(v):
    """安全转数字；失败返回 0。"""
    if v is None:
        return 0
    try:
        return float(v)
    except Exception:
        try:
            return int(v)
        except Exception:
            return 0


def read_jsonl(path):
    """读取 jsonl 全部行；坏行/空行/非 JSON 跳过。返回 (rows, error)。"""
    rows = []
    error = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except Exception as e:  # 文件不存在/权限/锁等
        error = str(e)
    return rows, error


def _is_subagent_sid(sid):
    """session id 是否属于 subagent 会话（排除，不参与当前会话判定与聚合）。"""
    if not sid or not isinstance(sid, str):
        return False
    s = sid.strip().lower()
    return s.startswith("sess_subagent_") or "subagent" in s


def current_session(rows):
    """取 ts 最大记录的**主会话** id（subagent 会话一律跳过；sessionId 为空退 slug）。"""
    candidates = [r for r in rows if not _is_subagent_sid(r.get("sessionId"))]
    if not candidates:
        return None
    latest = max(candidates, key=lambda r: _num(r.get("ts")))
    sid = latest.get("sessionId")
    if not sid:
        sid = latest.get("slug")
    return sid or None


def _db_connect(db_path):
    """只读打开 db.sqlite；失败返回 None（db 不存在/跑在 sqlite 版本不支持等）。"""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        conn.execute("PRAGMA query_only=ON")
        return conn
    except Exception:
        return None


def _time_ms():
    return int(time.time() * 1000)


def read_mark_file(data_dir):
    """读取 current-session.json 标记（mark_session.py 写入
    {"session_id": ..., "updated_at": <ms>}）。

    返回 session_id 或 None —— 文件不存在 / 坏 JSON / 字段缺失 /
    updated_at 距今超过 30 秒新鲜窗口 / 会话属于 subagent，一律 None。
    """
    if not data_dir:
        return None
    path = os.path.join(data_dir, MARK_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    sid = obj.get("session_id")
    if not sid or not isinstance(sid, str):
        return None
    try:
        updated = int(obj.get("updated_at") or 0)
    except Exception:
        updated = 0
    if updated <= 0 or (_time_ms() - updated) > MARK_FRESH_MS:
        return None
    if _is_subagent_sid(sid):
        return None
    return sid


def db_active_session_id(db_path, window_ms=DB_ACTIVE_WINDOW_MS):
    """db 侧「唯一活跃主会话」：最近 window_ms（默认 60 秒）内有模型调用的、
    sess_ 开头且非 subagent 的最新会话（ORDER BY started_at DESC 第一条）。

    db 不可读 / 无符合条件行 → None（调用方降级到下一优先级，绝不猜）。
    """
    if not db_path:
        return None
    conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT session_id FROM model_usage "
            "WHERE started_at > ? "
            "AND session_id LIKE 'sess_%' "
            "AND session_id NOT LIKE 'sess_subagent_%' "
            "ORDER BY started_at DESC LIMIT 1",
            (_time_ms() - window_ms,),
        ).fetchone()
        if row and row[0] and not _is_subagent_sid(row[0]):
            return row[0]
        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_aggregate_session(db_path, session_id):
    """只读聚合 db 中某**主会话**的 completed 行（跳过 subagent 会话与
    query_source='subagent' 调用）。返回 (input, output, cacheRead,
    cacheCreation, avgDurationMs, avgTtftMs) 或 None（会话无数据/读取失败）。"""
    if _is_subagent_sid(session_id):
        return None
    conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cache_read_input_tokens),0), "
            "COALESCE(SUM(cache_creation_input_tokens),0), "
            "COALESCE(AVG(duration_ms),0), "
            "COALESCE(AVG(time_to_first_token_ms),0) "
            "FROM model_usage WHERE session_id = ? AND status='completed' "
            "AND COALESCE(query_source,'') <> 'subagent'",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        inp, outp, cache_rd, cache_cre, avg_dur, avg_ttft = row
        inp = int(inp or 0)
        outp = int(outp or 0)
        cache_rd = int(cache_rd or 0)
        cache_cre = int(cache_cre or 0)
        if inp == 0 and outp == 0 and cache_rd == 0 and cache_cre == 0:
            return None
        return (inp, outp, cache_rd, cache_cre,
                float(avg_dur or 0.0), float(avg_ttft or 0.0))
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def aggregate_session(rows, session_id):
    """对当前会话全部行累计求和（subagent 会话 id 不参与聚合）。返回 (agg, last_record)。"""
    if _is_subagent_sid(session_id):
        return {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheCreationTokens": 0,
            "cacheReadTokens": 0,
        }, None
    agg = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheCreationTokens": 0,
        "cacheReadTokens": 0,
    }
    last = None
    for r in rows:
        if r.get("sessionId") == session_id or (
            not r.get("sessionId") and r.get("slug") == session_id
        ):
            agg["inputTokens"] += int(_num(r.get("inputTokens")))
            agg["outputTokens"] += int(_num(r.get("outputTokens")))
            agg["cacheCreationTokens"] += int(_num(r.get("cacheCreationTokens")))
            agg["cacheReadTokens"] += int(_num(r.get("cacheReadTokens")))
            if last is None or r.get("ts", 0) >= last.get("ts", 0):
                last = r
    return agg, last


def _line_from_agg(agg, tag=None):
    """由聚合 dict 构建统计行文本。tag 非空时追加标注（如"（最近会话累计）"）。"""
    denom = agg["inputTokens"]
    hit_rate = (agg["cacheReadTokens"] / denom if denom > 0 else 0.0) * 100.0
    line = (
        u"\u23f1%.1fs \u00b7 in %s \u00b7 out %s \u00b7 cache hit %.1f%%"
        % (agg["avgDurationMs"] / 1000.0,
           format_tokens(agg["inputTokens"]),
           format_tokens(agg["outputTokens"]),
           hit_rate)
    )
    if tag:
        line += u" " + tag
    return line


def _jsonl_line(agg, last):
    """由 jsonl 聚合 dict + 最近一条记录构建统计行文本
    （avgDurationMs/avgTtftMs 取最近一条记录的值）。"""
    denom = agg["inputTokens"]
    hit_rate = (agg["cacheReadTokens"] / denom if denom > 0 else 0.0) * 100.0
    return (
        u"\u23f1%.1fs \u00b7 in %s \u00b7 out %s \u00b7 cache hit %.1f%%"
        % (_num(last.get("avgDurationMs")) / 1000.0,
           format_tokens(agg["inputTokens"]),
           format_tokens(agg["outputTokens"]), hit_rate)
    )


def build_stats_line(rows, data_dir, db_path):
    """
    生成一行统计文本（fail-closed：绝不猜会话）。

    「当前会话」判定优先级（与 GUI 侧 docked_statusbar 约定一致）：
      1. data_dir/current-session.json 新鲜标记（< 30 秒）→ 用标记会话；
      2. db 唯一活跃主会话：最近 60 秒内有模型调用的、sess_ 开头非
         subagent 的最新会话（ORDER BY started_at DESC 第一条）→ 用它；
      3. jsonl 里 ts 最大且 sessionId 非 subagent 的主会话 → 用它，
         返回标注 RECENT_FALLBACK_TAG（由调用方拼在统计行末尾）；
      4. 都没有 → 返回 (None, None)（调用方输出「token 统计暂不可用」）。

    会话确定后的取数（三种来源一律 db 优先，含 jsonl 兜底判定的会话）：
    db 只读聚合该会话（completed 且非 subagent 行）；db 无数据退 jsonl
    增量累计；仍无数据且会话来自 1/2 → 本轮结束后更新。

    返回 (text, tag)；tag 仅在 jsonl 兜底路径非空。
    """
    # P1: 新鲜标记
    sid = read_mark_file(data_dir)
    source = "mark" if sid else None
    # P2: db 60 秒活跃主会话
    if sid is None:
        sid = db_active_session_id(db_path)
        if sid:
            source = "db-active"
    # P3: jsonl 最新主会话（兜底）
    if sid is None:
        sid = current_session(rows)
        if sid:
            source = "jsonl"

    # P4: 全无线索 → 绝不猜
    if sid is None:
        return None, None

    if source in ("mark", "db-active"):
        # 会话可信：db 行级聚合优先
        agg = db_aggregate_session(db_path, sid)
        if agg is not None:
            inp, outp, cache_rd, cache_cre, avg_dur, avg_ttft = agg
            d = {
                "inputTokens": inp,
                "outputTokens": outp,
                "cacheCreationTokens": cache_cre,
                "cacheReadTokens": cache_rd,
                "avgDurationMs": avg_dur,
                "avgTtftMs": avg_ttft,
            }
            return _line_from_agg(d), None
        # db 无 completed 数据：jsonl 该会话记录兜底
        jagg, last = aggregate_session(rows, sid)
        if last is not None:
            return _jsonl_line(jagg, last), None
        # 会话存在但还没有任何 completed 调用（新会话冷启动）
        return STATS_PENDING_TEXT, None

    # source == "jsonl"：会话线索来自 jsonl 兜底，标注「（最近会话累计）」。
    # 取数与 mark/db-active 路径对齐：db 行级聚合优先，db 无数据再退 jsonl 累计，
    # 避免注入行停留在 jsonl 冻结快照（jsonl 因 Stop 钩子故障停更时仍能取到实时值）。
    db_agg = db_aggregate_session(db_path, sid)
    if db_agg is not None:
        inp, outp, cache_rd, cache_cre, avg_dur, avg_ttft = db_agg
        d = {
            "inputTokens": inp,
            "outputTokens": outp,
            "cacheCreationTokens": cache_cre,
            "cacheReadTokens": cache_rd,
            "avgDurationMs": avg_dur,
            "avgTtftMs": avg_ttft,
        }
        return _line_from_agg(d), RECENT_FALLBACK_TAG
    jagg, last = aggregate_session(rows, sid)
    if last is None:
        return None, None
    return _jsonl_line(jagg, last), RECENT_FALLBACK_TAG


def format_tokens(n):
    """>=1_000_000 -> x.xM（1 位小数）；>=1_000 -> x.xk（1 位小数）；否则原值。"""
    n = int(n)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000.0)
    if n >= 1_000:
        return "%.1fk" % (n / 1_000.0)
    return str(n)


# ----------------------------------------------------------------------------
# 注入上下文输出
# ----------------------------------------------------------------------------

def build_output(event, data_dir, db_path):
    """生成 strict 单对象 additionalContext，任何情况都不抛异常。

    兜底标注（tag）拼在统计行末尾，session-start 与 user-prompt-submit
    两种事件都保留（此前 user-prompt-submit 分支会丢弃标注，已修）。
    """
    log_file = os.path.join(data_dir, LOG_FILE_NAME)
    rows, _err = read_jsonl(log_file)
    line, tag = build_stats_line(rows, data_dir, db_path)

    if line is not None and tag:
        line = line + u" " + tag

    if event == "user-prompt-submit":
        context = line if line is not None else STATS_UNAVAILABLE_TEXT
    else:  # session-start
        if line is None:
            context = INSTRUCTION_TEXT + u"\n" + STATS_UNAVAILABLE_TEXT
        else:
            context = INSTRUCTION_TEXT + u"\n" + line
    return json.dumps(
        {"additionalContext": context},
        ensure_ascii=False,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="ZCode token-stats in-conversation stat line injector")
    ap.add_argument(
        "--event",
        choices=("session-start", "user-prompt-submit"),
        default="session-start",
        help="event kind: session-start (inject instruction + stat line) or "
             "user-prompt-submit (inject latest stat line only)",
    )
    ap.add_argument("--data-dir", default=None, help="override data dir (default plugin data dir)")
    ap.add_argument("--db-path", default=None, help="override db.sqlite path (default cli/db/db.sqlite)")
    ap.add_argument("--once", action="store_true",
                    help="single-shot injection (this script already runs once per invocation; "
                         "kept for self-test readability)")
    args = ap.parse_args(argv)

    data_dir = args.data_dir or DATA_DIR_DEFAULT
    # 注：ZCODE_PLUGIN_DATA 不参与解析——钩子环境下该变量指向另一套空目录
    # （data/zcode-token-stats@local/），曾导致钩子读不到真实统计。
    db_path = args.db_path or DB_DEFAULT

    if args.once:
        pass  # 单次执行即为脚本自然行为，无额外逻辑

    try:
        out = build_output(args.event, data_dir, db_path)
    except Exception:
        # 兜底：任何异常都输出合法 JSON，绝不让 hook 拿到非 JSON / 非 additionalContext
        out = json.dumps({"additionalContext": STATS_UNAVAILABLE_TEXT}, ensure_ascii=False)
    try:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
