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


if __name__ == "__main__":
    unittest.main()
