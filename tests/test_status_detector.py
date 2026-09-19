# -*- coding: utf-8 -*-
"""状态徽标跨会话粘附修复：status_detector 的会话匹配测试。

根因：status-state.json 记录携带 session_id，但读端从不比对——切到
非活跃会话 B 时仍显示上一会话 A 的「工具中」，60s 后才回落 idle。

修复：status_detector 增加可选 current_sid；记录 sid 与 current_sid
均存在且不相等 -> 直接 ('idle', None)。两侧任一为空 -> 采信（兼容）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


def _tool_state():
    """新鲜的 tool 事件（10s 前，< STATUS_IDLE_AFTER_MS 60s）。"""
    return {"event": "tool", "ts": dsb.time_ms() - 10 * 1000,
            "session_id": "sess_a"}


class TestStatusDetectorSessionMatch(unittest.TestCase):
    """跨会话粘附消除：读端用 sticky 会话 id 比对 status-state 记录 sid。"""

    def test_status_detector_session_mismatch_yields_idle(self):
        """记录属 sess_a、当前会话 sess_b -> 不采信，直接 idle。"""
        self.assertEqual(
            dsb.status_detector(_tool_state(), None, None,
                                current_sid="sess_b"),
            ("idle", None))

    def test_status_detector_session_match_keeps_tool(self):
        """记录属 sess_a、当前会话 sess_a -> 正常显示 tool。"""
        self.assertEqual(
            dsb.status_detector(_tool_state(), None, None,
                                current_sid="sess_a"),
            ("tool", None))

    def test_status_detector_null_session_keeps_compat(self):
        """记录无 session_id（null 兼容）-> 采信，不误伤。"""
        state = _tool_state()
        state["session_id"] = None
        self.assertEqual(
            dsb.status_detector(state, None, None, current_sid="sess_b"),
            ("tool", None))

    def test_status_detector_no_current_sid_keeps_compat(self):
        """调用方无会话上下文（current_sid=None）-> 旧行为。"""
        self.assertEqual(
            dsb.status_detector(_tool_state(), None, None, current_sid=None),
            ("tool", None))


def _state(event, age_s, sid="sess_a"):
    """构造 status_state：age_s 秒前的指定事件（session_id 与 sid 一致，
    避免触发跨会话匹配分支）。"""
    return {"event": event, "ts": dsb.time_ms() - age_s * 1000,
            "session_id": sid}


class TestStatusDetectorWindows(unittest.TestCase):
    """按事件类型的新鲜窗口 + 生成中心跳续期 + 未知事件兜底。

    窗口契约（STATUS_IDLE_AFTER_MS）：generating=120s、tool=900s，
    未知事件走 STATUS_IDLE_AFTER_MS_DEFAULT=60s；
    心跳续期（ACTIVITY_GRACE_MS=90s）：generating 事件超窗但
    activity_ts 新鲜 -> 维持 generating；
    有界续期（GENERATING_MAX_LIFETIME_MS=600s）：事件超 600s 后
    心跳再新鲜也不再续期（防后台写 time_updated 无限续期）。
    """

    def test_generating_kept_by_activity_heartbeat(self):
        """generating 事件 200s 前（超 120s 窗）+ activity_ts 30s 前
        （< 90s 宽限）-> 心跳续期，维持 generating。"""
        self.assertEqual(
            dsb.status_detector(_state("generating", 200), None, None,
                                activity_ts=dsb.time_ms() - 30 * 1000),
            ("generating", None))

    def test_generating_expires_without_activity(self):
        """generating 事件 200s 前 + 无 activity_ts -> 超窗回落 idle。"""
        self.assertEqual(
            dsb.status_detector(_state("generating", 200), None, None,
                                activity_ts=None),
            ("idle", None))

    def test_generating_kept_by_heartbeat_within_max_lifetime(self):
        """上限内正常续期：generating 事件 300s 前（超 120s 窗、< 600s
        上限）+ activity_ts 30s 前 -> 维持 generating（模拟长生成中
        流式写入持续落库）。"""
        self.assertEqual(
            dsb.status_detector(_state("generating", 300), None, None,
                                activity_ts=dsb.time_ms() - 30 * 1000),
            ("generating", None))

    def test_generating_not_renewed_beyond_max_lifetime(self):
        """有界续期核心用例：generating 事件 700s 前（> 600s 上限）+
        activity_ts 10s 前（心跳再新鲜）-> 不再续期，回落 idle。"""
        self.assertEqual(
            dsb.status_detector(_state("generating", 700), None, None,
                                activity_ts=dsb.time_ms() - 10 * 1000),
            ("idle", None))

    def test_generating_beyond_max_lifetime_without_activity(self):
        """对照：generating 事件 700s 前 + 无 activity_ts -> idle
        （超上限且无心跳，两条路径都指向 idle）。"""
        self.assertEqual(
            dsb.status_detector(_state("generating", 700), None, None,
                                activity_ts=None),
            ("idle", None))

    def test_tool_within_900s_window(self):
        """tool 事件 300s 前（< 900s 窗）-> 仍为 tool。"""
        self.assertEqual(
            dsb.status_detector(_state("tool", 300), None, None),
            ("tool", None))

    def test_tool_expires_after_900s_window(self):
        """tool 事件 1000s 前（> 900s 窗）-> 超窗回落 idle。"""
        self.assertEqual(
            dsb.status_detector(_state("tool", 1000), None, None),
            ("idle", None))

    def test_unknown_event_falls_back_to_default_window(self):
        """未知事件走 STATUS_IDLE_AFTER_MS_DEFAULT（60s）兜底窗口。

        实现事实（与任务描述的差异）：status_detector 是纯函数、无
        「原状态」可保持；未知事件即使落在 60s 兜底窗口内（30s 前），
        也无法映射到 generating/tool 徽标 -> 一律 idle。窗口内外结果
        相同，但都受兜底常量约束、不抛异常。
        """
        # 超出 60s 兜底窗口（70s 前）-> idle
        self.assertEqual(
            dsb.status_detector(_state("weird_event", 70), None, None),
            ("idle", None))
        # 兜底窗口内（30s 前）-> 同样 idle（未知事件无徽标映射）
        self.assertEqual(
            dsb.status_detector(_state("weird_event", 30), None, None),
            ("idle", None))
        # 兜底路径不得误触发心跳续期：未知事件 + 新鲜 activity_ts 仍 idle
        self.assertEqual(
            dsb.status_detector(_state("weird_event", 30), None, None,
                                activity_ts=dsb.time_ms() - 5 * 1000),
            ("idle", None))


class TestDbClosureAuthoritative(unittest.TestCase):
    """0.9.0 步骤 A：DB 轮次收尾是**权威**结论，优先于钩子事件推断。

    背景：Stop 是 7 个平台事件里唯一写 idle 的路径，而平台没有 SessionEnd /
    中断钩子——用户打断、请求报错时 Stop 常常不触发，只靠事件老化就要白挂
    120s 假「生成中」。turn_usage 行只在轮次结束时落库（实测本机 3741 行
    completed_at 为 NULL 的行数为 0），故「turn 行收尾时刻 >= 最后一条钩子
    事件」就是本轮已结束的确凿证据，可据此直接给结论并带出 error/cancelled。
    """

    def _st(self, age_s=5, hook=None, turn_started_at=None, event="generating"):
        now = dsb.time_ms()
        rec = {"event": event, "ts": now - age_s * 1000, "session_id": "sess_a"}
        if hook is not None:
            rec["hook"] = hook
        if turn_started_at is not None:
            rec["turn_started_at"] = turn_started_at
        return rec

    def _tu(self, status="completed", closed_age_s=2):
        """构造 turn_usage 行（recent_turn_stats 的 dict 形状）。"""
        closed = dsb.time_ms() - closed_age_s * 1000
        return {"turn_id": "t1", "status": status,
                "startedAt": closed - 3000, "completedAt": closed,
                "durationMs": 3000}

    def test_error_turn_overrides_fresh_generating(self):
        """事件 5s 前（120s 窗内）+ turn 行 2s 前以 error 收尾 -> error。"""
        self.assertEqual(
            dsb.status_detector(self._st(5), None, self._tu("error", 2),
                                current_sid="sess_a"),
            ("error", None))

    def test_cancelled_turn_overrides_fresh_tool(self):
        """tool 事件新鲜 + turn 行以 cancelled 收尾 -> cancelled（不是 tool）。"""
        self.assertEqual(
            dsb.status_detector(self._st(5, event="tool"), None,
                                self._tu("cancelled", 2), current_sid="sess_a"),
            ("cancelled", None))

    def test_completed_turn_yields_idle(self):
        """正常收尾：即使 generating 事件在窗口内也立即 idle（不再挂 120s）。"""
        self.assertEqual(
            dsb.status_detector(self._st(5), None, self._tu("completed", 2),
                                current_sid="sess_a"),
            ("idle", None))

    def test_outcome_beyond_hold_falls_back_to_idle(self):
        """收尾超过 OUTCOME_HOLD_MS(20s) -> 不再展示 error 徽标，回落 idle。

        红/灰徽标是「刚失败」的提示，不是历史档案：一直挂着会让用户以为
        会话仍处于异常态，且新事件到来前无法自证已恢复。
        """
        self.assertEqual(
            dsb.status_detector(self._st(40), None,
                                self._tu("error", dsb.OUTCOME_HOLD_MS // 1000 + 10),
                                current_sid="sess_a"),
            ("idle", None))

    def test_new_user_prompt_is_not_previous_turn_closure(self):
        """例外：最后一条是本轮 UserPromptSubmit 且起点 >= 上一轮收尾时刻
        -> 那是**新一轮刚开始**，不得被上一轮的收尾判成 idle。"""
        closed = dsb.time_ms() - 2000
        st = {"event": "generating", "ts": closed + 1000,
              "session_id": "sess_a", "hook": "UserPromptSubmit",
              "turn_started_at": closed + 1000}
        tu = {"turn_id": "t1", "status": "completed",
              "startedAt": closed - 5000, "completedAt": closed,
              "durationMs": 5000}
        self.assertEqual(
            dsb.status_detector(st, None, tu, current_sid="sess_a"),
            ("generating", None))

    def test_turn_start_before_closure_keeps_idle(self):
        """反向：turn_started_at 早于收尾时刻（同一轮的 PostToolUse 回落）
        -> 不构成「新一轮」，仍按收尾给 idle。"""
        closed = dsb.time_ms() - 2000
        st = {"event": "generating", "ts": closed + 1000,
              "session_id": "sess_a", "hook": "UserPromptSubmit",
              "turn_started_at": closed - 10000}
        tu = {"turn_id": "t1", "status": "completed",
              "startedAt": closed - 5000, "completedAt": closed,
              "durationMs": 5000}
        self.assertEqual(
            dsb.status_detector(st, None, tu, current_sid="sess_a"),
            ("idle", None))

    def test_closure_older_than_event_does_not_gate(self):
        """收尾早于事件（超出 TURN_END_TIE_MS 容差）-> 不拦截，走事件窗口。

        场景：Stop 漏写后用户又发消息（或工件事件迟到），此时 turn 行属于
        更早的一轮，不能拿它否定当前事件。
        """
        self.assertEqual(
            dsb.status_detector(self._st(5), None,
                                self._tu("completed", 60),
                                current_sid="sess_a"),
            ("generating", None))

    def test_running_turn_row_does_not_gate(self):
        """turn 行无 completedAt（理论不出现，防御）-> 不当作收尾证据。"""
        tu = self._tu("completed", 2)
        tu["completedAt"] = None
        self.assertEqual(
            dsb.status_detector(self._st(5), None, tu, current_sid="sess_a"),
            ("generating", None))

    def test_closure_gate_respects_session_mismatch(self):
        """收尾判定不得绕过 0.7.1 的会话匹配：别的会话的记录仍 idle。"""
        st = self._st(5)
        st["session_id"] = "sess_other"
        self.assertEqual(
            dsb.status_detector(st, None, self._tu("error", 2),
                                current_sid="sess_a"),
            ("idle", None))

    def test_unknown_outcome_status_yields_idle_not_error(self):
        """收尾状态是未知字符串 -> 不映射成 error/cancelled，回落 idle。"""
        self.assertEqual(
            dsb.status_detector(self._st(5), None, self._tu("weird", 2),
                                current_sid="sess_a"),
            ("idle", None))


if __name__ == "__main__":
    unittest.main()
