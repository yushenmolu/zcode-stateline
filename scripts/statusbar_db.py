#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_db.py — docked_statusbar 的 db.sqlite 只读查询层（Stage 3 Step 2 抽离）。

本模块集中所有对 db.sqlite 的只读查询（model_usage / tool_usage 等表）与
支撑它们的 DB 常量、小工具函数；连接一律只读（mode=ro + PRAGMA query_only），
支持 conn 透传复用。docked_statusbar 通过 `from statusbar_db import *`
整体 re-export，旧名（dsb._db_connect、dsb.db_session_speed 等）全部保留可用。
会话判定（tail_session_resume / resolve_session_sticky 等）仍留在
docked_statusbar，经 re-export 使用本模块的 _db_connect / db_* 查询。
"""

import os
import sqlite3
import time

__all__ = [
    # 常量
    "BACKGROUND_QUERY_SOURCES", "INTERACTIVE_SOURCE_SQL", "ACTIVITY_GRACE_MS",
    "TOOL_LIVE_MAX_MS", "DB_ACTIVE_WINDOW_MS",
    "COLD_READ_RATIO", "COLD_READ_COUNT_SQL",
    # 小工具（被 DB 层与 docked_statusbar 共用，经 re-export 沿用旧名）
    "_num", "time_ms", "_is_subagent_sid",
    # 连接与查询
    "_db_connect",
    "db_recent_session_activity", "db_latest_session_id", "db_latest_model_id",
    "db_session_title", "db_aggregate_session", "db_session_speed",
    "recent_turn_stats", "turn_window_left_edge", "live_turn_stats",
    "db_turn_request_profile", "db_session_model_activity_ts",
    "db_tool_activity", "db_recent_tool_activity", "tool_live_ms",
    "db_latest_speed", "db_today_stats", "db_latest_model_input",
    "db_recent_sessions",
    "today_start_ms",
]

# ---- DB 常量（被本模块查询引用；docked_statusbar 其他部分经 re-export 沿用）----
# 后台模型调用来源：不出自用户这一轮交互，不能代表「该会话正在生成」，也不该
# 参与 tok/s。实测本机 session_title 98 行（p50 6.8s）、compact 54 行
# （p50 75.6s）——短输出 + 独立耗时，混进速度口径会把 tok/s 拉到完全无关的量级。
BACKGROUND_QUERY_SOURCES = ("subagent", "compact", "session_title")
INTERACTIVE_SOURCE_SQL = (
    "AND COALESCE(query_source, '') NOT IN (%s) "
    % ", ".join("'%s'" % s for s in BACKGROUND_QUERY_SOURCES))

ACTIVITY_GRACE_MS = 90 * 1000      # 生成中心跳续期窗口：事件超窗但本会话确有模型
                                   # 调用在事件后完成，则维持「生成中」

TOOL_LIVE_MAX_MS = 600 * 1000         # running 工具行的采信上限（0.9.2）：tool_usage
                                      # 在工具**开始时**就落行，故「仍在 running」就是
                                      # 「此刻确有一把工具在跑」的权威证据；但进程被强杀
                                      # 时该行的 completed_at 永远不再更新（实测本机有
                                      # 挂了 27 天的 running 僵尸行），必须按起点封顶

DB_ACTIVE_WINDOW_MS = 180 * 1000  # db 兜底判定窗口：最近 180 秒内有模型调用才算活跃

# ---- 冷读判定（0.9.4）----
# 一次模型调用若 cache_read_input_tokens 占 input_tokens 不到此比例，就算「冷读」
# （整份 prompt 基本没命中缓存）。取 1% 是实测双峰的结果：本机 9386 条非后台
# completed 调用中 16.6% 落在 <1%（该带内 cache_read 最大仅 2432）、1%~5% 只有
# 0.4%（波谷）、75.4% 落在 >=90%。阈值取在波谷里，往任一侧挪一档结论都不变。
COLD_READ_RATIO = 0.01
# SUM(CASE) 形态：NULL 参与的比较结果为 NULL，走 ELSE 0，故 input/cache_read 缺失
# 的行既不计数也不会炸查询。
COLD_READ_COUNT_SQL = (
    "COALESCE(SUM(CASE WHEN cache_read_input_tokens < input_tokens * %s "
    "THEN 1 ELSE 0 END), 0)" % COLD_READ_RATIO)

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


def time_ms():
    """当前 epoch 毫秒（通用时钟，只用于 freshness 判断与展示）。"""
    return int(time.time() * 1000)


def _is_subagent_sid(sid):
    """session id 是否属于 subagent 会话（ZCode 给子代理开的独立会话）。

    子代理（subagent）会被 ZCode 以独立 session_id 运行（形如
    ``sess_subagent_*``），它们的 Stop / UserPromptSubmit 也会触发本插件钩子。
    状态条 / 注入行应当展示**用户正在主会话**的统计，所以凡是 subagent
    会话一律排除（既不做当前会话，也不参与聚合）。
    """
    if not sid or not isinstance(sid, str):
        return False
    s = sid.strip().lower()
    return s.startswith("sess_subagent_") or "subagent" in s



def _db_connect(db_path):
    """只读打开 db.sqlite；失败返回 None。"""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        conn.execute("PRAGMA query_only=ON")
        return conn
    except Exception:
        return None



def db_recent_session_activity(db_path, window_ms=DB_ACTIVE_WINDOW_MS,
                               conn=None):
    """db 侧「最近活动」信号：window_ms 毫秒内最新主会话模型调用行的
    (session_id, started_at)。返回真实活动时间戳供粘滞判定信号竞争
    （处置：db 候选不得用读取时刻伪造 ts）。
    窗口外/无行/失败返回 (None, 0)。
    conn 传入时复用（不关闭），否则自开自关。"""
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None, 0
    try:
        since = time_ms() - int(window_ms)
        row = conn.execute(
            "SELECT session_id, started_at FROM model_usage "
            "WHERE session_id LIKE 'sess_%' "
            "AND session_id NOT LIKE 'sess_subagent_%' "
            "AND started_at >= ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (since,),
        ).fetchone()
        if row and row[0]:
            try:
                return row[0], int(row[1] or 0)
            except Exception:
                return row[0], 0
        return None, 0
    except Exception:
        return None, 0
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_latest_session_id(db_path, conn=None):
    """db 侧确定当前活跃主会话：最新 model_usage 的 session_id（跳过 subagent），
    兜底最新 session（同样跳过 subagent）。
    conn 传入时复用（不关闭），否则自开自关。"""
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None, "db access failed"
    try:
        row = conn.execute(
            "SELECT session_id FROM model_usage "
            "WHERE session_id NOT LIKE 'sess_subagent_%' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return row[0], None
        row = conn.execute(
            "SELECT id FROM session "
            "WHERE id NOT LIKE 'sess_subagent_%' "
            "ORDER BY time_updated DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return row[0], None
        return None, None
    except Exception as e:
        return None, str(e)
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_latest_model_id(db_path, session_id, conn=None):
    """会话最新 model_usage 行的 model_id（ORDER BY started_at DESC）；无则 None。
    subagent 会话不参与展示。
    conn 传入时复用（不关闭），否则自开自关。"""
    if not session_id or _is_subagent_sid(session_id):
        return None
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT model_id FROM model_usage WHERE session_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return (row[0] if row and row[0] else None)
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_session_title(db_path, session_id, conn=None):
    """session 表按 session_id 取 title（去首尾空白）；subagent / 无则 ''。
    conn 传入时复用（不关闭），否则自开自关。"""
    if not session_id or _is_subagent_sid(session_id):
        return ""
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return ""
    try:
        row = conn.execute(
            "SELECT title FROM session WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
        return ""
    except Exception:
        return ""
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_aggregate_session(db_path, session_id, conn=None):
    """只读聚合某**主会话**的 completed 行（subagent 会话返回 None，不参与统计）。
    主数据源行级聚合：avgDuration = AVG(completed 行 duration_ms)。
    返回标准 stats dict 或 None（会话无数据/读取失败）。
    conn 传入时复用（不关闭），否则自开自关。"""
    if _is_subagent_sid(session_id):
        return None
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cache_read_input_tokens),0), "
            "COALESCE(SUM(cache_creation_input_tokens),0), "
            "COALESCE(SUM(reasoning_tokens),0), "
            "COALESCE(AVG(duration_ms),0) "
            "FROM model_usage WHERE session_id = ? AND status='completed' "
            "AND COALESCE(query_source,'') <> 'subagent'",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        inp, outp, cache_rd, cache_cre, reas, avg_dur = row
        inp = int(inp or 0)
        outp = int(outp or 0)
        cache_rd = int(cache_rd or 0)
        cache_cre = int(cache_cre or 0)
        reas = int(reas or 0)
        if inp == 0 and outp == 0 and cache_rd == 0 and cache_cre == 0:
            return None
        return {
            "inputTokens": inp,
            "outputTokens": outp,
            "cacheReadTokens": cache_rd,
            "cacheCreationTokens": cache_cre,
            "reasoningTokens": reas,
            "avgDurationMs": float(avg_dur or 0.0),
        }
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_session_speed(db_path, session_id, conn=None):
    """当前会话的输出速度（tok/s，只读 db；jsonl 无行级数据故仅此一路）。

    - recent = 最近一条 completed 非 subagent 行的 output_tokens/(duration_ms/1000)
      （ORDER BY started_at DESC LIMIT 1；该行 duration_ms 为 NULL/0 时无速度）；
    - avg = SUM(output_tokens)/SUM(duration_ms)*1000（会话平均，completed 行）。
    返回 (recent, avg)，各自可为 None（无 db / 无会话 / 无有效数据）。
    conn 传入时复用（不关闭），否则自开自关；内部两条 SQL 共用同一 conn。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None, None
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None, None
    try:
        recent = None
        row = conn.execute(
            "SELECT output_tokens, duration_ms FROM model_usage "
            "WHERE session_id = ? AND status='completed' "
            + INTERACTIVE_SOURCE_SQL +
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is not None and row[0] and row[1]:
            dur = _num(row[1])
            if dur > 0:
                recent = row[0] / (dur / 1000.0)
        avg = None
        row2 = conn.execute(
            "SELECT COALESCE(SUM(output_tokens),0), COALESCE(SUM(duration_ms),0) "
            "FROM model_usage WHERE session_id = ? AND status='completed' "
            + INTERACTIVE_SOURCE_SQL.rstrip() + " " +
            "AND output_tokens > 0 AND duration_ms > 0",
            (session_id,),
        ).fetchone()
        if row2 is not None and row2[0] and row2[1]:
            avg = row2[0] / row2[1] * 1000.0
        return recent, avg
    except Exception:
        return None, None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def recent_turn_stats(db_path, session_id, conn=None):
    """当前会话**最近一轮** turn_usage 统计（0.4.0；表 PRIMARY KEY 为
    (session_id, turn_id)，按 started_at 取最新一行；subagent 过滤同前，
    不参与统计）。只读 db。

    返回 dict（无数据 / 读取失败返回 None）：
      {turn_id, status, startedAt, completedAt, durationMs,
       timeToFirstTokenMs, toolCallCount, toolErrorCount,
       inputTokens, outputTokens, cacheReadTokens, computedTotalTokens,
       errorType, cancelledByUser, contextExceeded}
    供「本轮统计」行与状态判定（turn 未完成特征）使用。
    conn 传入时复用（不关闭），否则自开自关。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT turn_id, status, started_at, completed_at, duration_ms, "
            "time_to_first_token_ms, tool_call_count, tool_error_count, "
            "input_tokens, output_tokens, cache_read_input_tokens, "
            "computed_total_tokens, error_type, cancelled_by_user, "
            "context_exceeded "
            "FROM turn_usage WHERE session_id = ? "
            "AND COALESCE(session_id, '') NOT LIKE 'sess_subagent_%' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        (turn_id, status, started_at, completed_at, duration_ms,
         ttft, tool_calls, tool_errors, inp, outp, cache_rd, total,
         err_type, cancelled_by, ctx_exceeded) = row
        return {
            "turn_id": turn_id,
            "status": status,
            "startedAt": started_at,
            "completedAt": completed_at,
            "durationMs": duration_ms,
            "timeToFirstTokenMs": ttft,
            "toolCallCount": int(tool_calls or 0),
            "toolErrorCount": int(tool_errors or 0),
            "inputTokens": int(inp or 0),
            "outputTokens": int(outp or 0),
            "cacheReadTokens": int(cache_rd or 0),
            "computedTotalTokens": int(total or 0),
            # 收尾专属字段（徽标 tooltip 用：光有「出错」两字看不出为什么出错）
            "errorType": err_type,
            "cancelledByUser": int(cancelled_by or 0) if cancelled_by is not None else None,
            "contextExceeded": int(ctx_exceeded or 0) if ctx_exceeded is not None else None,
        }
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def turn_window_left_edge(tu_row):
    """本轮「实时统计 / 速度」窗口的左沿（epoch 毫秒），None = 不设下界。

    取**上一轮** turn_usage 行的收尾时刻：turn_usage 只在轮次结束时落库，所以
    「该会话最新一条 turn 行的 completed_at」之后落库的交互来源 model_usage 行
    必然出自更新的那一轮——也就是正在进行中的这一轮。这样比较的两侧都来自 DB
    自身时钟，不再依赖钩子落盘时刻与 DB 时钟对齐（实测 UserPromptSubmit 写盘的
    turn_started_at 可比该轮 started_at 晚 17.0s，滞后量还随负载变化——任何固定
    容差都会踩穿，表现为本轮首次调用被排除出窗口：明明在跑本轮却标「· 上一轮」，
    速度也要晚一个调用才出现）。

    返回 None 表示该会话还没有任何已收尾的轮次（首轮）：此时本轮之前的行
    本来就全部属于本轮，不设下界才是对的。
    """
    try:
        val = int((tu_row or {}).get("completedAt") or 0)
    except Exception:
        return None
    return val or None


def live_turn_stats(db_path, session_id, since_ms, conn=None,
                    now_ms=None):
    """**本轮进行中**的实时统计：聚合该会话 since_ms 之后的 model_usage 行。

    为什么需要它：turn_usage 行只在轮次结束时才落库（实测本机 completed_at 为
    NULL 的行数为 0），所以生成中直接读 recent_turn_stats 拿到的必然是**上一轮**
    的数字，而徽标同时显示「生成中」——这就是「本轮统计和状态对不上」的根源。
    model_usage 每次模型调用完成即落库，按本轮左沿聚合即可得到本轮到目前为止的
    真实累计量（in/out/cache read），耗时用「现在 - 本轮首行起点」而非行内 duration。

    since_ms 是**纯下界**（由调用方给，见 turn_window_left_edge）：None 表示不设
    下界（会话首轮）。0.9.0 曾要求它非空、并把钩子时刻当本轮起点，故已改。

    返回与 recent_turn_stats **同键**的 dict（另多两个请求级计数
    modelCallCount / coldReadCount，见 db_turn_request_profile 为何需要它们），
    并带 "live": True；本轮尚无模型调用落库 / 读取失败 -> None。
    conn 传入时复用（不关闭），否则自开自关。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None
    if now_ms is None:
        now_ms = time_ms()
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        sql = ("SELECT COUNT(*), "
               "  COALESCE(SUM(input_tokens), 0), "
               "  COALESCE(SUM(output_tokens), 0), "
               "  COALESCE(SUM(cache_read_input_tokens), 0), "
               "  COALESCE(SUM(computed_total_tokens), 0), "
               "  COALESCE(SUM(tool_call_count), 0), "
               "  MIN(started_at), "
               "  COALESCE(MIN(first_token_at), 0), "
               "  COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0), "
               "  %s "
               "FROM model_usage WHERE session_id = ? " % COLD_READ_COUNT_SQL)
        args = [session_id]
        if since_ms is not None:
            sql += "AND started_at >= ? "
            args.append(int(since_ms))
        # INTERACTIVE_SOURCE_SQL 自带前导 AND，故接在 WHERE 尾部而非拼进 args
        row = conn.execute(sql + INTERACTIVE_SOURCE_SQL, tuple(args)).fetchone()
        if row is None or not row[0]:
            return None
        (calls, inp, outp, cache_rd, total, tools, first_start,
         first_token, err_calls, cold_calls) = row
        try:
            started_at = int(first_start)
        except (TypeError, ValueError):
            # 有行却取不到起点（started_at 为 NULL）：退回下界，再退回现在
            started_at = int(since_ms) if since_ms is not None else int(now_ms)
        # first_token_at 是绝对时刻，而 recent_turn_stats 的同名字段是**时长**
        # （turn_usage.time_to_first_token_ms）——这里换算成时长，两个数据源的
        # 键才真正同义。
        try:
            ttft = (int(first_token) - started_at) if first_token else None
            if ttft is not None and ttft < 0:
                ttft = None
        except Exception:
            ttft = None
        return {
            "turn_id": None,
            "status": "running",
            "startedAt": started_at,
            "completedAt": None,
            "durationMs": max(0, int(now_ms) - started_at),
            "timeToFirstTokenMs": ttft,
            "toolCallCount": int(tools or 0),
            "toolErrorCount": int(err_calls or 0),
            "inputTokens": int(inp or 0),
            "outputTokens": int(outp or 0),
            "cacheReadTokens": int(cache_rd or 0),
            "computedTotalTokens": int(total or 0),
            # 收尾专属字段（errorType / cancelledByUser / contextExceeded）在
            # 这里恒 None：本轮还在跑，turn_usage 尚未落库，没有结局可报。
            # 保持与 recent_turn_stats 同键，渲染层才是一条代码路径。
            "errorType": None,
            "cancelledByUser": None,
            "contextExceeded": None,
            "modelCallCount": int(calls or 0),
            "coldReadCount": int(cold_calls or 0),
            "live": True,
        }
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_turn_request_profile(db_path, session_id, since_ms, until_ms, conn=None):
    """某一轮**请求级**画像 `(n_calls, n_cold)`：窗口内模型调用次数、其中冷读次数。

    为什么不能只读 turn_usage：那里只有轮次收尾时的 token 总量，没有逐次调用的
    cache_read 分布，而「本轮 n 次请求里 k 次冷读」正是解释「这一轮命中率怎么这么低」
    的量（实测 49.7% 那轮 = 2 次请求、1 次冷读：一次 cr=64/161403 几乎全冷读，
    与另一次满命中平均后恰好腰斩）。故按该轮时间窗回 model_usage 数一遍。

    **不按 query_source 过滤**（与 live_turn_stats 相反，这是要点不是疏漏）：画像
    必须和被它解释的那串 token 同一个总体。本机对账 12 个主会话轮次，turn_usage 的
    input_tokens 与窗口内**全部** model_usage 行之和 12/12 完全相等（含 compact /
    session_title），而 model_request_count 也把这些算进去（一轮 3 次里有 1 次是
    compact：过滤后台来源后只剩 2 次，画像就对不上眼前数字了）。

    上下界都必须给（闭区间）：上界缺失意味着这轮还在跑，那种情况走
    live_turn_stats（同一条查询已带回这两个计数），在这里放开上界会把**下一轮**的
    调用算进**上一轮**的画像里。
    返回 None = 无数据 / 参数缺失 / 读取失败（调用方据此不显示该句，而不是显示 0）。
    conn 传入时复用（不关闭），否则自开自关。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None
    try:
        since_ms = int(since_ms)
        until_ms = int(until_ms)
    except (TypeError, ValueError):
        return None
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT COUNT(*), %s FROM model_usage "
            "WHERE session_id = ? AND started_at >= ? AND started_at <= ?"
            % COLD_READ_COUNT_SQL,
            (session_id, since_ms, until_ms)).fetchone()
        if row is None or not row[0]:
            return None
        return int(row[0]), int(row[1] or 0)
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_session_model_activity_ts(db_path, session_id, since_ms=None,
                                 conn=None):
    """该会话最近一次**真实模型调用**的完成时刻（epoch 毫秒），用于生成中心跳。

    取代 0.8.0 的 db_session_activity_ts（读 session.time_updated）：那个字段
    由任意写入者刷新（消息/part 落库、后台标题与 compact 调用都会动它），把
    早已结束的轮次一路续成「生成中」——它衡量的不是「这个会话在生成」。
    这里只认 model_usage 里排除后台来源的行，且可选只取 since_ms（最后一条钩子
    事件时刻）之后完成的调用：事件之后再无模型调用完成，就说明没有任何东西在跑。

    返回 int 或 None（无该会话 / 读取失败 / 无符合条件行）。subagent 会话 None。
    conn 传入时复用（不关闭），否则自开自关。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        sql = ("SELECT MAX(completed_at) FROM model_usage "
               "WHERE session_id = ? AND completed_at IS NOT NULL "
               + INTERACTIVE_SOURCE_SQL)
        args = [session_id]
        if since_ms is not None:
            sql += " AND completed_at >= ? "
            args.append(int(since_ms) - ACTIVITY_GRACE_MS)
        row = conn.execute(sql, tuple(args)).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_tool_activity(db_path, session_id, conn=None):
    """本会话**最近一把工具**的 tool_usage 行（0.9.2）。

    为什么需要它：tool_usage 在工具**开始**时就落行（实测抓到一条
    `TaskOutput / status=running / completed_at=NULL / 起点 154 秒前`，而这
    154 秒里 model_usage 没有任何新行、钩子文件也没有新事件），所以它是长工具
    运行期唯一能表达「此刻仍在跑」的实时源，也是唯一报得出**工具名**的源。
    0.9.1 及之前判「工具中」只能靠钩子事件 + 900 秒硬窗口，窗口内是真在跑、
    超窗就只能猜——且永远说不出在跑什么。

    返回 {'toolName','status','startedAt','completedAt','errorType'}；
    subagent 会话 / 无行 / 读取失败 -> None。conn 传入时复用（不关闭）。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT tool_name, status, started_at, completed_at, error_type "
            "FROM tool_usage WHERE session_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass
    if row is None:
        return None
    return {
        "toolName": row[0] or "",
        "status": row[1],
        "startedAt": row[2],
        "completedAt": row[3],
        "errorType": row[4],
    }


def db_recent_tool_activity(db_path, conn=None, window_ms=TOOL_LIVE_MAX_MS,
                            now_ms=None):
    """跨主会话找「起点在窗口内且仍在 running」的最近一把工具 -> (sid, ts)。

    供会话身份竞争用（0.9.2）：长工具运行期间 mark（30s）、钩子事件（60s）、
    model_usage 活动（180s）三路信号会全部过窗（实测一把 154 秒的工具就足够），
    此时只有未收尾的 tool_usage 行能证明「这个会话此刻在干活」。

    无符合条件行 / 读取失败 -> (None, 0)。running 僵尸行（进程被强杀后
    completed_at 永不更新，实测本机最老一条挂了 27 天）由 window_ms 排除。
    """
    if now_ms is None:
        now_ms = time_ms()
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None, 0
    try:
        row = conn.execute(
            "SELECT session_id, started_at FROM tool_usage "
            "WHERE status = 'running' AND completed_at IS NULL "
            "  AND started_at >= ? "
            "  AND session_id NOT LIKE 'sess_subagent_%' "
            "ORDER BY started_at DESC LIMIT 1",
            (int(now_ms) - int(window_ms),),
        ).fetchone()
    except Exception:
        row = None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass
    if not row or not row[0] or not row[1]:
        return None, 0
    return row[0], int(row[1])


def tool_live_ms(tool_row, now_ms=None, max_ms=TOOL_LIVE_MAX_MS):
    """该工具行「仍在跑」的已持续毫秒；不满足仍在跑的条件 -> None。

    仍在跑 = status='running' 且 completed_at 为空（两个条件都要：收尾行可能
    只写 completed_at 而 status 滞后）且起点在 max_ms 内。上限用来挡僵尸 running
    行（进程被强杀后 completed_at 永不更新），见 TOOL_LIVE_MAX_MS 注释。
    """
    if not isinstance(tool_row, dict):
        return None
    if tool_row.get("status") != "running" or tool_row.get("completedAt"):
        return None
    try:
        started = float(tool_row.get("startedAt") or 0)
    except Exception:
        return None
    if not started:
        return None
    if now_ms is None:
        now_ms = time_ms()
    try:
        live = float(now_ms) - started
    except Exception:
        return None
    if live < 0 or live >= max_ms:
        # 起点在将来（时钟回拨 / 宿主与 DB 时钟不一致）：不采信，免得倒着算
        # 出负寿命；起点早于上限：僵尸行，见 docstring
        return None
    return live



def db_latest_speed(db_path, session_id=None, conn=None, since_ms=None):
    """最近一条真实模型调用的精确速度：output_tokens/(duration_ms/1000)。
    返回 float 或 None（无数据 / 失败）。tok/s 数据源为 db 精确值，不依赖
    live_stream。

    session_id 为 None 时不过滤会话（保持旧行为：全库最近一条）；非 None 时
    只取该会话最近一条（避免跨会话取到别的会话的速度）。
    since_ms 非 None 时只取该时刻起算的行——把速度限定在**本轮窗口**内，
    否则「生成中」显示的是上一轮的历史速度。
    来源过滤排除 subagent / compact / session_title（后台调用输出短、耗时独立，
    混入会把 tok/s 拉偏）。conn 传入时复用（不关闭），否则自开自关。"""
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        where = ["status = 'completed'",
                 "output_tokens > 0 AND duration_ms > 0"]
        args = []
        if session_id is not None:
            where.append("session_id = ?")
            args.append(session_id)
        if since_ms is not None:
            where.append("started_at >= ?")
            args.append(int(since_ms))
        # INTERACTIVE_SOURCE_SQL 自带前导 AND，故接在 WHERE 尾部而非 join 列表里
        row = conn.execute(
            "SELECT output_tokens, duration_ms FROM model_usage WHERE "
            + " AND ".join(where) + " " + INTERACTIVE_SOURCE_SQL
            + " ORDER BY started_at DESC, rowid DESC LIMIT 1",
            tuple(args),
        ).fetchone()
        if row is None:
            return None
        outp, dur = row
        if not outp or not dur:
            return None
        return outp / (dur / 1000.0)
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def today_start_ms(now_ms=None):
    """今日 0 点的 epoch 毫秒（本地时间）。

    「今日用量」按自然日聚合，边界是本地时区的 0 点——用户看「今天用了多少」
    对的是日历上的今天，不是 UTC 日。now_ms 缺省取当前时刻（注入便于测试
    跨日边界）。
    """
    import datetime
    if now_ms is None:
        now_ms = time_ms()
    dt = datetime.datetime.fromtimestamp(now_ms / 1000.0)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def db_today_stats(db_path, conn=None, now_ms=None):
    """今日（本地 0 点起）全库交互来源调用的聚合：in / out / cacheRead / cacheCreation。

    「今日用量」是会话无关的全库口径——用户一天内可能跨多个会话，想知道的是
    今天总共烧了多少 token，不是某单个会话的。来源过滤同 INTERACTIVE_SOURCE_SQL
    （排除 subagent / compact / session_title 后台调用），与主显示口径一致。

    返回标准 stats dict 或 None（今日无数据 / 读取失败）：
      {inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens,
       reasoningTokens, avgDurationMs}
    conn 传入时复用（不关闭），否则自开自关。
    """
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        since = today_start_ms(now_ms)
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cache_read_input_tokens),0), "
            "COALESCE(SUM(cache_creation_input_tokens),0), "
            "COALESCE(SUM(reasoning_tokens),0), "
            "COALESCE(AVG(duration_ms),0) "
            "FROM model_usage WHERE status='completed' "
            "AND started_at >= ? "
            + INTERACTIVE_SOURCE_SQL,
            (since,),
        ).fetchone()
        if row is None:
            return None
        inp, outp, cache_rd, cache_cre, reas, avg_dur = row
        inp = int(inp or 0)
        outp = int(outp or 0)
        cache_rd = int(cache_rd or 0)
        cache_cre = int(cache_cre or 0)
        reas = int(reas or 0)
        if inp == 0 and outp == 0 and cache_rd == 0 and cache_cre == 0:
            return None
        return {
            "inputTokens": inp,
            "outputTokens": outp,
            "cacheReadTokens": cache_rd,
            "cacheCreationTokens": cache_cre,
            "reasoningTokens": reas,
            "avgDurationMs": float(avg_dur or 0.0),
        }
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_latest_model_input(db_path, session_id, conn=None):
    """该会话最近一次真实模型调用的 input_tokens（int 或 None）。

    「上下文占用率」的分子：一次调用送进模型的 prompt token 数（input_tokens 已含
    cache_read 部分），占该模型上下文窗口的比例就是「这一轮塞了多满」。取最新一条
    交互来源 completed 行（ORDER BY started_at DESC, rowid DESC LIMIT 1）。

    返回 None = 无该会话 / 无数据 / 读取失败；返回 0 是合法值（空 prompt 行）。
    conn 传入时复用（不关闭），否则自开自关。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT input_tokens FROM model_usage WHERE session_id = ? "
            "AND status='completed' "
            + INTERACTIVE_SOURCE_SQL +
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def db_recent_sessions(db_path, limit=3, conn=None):
    """最近 N 个活跃主会话速览：按各会话最新一条模型调用时间倒序取前 limit 个。

    每项 {session_id, title, last_started_at, status, last_duration_ms}：
      - title           session 表标题（无则 ''，调用方兜底显示 session_id）；
      - last_started_at 该会话最新模型调用 started_at（活跃程度排序键）；
      - status          最新一行的 status（completed/running/error/...）；
      - last_duration_ms 最新一行的 duration_ms（「本轮耗时」；running 行按
        started_at 到现在的已耗时算，没完成的行给 None）。
    subagent 会话与后台来源行（subagent/compact/session_title）不参与。
    无数据/失败返回 []。conn 传入时复用（不关闭），否则自开自关。
    """
    own = conn is None
    if own:
        conn = _db_connect(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT m.session_id, m.started_at, m.status, m.duration_ms, "
            "COALESCE(s.title, '') "
            "FROM model_usage m "
            "LEFT JOIN session s ON s.id = m.session_id "
            "JOIN (SELECT session_id, MAX(started_at) AS mx FROM model_usage "
            "      WHERE session_id LIKE 'sess_%' "
            "      AND session_id NOT LIKE 'sess_subagent_%' "
            "      AND COALESCE(query_source,'') NOT IN ('subagent','compact','session_title') "
            "      GROUP BY session_id) latest "
            "ON latest.session_id = m.session_id AND latest.mx = m.started_at "
            "WHERE COALESCE(m.query_source,'') NOT IN ('subagent','compact','session_title') "
            "GROUP BY m.session_id "
            "ORDER BY m.started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        now = time_ms()
        out = []
        for r in rows:
            status = r[2] or ""
            if status == "running":
                dur = max(0, now - int(r[1] or 0))
            elif r[3] is not None:
                dur = int(r[3])
            else:
                dur = None
            out.append({
                "session_id": r[0],
                "title": (r[4] or "").strip(),
                "last_started_at": int(r[1] or 0),
                "status": status,
                "last_duration_ms": dur,
            })
        return out
    except Exception:
        return []
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


