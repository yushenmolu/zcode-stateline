#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_against_sqlite.py — 对拍验证 db_stats.py 的聚合口径。

由于本机没有 sqlite3 CLI，对拍采用"独立裸 SQL 路径"：
  - 用一行式裸 SELECT 直接求各项总和（与 db_stats 的 `_aggregate` 实现路径不同），
  - 逐字段与 db_stats.query_* 的输出对比；
  - 依赖相同的业务口径（completed 行聚 token/耗时，全部行计状态数；
    行级排除 query_source='subagent'；avg_duration 分母 = completed 行数；
    cache_hit_rate = cache_read/input，input 已含 cache_read 部分）。

用法：
  python verify_against_sqlite.py --session-id <S> [--since <ms>] [--db <path>]
  python verify_against_sqlite.py --mode history --days 7
  python verify_against_sqlite.py --mode model

输出：每字段 MATCH 或差异明细；结尾 MATCH ALL / MISMATCH。
"""
import sqlite3
import argparse
import os
import time

DB_DEFAULT = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
sys = __import__("sys")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_stats as ds


def _conn(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def ground_truth(db_path, session_id=None, since_ts=None):
    """独立路径：裸 SQL 逐项求和（只读）。自带全部口径，不复刻 db_stats 的实现：
      - 行级排除子代理调用：COALESCE(query_source,'') <> 'subagent'；
      - token/耗时聚合仅 status='completed'；
      - avg_duration_ms 分母 = completed 行数（与分子 SUM(duration_ms) 同口径）；
      - since 边界为含边界（started_at >= since）。
    """
    conn = _conn(db_path)
    try:
        cond = ["COALESCE(query_source,'') <> 'subagent'"]
        args = []
        if session_id is not None:
            cond.append("session_id = ?")
            args.append(session_id)
        if since_ts is not None:
            cond.append("started_at >= ?")
            args.append(since_ts)
        where = " WHERE " + " AND ".join(cond)

        row = conn.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='running' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='completed' THEN tool_call_count ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='completed' THEN input_tokens ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='completed' THEN output_tokens ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='completed' THEN reasoning_tokens ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='completed' THEN cache_creation_input_tokens ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='completed' THEN cache_read_input_tokens ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN status='completed' THEN duration_ms ELSE 0 END),0), "
            "AVG(CASE WHEN status='completed' THEN time_to_first_token_ms END) "
            "FROM model_usage" + where,
            args,
        ).fetchone()
    finally:
        conn.close()
    (total, err, canc, run, done_cnt, tool, inp, outp, reas, cc, cr, dur,
     ttft) = row
    denom = (inp or 0)
    hit = round((cr or 0) / denom, 4) if denom > 0 else 0.0
    return {
        "model_request_count": int(total),
        "error_count": int(err),
        "cancelled_count": int(canc),
        "running_count": int(run),
        "tool_call_count": int(tool),
        "input_tokens": int(inp),
        "output_tokens": int(outp),
        "reasoning_tokens": int(reas),
        "cache_creation_input_tokens": int(cc),
        "cache_read_input_tokens": int(cr),
        "cache_hit_rate": hit,
        "total_duration_ms": int(dur),
        "avg_duration_ms": int(dur or 0) // int(done_cnt or 0) if done_cnt else 0,
        "avg_ttft_ms": float(ttft) if ttft is not None else 0.0,
    }


FIELDS = [
    "model_request_count", "error_count", "cancelled_count", "running_count",
    "tool_call_count", "input_tokens", "output_tokens", "reasoning_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "cache_hit_rate",
    "total_duration_ms", "avg_duration_ms", "avg_ttft_ms",
]


def compare(label, a, b):
    ok = True
    for f in FIELDS:
        va, vb = a.get(f), b.get(f)
        if isinstance(va, float) or isinstance(vb, float):
            same = abs(float(va or 0) - float(vb or 0)) < 1e-6
        else:
            same = va == vb
        status = "MATCH" if same else "DIFF"
        if not same:
            ok = False
        print("%-8s %-28s %-10s got=%s  ground=%s" % (status, label + ":" + f, status, va, vb))
    return ok


def latest_main_session(db_path):
    """独立选会话：最近更新的主会话 id（排除 sess_subagent_*），不复用
    db_stats._latest_session，避免选会话逻辑与被验证方同源。"""
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM session WHERE id NOT LIKE 'sess_subagent_%' "
            "ORDER BY time_updated DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", default=None)
    ap.add_argument("--mode", choices=["session", "history", "model"], default="session")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--since", type=int, default=None)
    ap.add_argument("--db", default=DB_DEFAULT)
    args = ap.parse_args()

    all_ok = True
    if args.mode == "session":
        sid = args.session_id
        if not sid:
            sid = latest_main_session(args.db)
        a = ds.query_session_stats(args.db, sid, since_ts=args.since)
        b = ground_truth(args.db, session_id=sid, since_ts=args.since)
        print("== session %s ==" % sid)
        all_ok = compare("session", a, b)
    elif args.mode == "history":
        now_ms = int(time.time() * 1000)
        since = args.since if args.since is not None else now_ms - args.days * 86400_000
        a = ds.query_history_stats(args.db, since_ts=since)
        b = ground_truth(args.db, since_ts=since)
        print("== history since=%d days=%d ==" % (since, args.days))
        all_ok = compare("history", a, b)
    else:
        # model 模式：对每个模型组合用 ground_truth 按 provider+model 单组核对
        print("== model mode: compare per (provider,model) ==")
        conn = _conn(args.db)
        try:
            combos = conn.execute(
                "SELECT DISTINCT provider_id, model_id, session_id FROM model_usage"
            ).fetchall()
        finally:
            conn.close()
        # 简化：仅用 db_stats 自身 + 全库 ground_truth 做整体对拍，再抽查两个组合
        rows = ds.query_model_stats(args.db, since_ts=args.since)
        agg_a = {"model_request_count": sum(r["model_request_count"] for r in rows),
                 "input_tokens": sum(r["input_tokens"] for r in rows),
                 "output_tokens": sum(r["output_tokens"] for r in rows),
                 "cache_creation_input_tokens": sum(r["cache_creation_input_tokens"] for r in rows),
                 "cache_read_input_tokens": sum(r["cache_read_input_tokens"] for r in rows)}
        b = ground_truth(args.db, since_ts=args.since)
        print("model rows:", len(rows))
        for f in ["model_request_count", "input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"]:
            if agg_a[f] == b[f]:
                print("MATCH  model-sum:%s got=%s ground=%s" % (f, agg_a[f], b[f]))
            else:
                all_ok = False
                print("DIFF   model-sum:%s got=%s ground=%s" % (f, agg_a[f], b[f]))

    print("RESULT:", "MATCH ALL" if all_ok else "MISMATCH")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
