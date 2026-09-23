#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_status.py — docked_statusbar 的状态机与状态徽标段（Round2 Step 3 抽离）。

本模块集中「钩子时序 -> 状态判定 -> 去抖/速度保留 -> 徽标文案/tooltip」全链：
状态文件分片读取（_read_status_state / status_state_for / status_state_latest）、
status_detector（DB 权威收尾 + 事件新鲜窗口 + 工具证据续期）、status_debounce、
speed_hold / speed_display、status_badge_text / status_badge_tip、
resolve_turn_status（统一解析「状态 + 本轮统计」），以及支撑它们的状态常量
（STATUS_TEXT / STATUS_COLORS / STATUS_TIPS / BUSY_FAMILY / 各时间窗）与
format_elapsed_ms。docked_statusbar 通过 `from statusbar_status import *`
整体 re-export，旧名（dsb._read_status_state、dsb.status_detector 等）全部保留。
db 层（recent_turn_stats / live_turn_stats / tool_live_ms 等）在 statusbar_db。
渲染层的颜色/字体常量（ACCENT_GREEN / ACCENT_BLUE / FG_DIM / FONT_MAIN /
FONT_DIM）是界面显示值，与 docked_statusbar 同源同步（值一致即可，零行为
差异）；它们不属于本模块的状态机语义，故不列入 __all__。
"""

import json
import os
import time

from statusbar_db import (
    ACTIVITY_GRACE_MS, TOOL_LIVE_MAX_MS,
    time_ms,
    recent_turn_stats, turn_window_left_edge, live_turn_stats,
    db_turn_request_profile, db_session_model_activity_ts,
    db_tool_activity, tool_live_ms, db_latest_speed,
)

# ---- 渲染显示常量（与 docked_statusbar 同源，值保持一致；不列入 __all__）----
# 徽标/速度段只用到这几个，值与 docked_statusbar 顶部主题常量完全相同；
# 抽离后由本模块自包含，docked_statusbar 经 re-export 拿到的是同一份值。
ACCENT_GREEN = "#3fb68b"   # 命中率 / 省钱（绿）
ACCENT_BLUE = "#4f9cf7"    # 强调色（蓝）
FG_DIM = "#9aa1aa"         # 次要文字 / 标签
FONT_MAIN = ("Microsoft YaHei UI", 10)   # 指标块中文标签
FONT_DIM = ("Microsoft YaHei UI", 9)     # 小字

__all__ = [
    # 状态常量（含时间窗）
    "STATUS_STATE_NAME", "STATUS_IDLE_AFTER_MS", "STATUS_IDLE_AFTER_MS_DEFAULT",
    "GENERATING_MAX_LIFETIME_MS", "BUSY_FAMILY", "STATUS_DWELL_MS",
    "TURN_END_TIE_MS", "OUTCOME_HOLD_MS", "SPEED_HOLD_MS",
    # 徽标文案 / 颜色 / tooltip 常量
    "STATUS_TEXT", "STATUS_COLORS", "STATUS_TIPS", "BADGE_TOOL_NAME_MAX",
    # 状态文件读取与分片视图（下划线私有名也经 re-export 保留旧名）
    "_read_status_state", "_status_slots",
    "status_state_for", "status_state_latest", "status_turn_started_at",
    # 状态判定 / 去抖 / 速度
    "status_detector", "status_debounce", "speed_hold", "speed_display",
    # 徽标文案 / tooltip 与统一解析
    "format_elapsed_ms", "status_badge_text", "status_badge_tip",
    "resolve_turn_status",
]

# ---- 状态时间窗常量（原 docked_statusbar 408-426 行）----
STATUS_STATE_NAME = "status-state.json" # 钩子时序状态文件（status_event.py 原子写）
STATUS_IDLE_AFTER_MS = {            # 钩子状态新鲜窗口（按事件类型区分）
    "generating": 120 * 1000,       # 生成中：单次生成通常远短于此（秒级~分钟级）
    "tool": 900 * 1000,             # 工具中：工具可能长时间运行（构建/测试套件）
}
STATUS_IDLE_AFTER_MS_DEFAULT = 60 * 1000   # 未知事件 / 时间戳解析失败时的兜底窗口
GENERATING_MAX_LIFETIME_MS = 600 * 1000  # generating 状态自事件时刻起最长存活；
                                         # 心跳续期不得突破此上限（单次模型调用
                                         # 实测 p99=422s，600s 已覆盖绝大概率）
BUSY_FAMILY = ("generating", "tool")  # 「正在干活」家族：族内切换可去抖，进出族立即生效
STATUS_DWELL_MS = 2000                # busy 族内最小驻留：一轮内 PreToolUse/PostToolUse
                                      # 交替写事件（实测单轮 model_request_count p95=67
                                      # 即 130+ 次 tool<->generating 翻转），逐次翻转会
                                      # 让徽标高频抖动；族内切换需驻留满此值才生效。
TURN_END_TIE_MS = 3000                # turn 行收尾与最后事件的时间归并容差：落库时刻与
                                      # 钩子写入时刻有几秒偏差，落在容差内视为「同时发生」
OUTCOME_HOLD_MS = 20 * 1000           # 上一轮 error/cancelled 结论的展示时长（自轮次
                                      # 收尾起算），到点后回落 idle，不再无限期挂红徽标
SPEED_HOLD_MS = 60 * 1000             # 轮次收尾后「最后已知速度」的保留时长：
                                      # 再短用户来不及看（数字随徽标转空闲当帧消失），
                                      # 再长就是在读几分钟前的旧值。标注「（上次）」
                                      # 说清它不是本轮的实时速度。

# ---- 状态徽标（0.4.0 对话级状态显示：第一行 状态徽标 + 对话名）----
STATUS_TEXT = {
    "idle": u"\u7a7a\u95f2",                        # 空闲
    "generating": u"\u26a1\u751f\u6210\u4e2d",      # ⚡生成中
    "tool": u"\U0001f527\u5de5\u5177\u4e2d",        # 🔧工具中
    "error": u"\u51fa\u9519",                       # 出错
    "cancelled": u"\u5df2\u4e2d\u65ad",             # 已中断
}
STATUS_COLORS = {
    "idle": "#7d8590",          # 灰
    "generating": ACCENT_GREEN,  # 绿
    "tool": ACCENT_BLUE,        # 蓝
    "error": "#e5534b",         # 红
    "cancelled": "#8b93a1",     # 灰蓝（中断既非成功也非失败，不配红）
}
STATUS_TIPS = {
    "idle": u"\u7a7a\u95f2\uff1a\u5f53\u524d\u65e0\u6a21\u578b\u6d3b\u52a8",
    "generating": (u"\u751f\u6210\u4e2d\uff1a\u672c\u8f6e\u6b63\u5728\u8fdb\u884c"
                   u"\uff0c\u901f\u5ea6\u4e3a\u672c\u8f6e\u5df2\u5b8c\u6210\u6a21\u578b"
                   u"\u8c03\u7528\u7684\u7cbe\u786e tok/s"),
                   # 生成中：本轮正在进行，速度为本轮已完成模型调用的精确 tok/s
    "tool": (u"\u5de5\u5177\u4e2d\uff1a\u6700\u8fd1\u4e00\u6b21\u6a21\u578b\u8c03\u7528\u542b"
             u"\u5de5\u5177\u8c03\u7528\uff0c\u6216\u5f53\u524d\u8f6e\u672a\u5b8c\u6210"),
             # 工具中：最近一次模型调用含工具调用，或当前轮未完成
    "error": u"\u51fa\u9519\uff1a\u6700\u8fd1\u4e00\u6b21\u6a21\u578b\u8c03\u7528\u4ee5\u9519\u8bef\u7ed3\u675f",
    "cancelled": (u"\u5df2\u4e2d\u65ad\uff1a\u672c\u8f6e\u88ab\u53d6\u6d88"
                  u"\uff08\u7528\u6237\u6253\u65ad / \u4f1a\u8bdd\u5173\u95ed\uff09"),
                  # 已中断：本轮被取消（用户打断 / 会话关闭）
}


def _read_status_state(data_dir):
    """读 ZCode 钩子时序状态文件 status-state.json（status_event.py 原子写）
    的**原始文档**；不存在 / 坏 JSON 返回 None（绝不抛）。

    0.9.0 起文件是按会话分片的文档（v2），需经 status_state_for /
    status_state_latest 取视图；本函数只做「读文件 + 解析」，一拍一次。
    """
    try:
        with open(os.path.join(data_dir, STATUS_STATE_NAME), "r",
                  encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _status_slots(doc):
    """把状态文档归一化为 {session_id: record}。

    v2：直接取 sessions。v1（全局单条 {"event","ts","session_id"}）：放进它
    session_id 对应的单个槽位（老文件无需迁移即可读；无 sid 的落在 "" 槽）。
    """
    if not isinstance(doc, dict):
        return {}
    sessions = doc.get("sessions")
    if isinstance(sessions, dict):
        return dict((k, v) for k, v in sessions.items()
                    if isinstance(v, dict) and v.get("event"))
    if doc.get("event"):
        return {doc.get("session_id") or "": dict(doc)}
    return {}


def status_state_for(doc, session_id):
    """取**指定会话**的钩子事件记录（无则 None）。

    分片后每会话各读各的，从根上不发生跨会话互读；v1 单条文件仍可能是别人
    会话留下的记录，故沿用 0.7.1 的读端校验（sid 明确不符 -> 不采信），避免
    升级窗口内旧格式继续粘附。
    """
    rec = _status_slots(doc).get(session_id or "")
    if not rec:
        # 无 sid 的记录（stdin 与 ${CLAUDE_SESSION_ID} 双双取空时才产生）落在
        # "" 槽：按「任一侧为空即采信」的既有兼容语义取它，不误伤老数据。
        rec = _status_slots(doc).get("") if session_id else None
    if not rec:
        return None
    out = dict(rec)
    if not out.get("session_id") and session_id:
        out["session_id"] = session_id
    return out


def status_state_latest(doc):
    """取文档中**最新一条**记录的 (session_id, ts)（供会话身份竞争）。

    无任何记录 -> (None, 0)。ts 非数字的记录跳过（不因脏数据抛）。
    """
    best_sid, best_ts = None, 0
    for sid, rec in _status_slots(doc).items():
        try:
            ts = float(rec.get("ts") or 0)
        except Exception:
            continue
        if ts > best_ts:
            best_sid, best_ts = (sid or None), ts
    return best_sid, int(best_ts)


def status_turn_started_at(status_state):
    """本轮起点（epoch 毫秒）：仅 UserPromptSubmit 置位，工具事件不覆盖。
    缺失 / 非数字 -> None（老数据无此字段）。"""
    if not isinstance(status_state, dict):
        return None
    try:
        v = int(status_state.get("turn_started_at") or 0)
    except Exception:
        return None
    return v or None



def status_detector(status_state, mu_row, tu_row, now=None, current_sid=None,
                    activity_ts=None, turn_started_at=None, tool_row=None):
    """状态判定（0.9.0；纯函数，可独立单测）。返回 (status, speed_tok_per_s)：
      status ∈ {'generating', 'tool', 'idle', 'error', 'cancelled'}；
      speed 恒 None（生成中时由调用方以 db_latest_speed 填充精确速度）。

      判定顺序（**先权威后推断**）：

      A. DB 轮次收尾（权威）。turn_usage 行只在轮次结束时落库（实测本机 3741
         行里 completed_at 为 NULL 的行数为 0），所以「该会话最新 turn 行的
         completed_at 不早于最后一条钩子事件」就是**本轮已结束**的确凿证据。
         据此直接给结论，不再等钩子事件老化：
           - status='error'    且收尾在 OUTCOME_HOLD_MS 内 -> 'error'（红）；
           - status='cancelled'且收尾在 OUTCOME_HOLD_MS 内 -> 'cancelled'（灰）；
           - 其余（completed）-> 'idle'。
         为什么必须引入这一步：Stop 钩子是唯一写 idle 的路径，而平台钩子全集
         只有 7 种、没有 SessionEnd/中断钩子——用户打断、请求报错、窗口关闭时
         Stop 常常不触发（实测现场即有一条 generating 事件晚于该会话最后一个
         turn 收尾 11 分钟且始终没有 idle）。只靠老化就要白挂 120s 假「生成中」。
         例外：最后一条事件是本轮的 UserPromptSubmit（turn_started_at >= 上一轮
         completed_at）——那是**新一轮刚开始**，不能被判成上一轮的收尾。工具行
         同理：收尾之后又开了一把仍未结束的工具，也是新一轮已在跑（0.9.2）。
      B. 事件新鲜窗口（推断）：generating <120s、tool <900s（未知事件 60s
         兜底）-> 对应徽标；超窗后再走 C / 心跳续期。
      C. 工具证据续期（0.9.2，权威）：本会话仍有未收尾的 tool_usage 行，且其
         起点不早于最后一条钩子事件（容差 TURN_END_TIE_MS）、持续时长在
         TOOL_LIVE_MAX_MS 内 -> 'tool'。长工具（实测 154s 起）期间钩子与
         model_usage 都无新动静，这一路是「工具中」唯一的实时依据。
      D. 否则 'idle'。

      activity_ts：**该会话在本事件之后最近一次真实模型调用的完成时刻**
      （epoch 毫秒，由 db_session_model_activity_ts 提供，已排除
      subagent / compact / session_title 后台调用）。0.8.0 曾取
      session.time_updated 作心跳，而该字段由后台任务刷新，会把早已结束的
      轮次续成「生成中」；换成模型调用完成后，心跳只在真有模型产出时生效。
      续期仍受 GENERATING_MAX_LIFETIME_MS（600s，自事件时刻起算）硬上限约束。
      None -> 不续期。

      turn_started_at：本轮起点，用于上面的 UserPromptSubmit 例外判定；None 时
      先尝试从 status_state 的 turn_started_at 字段自取（记录本身就带），仍取不
      到才按「本轮起点未知」处理——不让上一轮的收尾压掉刚开始的新轮次。

      tool_row（0.9.2）：本会话最近一把工具的 tool_usage 行（db_tool_activity）。
      工具在**开始**时就落行、结束时才补 completed_at，所以「仍在 running」是
      「此刻确有一把工具在跑」的权威证据，用于两处：步骤 A 里「收尾之后又开了
      一把工具」= 新一轮已开始（与 UserPromptSubmit 例外同义，但不依赖钩子没漏
      写）；步骤 C 里让「工具中」不再靠 900 秒硬窗口猜。None 时两路都不生效，
      行为与 0.9.1 一致。

      会话匹配（0.7.1 读端兜底）：记录 sid 与 current_sid 均存在且不等 ->
      ('idle', None)。0.9.0 起 status-state.json 已按会话分片、各读各的，本
      校验只在 v1 老文件与 "" 槽回退路径上生效。任一侧为空 -> 采信。

      mu_row 参数保留（历史签名兼容），不参与判定；tu_row 即 turn_usage 行
      （recent_turn_stats 的 dict），是步骤 A 的输入。
    """
    if now is None:
        now = time.time()
    if not isinstance(status_state, dict):
        return "idle", None
    event = status_state.get("event")
    ts = status_state.get("ts")
    if not (event and ts):
        return "idle", None
    rec_sid = status_state.get("session_id")
    if (rec_sid and current_sid and str(rec_sid) != str(current_sid)):
        return "idle", None
    try:
        now_ms = now * 1000.0
        event_ts = float(ts)
    except Exception:
        return "idle", None
    elapsed_ms = now_ms - event_ts

    # ---- A. DB 轮次收尾：权威结论优先于事件推断 ----
    if turn_started_at is None:
        # 调用方未显式传时从记录自取：漏接线会把「新一轮刚开始」误判成
        # 上一轮的收尾（表现为发完消息仍显示空闲）
        turn_started_at = status_turn_started_at(status_state)
    closed_at = None
    if isinstance(tu_row, dict):
        try:
            closed_at = tu_row.get("completedAt")
            closed_at = float(closed_at) if closed_at else None
        except Exception:
            closed_at = None
    if closed_at is not None and closed_at >= event_ts - TURN_END_TIE_MS:
        is_new_turn_start = (
            status_state.get("hook") == "UserPromptSubmit"
            and (turn_started_at is None or turn_started_at >= closed_at))
        if not is_new_turn_start:
            # 收尾之后又开了一把仍未结束的工具 = 新一轮已在跑（钩子漏写
            # UserPromptSubmit 时的第二路证据）
            tool_live = tool_live_ms(tool_row, now_ms)
            if tool_live is not None:
                try:
                    is_new_turn_start = float(tool_row.get("startedAt")) >= closed_at
                except Exception:
                    is_new_turn_start = False
        if not is_new_turn_start:
            hold_ms = now_ms - closed_at
            turn_status = tu_row.get("status")
            if 0 <= hold_ms < OUTCOME_HOLD_MS:
                if turn_status == "error":
                    return "error", None
                if turn_status == "cancelled":
                    return "cancelled", None
            return "idle", None

    # ---- B. 事件新鲜窗口 ----
    window = STATUS_IDLE_AFTER_MS.get(event, STATUS_IDLE_AFTER_MS_DEFAULT)
    if elapsed_ms < window:
        if event == "generating":
            return "generating", None
        if event == "tool":
            return "tool", None
    # ---- C. 工具证据续期（0.9.2）：钩子事件超窗，但该会话仍有一把工具没收尾
    # （一把 TaskOutput 实测能跑 154s+，其间 model_usage 无新行、钩子文件无新
    # 事件，B 与心跳都救不了）。要求工具起点不早于最后一条事件（容差同 A），
    # 否则是上一轮遗留的行在替本轮说话。
    tool_live = tool_live_ms(tool_row, now_ms)
    if tool_live is not None:
        try:
            tool_newer = float(tool_row.get("startedAt") or 0) >= (
                event_ts - TURN_END_TIE_MS)
        except Exception:
            tool_newer = False
        if tool_newer:
            return "tool", None
    # 长生成心跳续期：generating 事件虽已超窗，但该会话在事件之后确有模型
    # 调用完成 -> 维持「生成中」，不中途掉「空闲」。续期不自事件起超过
    # GENERATING_MAX_LIFETIME_MS（600s）——再长的静默按已结束处理。
    if (event == "generating" and activity_ts is not None
            and elapsed_ms < GENERATING_MAX_LIFETIME_MS):
        try:
            idle_ms = now_ms - float(activity_ts)
        except Exception:
            idle_ms = ACTIVITY_GRACE_MS
        if 0 <= idle_ms < ACTIVITY_GRACE_MS:
            return "generating", None
    return "idle", None


def status_debounce(state, new_status, now_ms=None):
    """busy 族内切换去抖（0.9.0）。返回本拍实际展示的徽标状态。

    一轮内 PreToolUse -> tool 与 PostToolUse -> generating 交替写事件，实测
    单轮 model_request_count p95=67（即 130+ 次 tool<->generating 翻转），
    叠加事件驱动刷新（响应上限 ~150ms）后徽标在蓝/绿间高频抖动。

    取舍：只对 **generating <-> tool** 这一对同义「正在干活」状态设最小驻留
    （STATUS_DWELL_MS）；进出 busy 族（-> idle / error / cancelled 及反向）
    一律立即生效——「停了要立刻显停」优先级高于防抖，去抖绝不能延迟收尾态。

    state 键：badge_shown（当前展示态）、badge_shown_at（该态起算时刻）。
    state 为 None / 空 dict 时等同首次调用，直接采纳 new_status。
    """
    if state is None:
        return new_status
    if now_ms is None:
        now_ms = time_ms()
    shown = state.get("badge_shown")
    if new_status == shown:
        return shown          # 同态重复到来：不动驻留起算时刻（否则事件密集时
                              # 永远驻留不满，去抖形同虚设）
    if not shown:
        state["badge_shown"] = new_status
        state["badge_shown_at"] = now_ms
        return new_status
    in_family = shown in BUSY_FAMILY and new_status in BUSY_FAMILY
    if in_family:
        try:
            held = now_ms - int(state.get("badge_shown_at") or 0)
        except Exception:
            held = STATUS_DWELL_MS
        if held < STATUS_DWELL_MS:
            return shown  # 驻留未满：保持旧态，且不重置起算时刻
    state["badge_shown"] = new_status
    state["badge_shown_at"] = now_ms
    return new_status


def speed_hold(state, status, speed, session_id, now_ms=None):
    """轮次收尾后短时保留「最后已知速度」（0.9.1；纯函数，可独立单测）。

    返回 (display_speed, held)：
      - speed 非 None（busy 族且本轮已有完成的调用）-> 记入 state 并原样显示；
      - busy 族但 speed 为 None -> (None, False)：本轮还没有任何可用速度，
        绝不拿上一轮的值冒充（批 4 立的口径），由渲染层显示「生成中…」；
      - 非 busy（轮次刚收尾）-> SPEED_HOLD_MS 内返回 (last, True)，超时清值。

    为什么需要这一步：model_usage 与 turn_usage 都只在调用结束时落库，轮次一
    收尾就没有任何「当前」速度来源，而徽标同帧转空闲、数字同帧消失——用户
    来不及看，并把它读作「速度功能没生效」。保留最后已知值并明确标「上次」是
    诚实的妥协：值真实（本轮最后一次调用），只是不再是「正在跑」的速度。

    state 键：speed_last / speed_last_at / speed_last_sid。会话变化时缓存作废
    （别的会话的速度不是本会话的速度，与徽标换会话清驻留同一逻辑）。
    """
    if now_ms is None:
        now_ms = time_ms()
    if speed is not None:
        if state is not None:
            state["speed_last"] = speed
            state["speed_last_at"] = now_ms
            state["speed_last_sid"] = session_id
        return speed, False
    if state is None:
        return None, False
    last = state.get("speed_last")
    if last is None:
        return None, False
    if state.get("speed_last_sid") != session_id:
        state["speed_last"] = None
        return None, False
    if status in BUSY_FAMILY:
        return None, False          # 本轮尚无速度：宁可空着，也不显上一轮的值
    try:
        if now_ms - int(state.get("speed_last_at") or 0) > SPEED_HOLD_MS:
            state["speed_last"] = None   # 过期即清，不留悬空值给下个轮次窗口
            return None, False
    except Exception:
        state["speed_last"] = None
        return None, False
    return last, True


def speed_display(status, speed, held=False, show_live=True, show_speed=True):
    """第二行本轮统计尾部的速度段（0.9.1；纯数据，渲染层直接追加）。
    返回 [(text, font, color), ...]；无内容返回 []。

    0.4.3 起这一段只在 status=='generating' 时画，于是占了一轮大半时间的
    「工具中」和轮次收尾后的短暂窗口都没有速度——0.9.1 扩到整个 busy 族，并
    在收尾后按 speed_hold 的保留值标注「（上次）」显示。
    show_speed 只关掉数值段；「生成中…」占位归 show_live 管（它是状态可见性
    兜底，不是速度读数）。
    """
    if not show_live:
        return []
    if speed is not None:
        if not show_speed:
            return []
        segs = [(u" · \u26a1", FONT_MAIN, FG_DIM),
                (u"%.1f tok/s" % speed, FONT_DIM, FG_DIM)]
        if held:
            segs.append((u"\uff08\u4e0a\u6b21\uff09", FONT_MAIN, FG_DIM))
        return segs
    if status == "generating":
        # 首 token 前没有任何速度来源，占位文案保证「正在生成」这件事看得见
        return [(u" · \u751f\u6210\u4e2d\u2026", FONT_MAIN, ACCENT_GREEN)]
    return []


BADGE_TOOL_NAME_MAX = 12   # 徽标内联工具名的长度上限（徽标槽永不裁剪，见布局
                           # 注释；超长就退回纯状态词，别把对话名挤没）


def format_elapsed_ms(ms):
    """时长口语化：<60s「34 秒」，<1h「2 分 34 秒」，再长「1 时 5 分」。"""
    try:
        s = int(ms) // 1000
    except Exception:
        return u""
    if s < 60:
        return u"%d \u79d2" % s
    if s < 3600:
        return u"%d \u5206 %d \u79d2" % (s // 60, s % 60)
    return u"%d \u65f6 %d \u5206" % (s // 3600, (s % 3600) // 60)


def status_badge_text(status, turn_stats):
    """徽标文案：「工具中」内联正在跑的工具名（`🔧工具中·Bash`）。

    0.9.1 及之前徽标只会说「工具中」，一轮里等三分钟也看不出在等哪把工具；
    tool_usage 行自带 tool_name，故顺手带出来。取不到名字 / 超长时退回纯状态词。
    """
    txt = STATUS_TEXT.get(status, STATUS_TEXT["idle"])
    if status != "tool" or not isinstance(turn_stats, dict):
        return txt
    at = turn_stats.get("activeTool")
    name = ((at or {}).get("name") or "").strip() if isinstance(at, dict) else ""
    if name and len(name) <= BADGE_TOOL_NAME_MAX:
        return txt + u"\u00b7" + name
    return txt


def status_badge_tip(status, turn_stats):
    """徽标 tooltip：静态口径 + 本轮的确凿证据（0.9.2）。

    0.9.1 及之前只有 STATUS_TIPS 的固定文案：徽标说「出错」却不说为什么，
    而 turn_usage 的 error_type / cancelled_by_user / tool_error_count 就在
    同一行里，只是没露出来。
    """
    tip = STATUS_TIPS.get(status, STATUS_TIPS["idle"])
    ts = turn_stats if isinstance(turn_stats, dict) else {}
    extra = []
    if status == "tool":
        at = ts.get("activeTool")
        if isinstance(at, dict) and at.get("name"):
            extra.append(u"\u5f53\u524d\u5de5\u5177\uff1a%s\uff0c\u5df2\u8dd1 %s"
                         % (at["name"],
                            format_elapsed_ms(at.get("runningMs") or 0)))
    elif status == "error":
        if ts.get("errorType"):
            extra.append(u"\u539f\u56e0\uff1a%s" % ts["errorType"])
        if ts.get("contextExceeded"):
            extra.append(u"\u4e0a\u4e0b\u6587\u5df2\u6ea2\u51fa")
    elif status == "cancelled":
        cb = ts.get("cancelledByUser")
        if cb == 1:
            extra.append(u"\u7531\u4f60\u624b\u52a8\u6253\u65ad")
        elif cb == 0:
            extra.append(u"\u975e\u7528\u6237\u53d6\u6d88\uff08\u4f1a\u8bdd\u5173"
                         u"\u95ed / \u8d85\u65f6 / \u5bbf\u4e3b\u4e2d\u6b62\uff09")
    errs = int(ts.get("toolErrorCount") or 0)
    if errs and status in ("tool", "error", "cancelled"):
        extra.append(u"\u672c\u8f6e\u5de5\u5177\u62a5\u9519 %d \u6b21" % errs)
    if extra:
        tip = tip + u"\n" + u"\n".join(extra)
    return tip


def resolve_turn_status(status_state, db_path, session_id, conn=None):
    """0.9.0 统一解析「状态 + 本轮统计」（GUI 每帧与 --once 共用）。
    返回 (status, speed_tok_per_s, turn_stats)：
      - status 走 status_detector（钩子事件 + DB 轮次收尾，见其文档）；
      - speed 仅 busy 族（generating / tool）非 None：db_latest_speed 取**本轮
        窗口内**最近一次已完成模型调用的精确 tok/s（排除后台来源）；本轮还没
        有完成的调用时返回 None，由渲染层显示「生成中…」占位——不再拿上一轮
        的速度冒充本轮。非 busy 恒 None（收尾后短时留值是 GUI 层的 speed_hold，
        --once 单帧无历史故不参与）。
      - turn_stats 三态，用标记位区分，渲染层据此加提示词：
          * live=True      本轮进行中的实时聚合（live_turn_stats）
          * stale_turn=True 该 turn 行不属于本轮（行收尾时刻早于本轮起点，即
                            本轮尚未落库任何行）——展示的是上一轮
          * 无标记          权威的本轮已完成行
      - busy 且该会话确有一把工具仍在跑时，turn_stats 另带
        activeTool={"name","runningMs"}（0.9.2）：徽标文案与 tooltip 用它说清
        「在等哪把工具、已经等了多久」。
      - turn_stats 另带请求级计数 modelCallCount / coldReadCount（0.9.4）：live 那
        一路取自 live_turn_stats 的同一条聚合，turn_usage 两路（权威行 / 上一轮）
        由 db_turn_request_profile 按该轮时间窗补齐；取不到时两个键都不出现，
        本轮 tooltip 相应少一句（不显示 0 冒充「没有冷读」）。
    本轮起点 turn_started_at 取自 status_state（写端在 UserPromptSubmit 置位），
    只用于 stale 判定；「本轮窗口」的左沿改用 DB 时钟（turn_window_left_edge），
    因为钩子落盘比真实轮次起点晚十几秒且滞后量随负载变化。
    会话未识别（None）时返回 ('idle', None, None)，不猜。
    conn 透传给 db 查询（复用不关闭）；None 时各 helper 自开自关。
    """
    if not session_id:
        return "idle", None, None
    tu_row = recent_turn_stats(db_path, session_id, conn=conn)
    turn_started_at = status_turn_started_at(status_state)
    window_left = turn_window_left_edge(tu_row)
    try:
        event_ts = float(status_state.get("ts")) if isinstance(
            status_state, dict) and status_state.get("ts") else None
    except Exception:
        event_ts = None
    # 第 2 参（历史 mu_row 位）恒 None：status_detector 不消费该参数
    activity_ts = db_session_model_activity_ts(db_path, session_id,
                                              since_ms=event_ts, conn=conn)
    tool_row = db_tool_activity(db_path, session_id, conn=conn)
    status, spd = status_detector(status_state, None, tu_row,
                                  current_sid=session_id,
                                  activity_ts=activity_ts,
                                  turn_started_at=turn_started_at,
                                  tool_row=tool_row)

    def _belongs_to_this_turn(row):
        """该行是否出自本轮（无本轮起点时不妄判，视为属于本轮）。

        比**收尾时刻**：0.9.0 比的是行起点，而钩子落盘的 turn_started_at 本身
        就晚于真实轮次起点（实测 17.0s），于是每一轮的正常收尾行都被判成上一轮。
        残留边界：本轮的 turn 行由其最后一次调用落库，「最后一笔调用早于
        turn_started_at」的极短轮次仍会误标；进行中那一路不受影响。
        """
        if not row or not turn_started_at:
            return True
        try:
            return int(row.get("completedAt") or 0) >= turn_started_at
        except Exception:
            return True

    turn_stats = tu_row
    if status in BUSY_FAMILY:
        # 进行中：turn_usage 只在轮次结束时落库，行本身必然是上一轮的，
        # 故优先改用 model_usage 实时聚合（左沿 = 上一轮收尾时刻）。
        live = live_turn_stats(db_path, session_id, window_left, conn=conn)
        if live is not None:
            turn_stats = live
        elif tu_row is not None:
            turn_stats = dict(tu_row)
            turn_stats["stale_turn"] = True
    elif tu_row is not None and not _belongs_to_this_turn(tu_row):
        # 已结束（含 Stop 漏写后老化回落）：但该行收尾早于本轮起点
        # -> 本轮没留下任何 turn 行（中断在首次模型调用之前），明说是上一轮。
        turn_stats = dict(tu_row)
        turn_stats["stale_turn"] = True

    if status in BUSY_FAMILY:
        # 速度按当前会话 + 本轮窗口过滤：避免拿别的会话或上一轮的速度。
        # 0.9.1 起「工具中」也取（一轮里工具时间常占大半，只在生成中取会让
        # 数字大段消失，读起来像没生效）。窗口左沿同样改用 DB 时钟。
        spd = db_latest_speed(db_path, session_id, since_ms=window_left,
                             conn=conn)
    # 请求级画像（0.9.4）：turn_usage 那两路（权威收尾行 / 标注「上一轮」的旧行）
    # 没有逐次调用的 cache_read 分布，按该轮自己的时间窗回 model_usage 数一遍。
    # 用 turn_stats 而非 tu_row 的窗口：句子里的 n/k 必须解释**眼前这串数字**，
    # 不能解释别的轮次。
    if turn_stats is not None and not turn_stats.get("live"):
        prof = db_turn_request_profile(db_path, session_id,
                                       turn_stats.get("startedAt"),
                                       turn_stats.get("completedAt"), conn=conn)
        if prof is not None:
            turn_stats = dict(turn_stats)
            turn_stats["modelCallCount"], turn_stats["coldReadCount"] = prof
    # 正在跑的那把工具（名字 + 已持续时长）交给渲染层：徽标 tooltip 要说清
    # 「工具中 · Bash，已跑 2 分 34 秒」，否则用户只知道在等、不知道在等什么。
    # 只在确实仍在跑时挂（tool_live_ms 不满足即 None），遗留行不算本轮工具。
    tool_live = tool_live_ms(tool_row)
    if status in BUSY_FAMILY and tool_live is not None and turn_stats is not None:
        turn_stats = dict(turn_stats)
        turn_stats["activeTool"] = {"name": tool_row.get("toolName") or "",
                                    "runningMs": int(tool_live)}
    return status, spd, turn_stats

