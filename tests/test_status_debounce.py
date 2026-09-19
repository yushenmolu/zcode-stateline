# -*- coding: utf-8 -*-
"""徽标去抖（0.9.0）：busy 族（generating<->tool）内切换需最小驻留。

根因：hooks.json 里 PreToolUse->tool 与 PostToolUse->generating 逐次交替，
实测单轮 model_request_count p95=67（130+ 次翻转），叠加事件驱动刷新后
徽标在蓝/绿之间高频抖动 = 用户反馈的「状态不稳定」。

契约：族内切换受 STATUS_DWELL_MS 约束；进出 busy 族立即生效（去抖不得
延迟「停了显停」）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


class TestStatusDebounce(unittest.TestCase):

    def setUp(self):
        self.state = {}
        self.t0 = dsb.time_ms()

    def _at(self, status, offset_ms):
        return dsb.status_debounce(self.state, status,
                                   now_ms=self.t0 + offset_ms)

    def test_first_call_adopts_status(self):
        self.assertEqual(self._at("generating", 0), "generating")

    def test_same_status_does_not_reset_dwell(self):
        """同态重复到来只刷新展示，不重置驻留起算时刻。"""
        self._at("tool", 0)
        since = self.state["badge_shown_at"]
        self._at("tool", 1000)
        self.assertEqual(self.state["badge_shown_at"], since)

    def test_busy_family_switch_within_dwell_is_suppressed(self):
        """tool -> generating 在驻留窗内被压住，仍显示 tool。"""
        self._at("tool", 0)
        self.assertEqual(self._at("generating", 500), "tool")
        self.assertEqual(self._at("tool", 900), "tool")

    def test_busy_family_switch_after_dwell_takes_effect(self):
        """驻留满 STATUS_DWELL_MS 后族内切换生效。"""
        self._at("tool", 0)
        self.assertEqual(self._at("generating", dsb.STATUS_DWELL_MS + 1),
                         "generating")

    def test_entering_busy_family_is_immediate(self):
        """idle -> generating / idle -> tool 立即生效（新轮开始不能延迟）。"""
        self._at("idle", 0)
        self.assertEqual(self._at("generating", 10), "generating")
        self.assertEqual(self.state["badge_shown"], "generating")

    def test_leaving_busy_to_idle_is_immediate(self):
        self._at("tool", 0)
        self.assertEqual(self._at("idle", 10), "idle")

    def test_leaving_busy_to_error_is_immediate(self):
        """出错/中断属收尾态，必须压过正在展示的 busy 态即刻显红。"""
        self._at("generating", 0)
        self.assertEqual(self._at("error", 10), "error")
        self.assertEqual(self._at("cancelled", 20), "cancelled")

    def test_busy_after_outcome_state_is_immediate(self):
        """收尾态 -> busy 不属族内切换（error 不在 busy 族），立即生效。"""
        self._at("error", 0)
        self.assertEqual(self._at("generating", 10), "generating")

    def test_suppressed_switch_does_not_lose_target_state(self):
        """被压住的拍不污染 state：驻留满后下一拍即切到新态。"""
        self._at("tool", 0)
        self._at("generating", 100)
        self.assertEqual(self.state["badge_shown"], "tool")
        self.assertEqual(self._at("generating", dsb.STATUS_DWELL_MS + 100),
                         "generating")

    def test_none_state_returns_status_without_error(self):
        """state=None（--once 一次性调用）：直接透传，不抛。"""
        self.assertEqual(
            dsb.status_debounce(None, "tool", now_ms=self.t0), "tool")

    def test_badge_text_and_color_defined_for_all_family_members(self):
        """去抖依赖 BUSY_FAMILY 成员名，徽标文案/配色必须齐全，
        否则徽标会渲染成空白。"""
        for s in ("idle", "generating", "tool", "error", "cancelled"):
            self.assertIn(s, dsb.STATUS_TEXT, u"缺 STATUS_TEXT[%s]" % s)
            self.assertIn(s, dsb.STATUS_COLORS, u"缺 STATUS_COLORS[%s]" % s)
            self.assertIn(s, dsb.STATUS_TIPS, u"缺 STATUS_TIPS[%s]" % s)


if __name__ == "__main__":
    unittest.main()
