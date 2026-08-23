#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
db_stats.py — 只读查询 ZCode 的 db.sqlite，输出 token / 缓存 / 速度统计。

只读原则：以 `file:<path>?mode=ro` 打开连接并执行 `PRAGMA query_only=ON`，
任何写库行为都不存在。SQL 聚合仅针对 model_usage 表。

口径（与 README 一致）：
  - model_request_count / error_count / cancelled_count / running_count 对全部行计数；
  - input/output/reasoning/cache_creation/cache_read/duration 等聚合只对
    status='completed' 的行统计；
  - 行级一律排除子代理调用：COALESCE(query_source,'') <> 'subagent'；
  - cache_hit_rate = cache_read_input_tokens / input_tokens
    （db 的 input_tokens 已含 cache_read 部分），仅看输入侧，排除 cache_creation；
  - avg_ttft_ms = AVG(time_to_first_token_ms) over completed rows；
  - avg_duration_ms = SUM(duration_ms over completed) / completed 行数
    （分子分母同口径，不用全状态行数当分母）；
  - --since 时间边界统一为含边界（started_at >= since），session/history 同语义。

CLI 用法：
  python db_stats.py --mode {session|history|model} [--session-id <id>] [--days N] [--since <ms>] [--json|--table] [--db <path>]
"""
import sqlite3
import json
import argparse
import os
import time

DB_DEFAULT = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")


def _connect(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


STATUS_COND_DONE = ["status='completed'"]
SOURCE_COND = ["COALESCE(query_source,'') <> 'subagent'"]


def _aggregate(db_path, session_id=None, since_ts=None, strict=False):
    """两条独立查询：Q1 计数（不限 status），Q2 聚合 token/耗时（仅 completed）。

    行级一律排除子代理调用（query_source='subagent'）。
    时间过滤默认 `started_at >= since_ts`（含边界，session/history 同语义）；
    strict=True 时改为 `started_at > since_ts`（严格大于，供游标式增量防重）。
    avg_duration_ms 分母 = completed 行数（分子 SUM(duration_ms) 仅来自 completed 行）。
    """
    cond_all, cond_done = list(SOURCE_COND), STATUS_COND_DONE + list(SOURCE_COND)
    args_all, args_done = [], []
    if session_id is not None:
        cond_all.append("session_id = ?")
        cond_done.append("session_id = ?")
        args_all.append(session_id)
        args_done.append(session_id)
    if since_ts is not None:
        op = ">" if strict else ">="
        cond_all.append("started_at %s ?" % op)
        cond_done.append("started_at %s ?" % op)
        args_all.append(since_ts)
        args_done.append(since_ts)
    where_all = (" WHERE " + " AND ".join(cond_all)) if cond_all else ""
    where_done = " WHERE " + " AND ".join(cond_done)
    conn = _connect(db_path)
    try:
        q1 = (
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='running' THEN 1 ELSE 0 END),0) "
            "FROM model_usage" + where_all
        )
        total, err, canc, run = conn.execute(q1, args_all).fetchone()
        q2 = (
            "SELECT COUNT(*), "
            "COALESCE(SUM(tool_call_count),0), "
            "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(reasoning_tokens),0), "
            "COALESCE(SUM(cache_creation_input_tokens),0), "
            "COALESCE(SUM(cache_read_input_tokens),0), "
            "COALESCE(SUM(duration_ms),0), AVG(time_to_first_token_ms) "
            "FROM model_usage" + where_done
        )
        done_cnt, tool_calls, inp, outp, reas, cache_cre, cache_rd, dur, ttft = conn.execute(q2, args_done).fetchone()
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
    }


def query_session_stats(db_path, session_id=None, since_ts=None, strict=False):
    """strict=False（默认）：started_at >= since_ts（含边界）；
    strict=True：started_at > since_ts（游标式增量防重）。返回值结构两种模式完全一致。"""
    return _aggregate(db_path, session_id=session_id, since_ts=since_ts, strict=strict)


def query_history_stats(db_path, since_ts=None, days=7):
    now_ms = int(time.time() * 1000)
    since = since_ts if since_ts is not None else now_ms - days * 86400_000
    agg = _aggregate(db_path, since_ts=since)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT session_id, COUNT(*) FROM model_usage WHERE started_at >= ? "
            "AND COALESCE(query_source,'') <> 'subagent' "
            "AND session_id NOT LIKE 'sess_subagent_%' "
            "GROUP BY session_id",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    agg["per_session"] = [
        {"session_id": r[0], "model_request_count": r[1]} for r in rows
    ]
    agg["window_start_ms"] = since
    agg["window_start_iso"] = _iso(since)
    return agg


def query_model_stats(db_path, since_ts=None):
    conn = _connect(db_path)
    try:
        if since_ts is not None:
            rows = conn.execute(
                """SELECT provider_id, model_id, COUNT(*),
                          COALESCE(SUM(CASE WHEN status='completed' THEN input_tokens ELSE 0 END),0),
                          COALESCE(SUM(CASE WHEN status='completed' THEN output_tokens ELSE 0 END),0),
                          COALESCE(SUM(CASE WHEN status='completed' THEN cache_read_input_tokens ELSE 0 END),0),
                          COALESCE(SUM(CASE WHEN status='completed' THEN cache_creation_input_tokens ELSE 0 END),0),
                          AVG(CASE WHEN status='completed' THEN time_to_first_token_ms END)
                   FROM model_usage WHERE started_at >= ?
                   AND COALESCE(query_source,'') <> 'subagent'
                   GROUP BY provider_id, model_id""",
                (since_ts,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT provider_id, model_id, COUNT(*),
                          COALESCE(SUM(CASE WHEN status='completed' THEN input_tokens ELSE 0 END),0),
                          COALESCE(SUM(CASE WHEN status='completed' THEN output_tokens ELSE 0 END),0),
                          COALESCE(SUM(CASE WHEN status='completed' THEN cache_read_input_tokens ELSE 0 END),0),
                          COALESCE(SUM(CASE WHEN status='completed' THEN cache_creation_input_tokens ELSE 0 END),0),
                          AVG(CASE WHEN status='completed' THEN time_to_first_token_ms END)
                   FROM model_usage WHERE COALESCE(query_source,'') <> 'subagent'
                   GROUP BY provider_id, model_id"""
            ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        inp, outp, cache_rd, cache_cre = r[3], r[4], r[5], r[6]
        denom = (inp or 0)
        hit = round((cache_rd or 0) / denom, 4) if denom > 0 else 0.0
        out.append({
            "provider_id": r[0],
            "model_id": r[1],
            "model_request_count": r[2],
            "input_tokens": r[3],
            "output_tokens": r[4],
            "cache_read_input_tokens": r[5],
            "cache_creation_input_tokens": r[6],
            "cache_hit_rate": hit,
            "avg_ttft_ms": float(r[7]) if r[7] is not None else 0.0,
        })
    out.sort(key=lambda m: m["model_request_count"], reverse=True)
    return out


