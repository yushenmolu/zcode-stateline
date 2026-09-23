#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_session.py — docked_statusbar 的会话判定层（Round2 Step 4 抽离）。

本模块集中「当前会话是谁」的全部判定：mark 文件读取（read_mark_raw）、
日志时间解析（_log_line_ts_ms）、resume 日志增量 tail（tail_session_resume）、
jsonl 兜底判定（current_session）、信号竞争 + 粘滞（resolve_session_sticky），
以及支撑它们的判定常量（MARK_FILE_NAME / LOG_DIR_DEFAULT / MARK_FRESH_MS /
STATUS_SIGNAL_FRESH_MS / RESUME_FRESH_MS）。
DB 查询经显式 import 取自 statusbar_db（db_recent_session_activity /
db_recent_tool_activity / _is_subagent_sid / _num / time_ms /
DB_ACTIVE_WINDOW_MS），钩子时序文件读取取自 statusbar_status
（_read_status_state / status_state_latest）；import 方向
dsb→session→db/status 无环。docked_statusbar 通过
`from statusbar_session import *` 整体 re-export，旧名（dsb.read_mark_raw、
dsb.resolve_session_sticky、dsb.current_session 等）全部保留。
"""

import datetime
import json
import os

from statusbar_db import (
    DB_ACTIVE_WINDOW_MS,
    _is_subagent_sid, _num, time_ms,
    db_recent_session_activity, db_recent_tool_activity,
)
from statusbar_status import _read_status_state, status_state_latest

__all__ = [
    # 判定常量
    "MARK_FILE_NAME", "LOG_DIR_DEFAULT",
    "MARK_FRESH_MS", "STATUS_SIGNAL_FRESH_MS", "RESUME_FRESH_MS",
    # mark / 日志解析 / resume tail（下划线私有名也经 re-export 保留旧名）
    "read_mark_raw", "_log_line_ts_ms", "tail_session_resume",
    # jsonl 兜底判定与信号竞争 + 粘滞
    "current_session", "resolve_session_sticky",
]

# ---- 会话判定路径常量（原 docked_statusbar 409/412 行；只被本模块消费）----
LOG_DIR_DEFAULT = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "log")
MARK_FILE_NAME = "current-session.json"

# ---- 信号 freshness 窗口（原 docked_statusbar 417-419 行）----
MARK_FRESH_MS = 30 * 1000  # current-session.json 标记的 freshness 窗口（30 秒）
STATUS_SIGNAL_FRESH_MS = 60 * 1000       # status-state.json 事件作为会话信号的 freshness 窗口
RESUME_FRESH_MS = 600 * 1000             # resume 信号 freshness 窗口：超此值的陈旧 resume 不入候选池


def read_mark_raw(data_dir):
    """读取 current-session.json；返回 (session_id, updated_at_ms)。
    文件不存在/坏 JSON/缺字段返回 (None, 0)。不做新鲜度判断。"""
    path = os.path.join(data_dir, MARK_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return None, 0
    sid = None
    try:
        sid = obj.get("session_id")
    except Exception:
        sid = None
    if not sid or not isinstance(sid, str):
        return None, 0
    try:
        updated = int(obj.get("updated_at") or 0)
    except Exception:
        updated = 0
    return sid, updated


def _log_line_ts_ms(obj):
    """行内时间字段（ts/time/timestamp；ISO 8601 字符串或 epoch 毫秒）-> epoch ms。
    全部解析失败返回 0（调用方兜底 time_ms()）。"""
    for key in ("ts", "time", "timestamp"):
        try:
            v = obj.get(key)
        except Exception:
            return 0
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                return int(v)
            s = str(v).strip()
            if not s:
                continue
            if s.isdigit():
                return int(s)
            # fromisoformat 在 3.11 前不认 "Z" 后缀，统一替换为 +00:00
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return 0


def tail_session_resume(state, log_dir=None):
    """增量 tail 今日 zcode-YYYY-MM-DD.jsonl，返回最新 session.resumed 的 (session_id, ts_ms)。

    state 键：log_date（str）、log_offset（int）、log_last_sid、log_last_ts。
    规则：
      - 文件名按本地日期 datetime.date.today() 生成；跨天时 offset 归零、重新打开；
      - 从 state["log_offset"] seek（字节偏移），只读新增字节；末行不完整（无 \n
        结尾）时 offset 回退到最后一个完整行尾，下次再读；
      - 逐行 json.loads，筛 event/type 字段为 "session.resumed" 的行，取 sessionId；
        行内时间字段（ts/time/timestamp，ISO 或 epoch 毫秒）解析失败则用 time_ms()；
      - 文件不存在/权限错/解析全败：返回 (None, 0)，不抛；
      - 无新增字节时返回 state 记住的上次结果（log_last_sid, log_last_ts）。

    日志行结构（2026-09-07 取样 ~/.zcode/cli/log/zcode-2026-09-07.jsonl 确认）：
      {"timestamp":"2026-09-07T00:35:50.153Z","level":"info","event":"session.resumed",
       "module":"core.runtime","message":"Session resumed","traceId":"...","spanId":"...",
       "sessionId":"sess_xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx","status":"completed",
       "context":{"appliedMessageCount":537,...}}
      即：事件名在 event 字段（非 type），时间为 ISO 8601 UTC 的 timestamp 字段，
      会话 id 在 sessionId 字段；子代理 resume 的 sessionId 以 sess_subagent_ 开头
      （tailer 单槽只记主会话：subagent resume 跳过不覆盖，判定层过滤后
      主会话切换信号不再被 subagent 堵死）。
    """
    if state is None:
        state = {}
    if log_dir is None:
        log_dir = LOG_DIR_DEFAULT
    try:
        today = datetime.date.today().isoformat()
    except Exception:
        return None, 0
    # 跨天：offset 归零，重新打开新日期文件
    if state.get("log_date") != today:
        state["log_date"] = today
        state["log_offset"] = 0
    path = os.path.join(log_dir, "zcode-%s.jsonl" % today)
    offset = state.get("log_offset") or 0
    if not isinstance(offset, int) or offset < 0:
        offset = 0
    try:
        size = os.path.getsize(path)
    except Exception:
        return None, 0
    if offset > size:
        offset = 0  # 文件被截断/轮换：从头读
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read()
    except Exception:
        return None, 0
    new_end = offset + len(raw)
    if raw and not raw.endswith(b"\n"):
        # 末行不完整：offset 回退到最后一个完整行尾，残余下次再读
        last_nl = raw.rfind(b"\n")
        if last_nl < 0:
            raw_complete = b""
            new_end = offset
        else:
            raw_complete = raw[:last_nl + 1]
            new_end = offset + last_nl + 1
    else:
        raw_complete = raw
    state["log_offset"] = new_end
    last_sid = state.get("log_last_sid")
    try:
        last_ts = int(state.get("log_last_ts") or 0)
    except Exception:
        last_ts = 0
    if raw_complete:
        try:
            text = raw_complete.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            try:
                ev = obj.get("event")
                if ev is None:
                    ev = obj.get("type")
            except Exception:
                continue
            if ev != "session.resumed":
                continue
            sid = obj.get("sessionId")
            if not sid or not isinstance(sid, str):
                continue
            ts = _log_line_ts_ms(obj)
            if not ts:
                ts = time_ms()
            # 单槽只记主会话：subagent resume 跳过不覆盖单槽（log_offset 已
            # 随读文件推进，跳过不等于停止读），主会话切换信号不被堵死
            if _is_subagent_sid(sid):
                continue
            last_sid = sid
            last_ts = ts
    state["log_last_sid"] = last_sid
    state["log_last_ts"] = last_ts
    if not last_sid:
        return None, 0
    return last_sid, last_ts


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


def resolve_session_sticky(state, rows, data_dir, db_path, conn=None,
                           cfg=None, *, read_mark=None, read_resume=None,
                           db_activity=None):
    """信号驱动 + 粘滞的当前会话判定。返回 (session_id, source)。

    依赖注入（可选，默认 None 走本模块全局，向后兼容）：read_mark /
    read_resume / db_activity 分别替换 read_mark_raw /
    tail_session_resume / db_recent_session_activity 的取数通道。
    用途：(a) docked_statusbar.resolve_gui_info 显式透传 dsb 命名空间的
    同名函数，保留「往 dsb 打补丁即生效」的旧 mock 语义（抽离前车与
    函数同模块，patch.object(dsb, ...) 能截获；抽离后函数在
    statusbar_session 解析全局名，补丁落空——注入把通道指回 dsb）；
    (b) 测试不经 mock 直接注入确定性信号。

    候选信号（各带真实发生时间戳，禁止用读取时刻伪造）：
      mark   : read_mark_raw(data_dir)      -> (sid, updated_at)；
               仅当 updated_at 在 MARK_FRESH_MS 内入池（陈旧标记不参与竞争）
      status : status_state_latest(_read_status_state(data_dir)) -> (sid, ts)；
               钩子时序文件 status-state.json 中最新一条记录的 session_id + ts
               （真实事件时刻），
               仅当 ts 在 STATUS_SIGNAL_FRESH_MS 内入池。该文件在
               UserPromptSubmit / PreToolUse / PostToolUse / PostToolUseFailure
               都会刷新 —— 等于「每次消息与每次工具调用」都刷新会话信号
      resume : tail_session_resume(state)   -> (sid, ts)；
               仅当 ts 在 RESUME_FRESH_MS 内入池（重启后 log_offset 归零会重读
               当天整个日志，陈旧 resume 不得在冷启动时抢占 sticky）
      db     : db_recent_session_activity(db_path, DB_ACTIVE_WINDOW_MS, conn)
               -> (sid, started_at)（窗口外/无行返回 (None, 0)；
                 conn 传入时复用不关闭，否则自开自关）
      tool   : db_recent_tool_activity(db_path, conn=conn) -> (sid, started_at)
               （窗口 TOOL_LIVE_MAX_MS）：起点在窗口内且**仍 running** 的最近
               一把工具（0.9.2 新增）。mark/status/db 三路同时过窗时（长工具
               运行）它是唯一仍在生效的活跃证据；无符合行 -> (None, 0)。
    state 键：sticky_sid、sticky_set_at（本函数维护）；
              tail_session_resume 另维护 log_* 键。

    切换规则：
      1. 过滤 subagent sid 后，取时间戳最新的信号（平局按
         mark>status>resume>db>tool 优先——max() 并列取先出现者）；
      2. sticky 为空（首次）：取该信号 sid；无任何信号 -> current_session(rows)
         的 jsonl 兜底（保持旧行为，source="jsonl"）；都没有 -> (None, "none")；
      3. 最新信号 sid != sticky 且信号 ts > sticky_set_at -> 切换 sticky，
         source 为信号类型；
      4. 否则保持 sticky，source="sticky"（含无任何信号的空闲轮询）。
    """
    if state is None:
        state = {}
    if read_mark is None:
        read_mark = read_mark_raw
    if read_resume is None:
        read_resume = tail_session_resume
    if db_activity is None:
        db_activity = db_recent_session_activity
    candidates = []
    try:
        sid, ts = read_mark(data_dir)
        # mark 是瞬时确认信号：超过 MARK_FRESH_MS 的陈旧标记不入池（过期
        # 由粘滞保持，沿用标记 30 秒新鲜度的既有语义）——否则陈旧
        # mark 可顶位、与 db 信号进出 60 秒窗口竞争造成切换摆动
        if (sid and ts and not _is_subagent_sid(sid)
                and (time_ms() - ts) <= MARK_FRESH_MS):
            candidates.append((ts, "mark", sid))
    except Exception:
        pass
    try:
        # status-state.json 候选（钩子时序文件，UserPromptSubmit / PreToolUse /
        # PostToolUse / PostToolUseFailure 都会刷新 -> 每次消息与每次工具调用
        # 都刷新会话信号）。用文件里的 ts（真实事件时刻）入池，不得用读取时刻。
        # 0.9.0 分片后取「最新一条」而非顶层字段；身份竞争要的就是「最近哪个
        # 会话有动静」，所以跨会话取最新，不必按 current sid 过滤。
        st_sid, st_ts = status_state_latest(_read_status_state(data_dir))
        if (st_sid and st_ts and not _is_subagent_sid(st_sid)
                and (time_ms() - float(st_ts)) <= STATUS_SIGNAL_FRESH_MS):
            candidates.append((float(st_ts), "status", st_sid))
    except Exception:
        pass
    try:
        sid, ts = read_resume(state)
        # resume 补新鲜度门槛：现状无 TTL，状态条重启后 log_offset 归零会重读
        # 当天整个日志，若最后一条 session.resumed 是数小时前的旧会话，会在
        # 冷启动时抢占 sticky（sticky_set_at=0 时任何信号都能置位）。
        if (sid and ts and not _is_subagent_sid(sid)
                and (time_ms() - float(ts)) <= RESUME_FRESH_MS):
            candidates.append((ts, "resume", sid))
    except Exception:
        pass
    try:
        sid, ts = db_activity(db_path, DB_ACTIVE_WINDOW_MS,
                              conn=conn)
        if sid and ts and not _is_subagent_sid(sid):
            candidates.append((ts, "db", sid))
    except Exception:
        pass
    try:
        # tool（0.9.2）：起点在窗口内且仍 status='running' 的 tool_usage 行 ->
        # (sid, started_at)。上面三路信号在一把长工具面前会**同时**过窗（实测
        # 154s 的 TaskOutput 期间：mark 超 30s、钩子事件超 60s、model_usage 超
        # 180s 无新行），此时它是唯一还在说「这个会话在干活」的证据。行的
        # started_at 是真实时刻，不用读取时刻伪造。
        sid, ts = db_recent_tool_activity(db_path, conn=conn)
        if sid and ts and not _is_subagent_sid(sid):
            candidates.append((ts, "tool", sid))
    except Exception:
        pass
    newest = max(candidates, key=lambda c: c[0]) if candidates else None
    sticky = state.get("sticky_sid")
    try:
        sticky_set_at = int(state.get("sticky_set_at") or 0)
    except Exception:
        sticky_set_at = 0
    if not sticky:
        if newest is not None:
            state["sticky_sid"] = newest[2]
            state["sticky_set_at"] = newest[0]
            return newest[2], newest[1]
        sid = current_session(rows)
        if sid:
            state["sticky_sid"] = sid
            state["sticky_set_at"] = 0  # jsonl 兜底无真实信号时刻，任何信号可校正
            return sid, "jsonl"
        return None, "none"
    if newest is not None and newest[2] != sticky and newest[0] > sticky_set_at:
        state["sticky_sid"] = newest[2]
        state["sticky_set_at"] = newest[0]
        return newest[2], newest[1]
    return sticky, "sticky"