def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms / 1000))


def _fmt_table(agg):
    lines = []
    lines.append("model_request_count : %d" % agg["model_request_count"])
    lines.append("  status  errors=%d cancelled=%d running=%d" % (
        agg["error_count"], agg["cancelled_count"], agg["running_count"]))
    lines.append("tool_call_count     : %d" % agg["tool_call_count"])
    lines.append("input_tokens        : %d" % agg["input_tokens"])
    lines.append("output_tokens       : %d" % agg["output_tokens"])
    lines.append("reasoning_tokens    : %d" % agg["reasoning_tokens"])
    lines.append("cache_creation_input: %d" % agg["cache_creation_input_tokens"])
    lines.append("cache_read_input    : %d" % agg["cache_read_input_tokens"])
    lines.append("cache_hit_rate      : %.4f" % agg["cache_hit_rate"])
    lines.append("total_duration_ms   : %d" % agg["total_duration_ms"])
    lines.append("avg_duration_ms     : %d" % agg["avg_duration_ms"])
    lines.append("avg_ttft_ms         : %s" % agg["avg_ttft_ms"])
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="ZCode token/speed/cache stats from db.sqlite (read-only)")
    ap.add_argument("--mode", choices=["session", "history", "model"], default="session")
    ap.add_argument("--session-id", default=None, help="session id (session mode)")
    ap.add_argument("--days", type=int, default=7, help="history window days (history mode)")
    ap.add_argument("--since", type=int, default=None,
                    help="since epoch ms, inclusive boundary: rows with started_at >= since "
                         "(overrides --days; same semantics in session/history mode)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--table", action="store_true", help="output human-readable table (default)")
    ap.add_argument("--db", default=DB_DEFAULT)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(json.dumps({"error": "db not found: %s" % args.db}))
        return 2

    if args.mode == "session":
        if not args.session_id:
            args.session_id = _latest_session(args.db)
        agg = query_session_stats(args.db, args.session_id, since_ts=args.since)
        agg["session_id"] = args.session_id
        agg["mode"] = "session"
        if args.json:
            print(json.dumps(agg, ensure_ascii=False))
        else:
            print("session: %s" % args.session_id)
            print(_fmt_table(agg))
    elif args.mode == "history":
        agg = query_history_stats(args.db, since_ts=args.since, days=args.days)
        agg["mode"] = "history"
        agg["days"] = args.days
        if args.json:
            print(json.dumps(agg, ensure_ascii=False))
        else:
            print("history: last %d days (window_start_iso=%s)" % (args.days, agg.get("window_start_iso")))
            print(_fmt_table(agg))
            if agg.get("per_session"):
                print("per_session:")
                for s in agg["per_session"][:20]:
                    print("  %s  %d" % (s["session_id"], s["model_request_count"]))
    else:  # model
        rows = query_model_stats(args.db, since_ts=args.since)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for m in rows:
                print("%s / %s  req=%d  in=%d  out=%d  cacheR=%d  cacheC=%d  hit=%.4f  ttft=%s" % (
                    m["provider_id"], m["model_id"], m["model_request_count"],
                    m["input_tokens"], m["output_tokens"], m["cache_read_input_tokens"],
                    m["cache_creation_input_tokens"], m["cache_hit_rate"], m["avg_ttft_ms"]))
    return 0


def _latest_session(db_path):
    """最近更新的**主会话** id（排除 sess_subagent_* 子代理会话）。"""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM session WHERE id NOT LIKE 'sess_subagent_%' "
            "ORDER BY time_updated DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


if __name__ == "__main__":
    raise SystemExit(main())
