# -*- coding: utf-8 -*-
"""0.9.1 速度显示层单测：speed_hold（轮次收尾后短时保留最后已知值）与
speed_display（第二行尾部该画哪几段）。

背景（用户反馈「为什么不显示速度」）：0.9.0 的速度只在 status=='generating'
时取、也只有 generating 才画，而
  1. 一轮里 PreToolUse -> tool 与 PostToolUse -> generating 交替，工具执行期
     （常占一轮大半时间）整段不画；
  2. model_usage 只在调用结束时落库（本机 completed_at 为 NULL 的行数为 0），
     所以本轮首次调用期间没有可用值；
  3. 轮次一收尾（Stop / DB 权威收尾）徽标离开 busy 族，数字当帧消失，用户
     来不及看，也读作「功能没生效」。
1 与 3 是本文件覆盖的修复；2 是平台边界，仍以「生成中…」占位诚实表达。
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "docked_statusbar.py")
# Round2 Step 5：refresh_once / render_ui 随 run_gui 闭包提为
# statusbar_gui.StatusBarApp 方法，AST 接线断言改钉 statusbar_gui 源码。
_SRC_GUI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "statusbar_gui.py")


class TestSpeedHold(unittest.TestCase):
    """speed_hold：把「本轮速度」与「收尾后短时保留的最后已知值」分开。"""

    def test_busy_speed_is_returned_and_cached(self):
        st = {}
        self.assertEqual(dsb.speed_hold(st, "generating", 120.0, "s1", now_ms=1000),
                         (120.0, False))
        self.assertEqual(st["speed_last"], 120.0)
        self.assertEqual(st["speed_last_at"], 1000)
        self.assertEqual(st["speed_last_sid"], "s1")

    def test_busy_without_speed_shows_nothing(self):
        """本轮尚无完成的调用：即使缓存里有上一轮的值也不画——宁可显示
        「生成中…」占位，不拿别的轮次的速度冒充本轮（批 4 的口径）。"""
        st = {"speed_last": 99.0, "speed_last_at": 0, "speed_last_sid": "s1"}
        self.assertEqual(dsb.speed_hold(st, "tool", None, "s1", now_ms=1000),
                         (None, False))

    def test_after_turn_end_last_value_is_held_and_marked(self):
        st = {"speed_last": 120.0, "speed_last_at": 1000, "speed_last_sid": "s1"}
        got = dsb.speed_hold(st, "idle", None, "s1",
                             now_ms=1000 + dsb.SPEED_HOLD_MS - 1)
        self.assertEqual(got, (120.0, True))

    def test_hold_expires(self):
        st = {"speed_last": 120.0, "speed_last_at": 1000, "speed_last_sid": "s1"}
        self.assertEqual(
            dsb.speed_hold(st, "idle", None, "s1",
                           now_ms=1000 + dsb.SPEED_HOLD_MS + 1), (None, False))
        self.assertIsNone(st["speed_last"])   # 过期即清，不留悬空值

    def test_other_session_cache_is_dropped(self):
        """切会话不得把上一会话的速度带过来：徽标已经是新会话的了。"""
        st = {"speed_last": 120.0, "speed_last_at": 1000, "speed_last_sid": "s1"}
        self.assertEqual(dsb.speed_hold(st, "idle", None, "s2", now_ms=2000),
                         (None, False))
        self.assertIsNone(st["speed_last"])

    def test_none_state_is_safe(self):
        self.assertEqual(dsb.speed_hold(None, "idle", None, "s1", now_ms=1000),
                         (None, False))
        self.assertEqual(dsb.speed_hold({}, "generating", 5.0, "s1", now_ms=1000),
                         (5.0, False))

    def test_error_and_cancelled_endings_also_hold(self):
        """出错/打断收尾后同样短时留值（用户正是想知道「最后那段跑多快」）。"""
        for status in ("error", "cancelled"):
            st = {"speed_last": 77.0, "speed_last_at": 1000,
                  "speed_last_sid": "s1"}
            self.assertEqual(
                dsb.speed_hold(st, status, None, "s1", now_ms=2000),
                (77.0, True), status)


class TestSpeedDisplaySegments(unittest.TestCase):
    """speed_display：第二行本轮统计尾部的速度段（纯数据，不碰 tkinter）。"""

    def test_generating_and_tool_render_identically(self):
        """B 的核心：工具中与生成中一样显示数值，不再只有生成中才画。"""
        a = dsb.speed_display("generating", 64.2, False)
        b = dsb.speed_display("tool", 64.2, False)
        self.assertEqual(a, b)
        self.assertEqual([s[0] for s in a], [u" · \u26a1", u"64.2 tok/s"])

    def test_generating_without_speed_keeps_placeholder(self):
        segs = dsb.speed_display("generating", None, False)
        self.assertEqual([s[0] for s in segs], [u" · \u751f\u6210\u4e2d\u2026"])

    def test_tool_without_speed_renders_nothing(self):
        """工具执行中不该显示「生成中…」占位（那会误导成在出 token）。"""
        self.assertEqual(dsb.speed_display("tool", None, False), [])

    def test_held_value_is_marked(self):
        segs = dsb.speed_display("idle", 64.2, True)
        self.assertEqual([s[0] for s in segs],
                         [u" · \u26a1", u"64.2 tok/s", u"\uff08\u4e0a\u6b21\uff09"])
        self.assertEqual(segs[-1][2], dsb.FG_DIM)   # 标注一律灰字，不与本轮值争视觉

    def test_idle_without_any_value_renders_nothing(self):
        self.assertEqual(dsb.speed_display("idle", None, False), [])

    def test_show_live_off_disables_everything(self):
        for status, spd, held in (("generating", 64.2, False),
                                  ("tool", 64.2, False),
                                  ("idle", 64.2, True),
                                  ("generating", None, False)):
            self.assertEqual(
                dsb.speed_display(status, spd, held, show_live=False), [],
                "show_live=false 时不得画任何速度段")

    def test_show_speed_off_hides_readouts_but_not_placeholder(self):
        """show_speed 从 0.9.1 起真的管第二行数值段。此前它只管
        build_metric_blocks，而那个函数自 0.4.0 三区改版后无任何调用者——
        配置项形同虚设。占位「生成中…」是状态可见性兜底，不受它影响。"""
        self.assertEqual(
            dsb.speed_display("generating", 64.2, False, show_speed=False), [])
        self.assertEqual(
            dsb.speed_display("idle", 64.2, True, show_speed=False), [])
        self.assertEqual(
            [s[0] for s in dsb.speed_display("generating", None, False,
                                             show_speed=False)],
            [u" · \u751f\u6210\u4e2d\u2026"])

    def test_all_segments_are_three_tuples(self):
        """段结构 (text, font, color) 与 _turn_segments 的消费方式一致。"""
        for seg in (dsb.speed_display("generating", 1.0, False)
                    + dsb.speed_display("idle", 1.0, True)):
            self.assertEqual(len(seg), 3)
            self.assertIsInstance(seg[0], str)


class TestWiring(unittest.TestCase):
    """静态接线契约：纯函数写得对但没接上 = 用户仍然看不到速度（0.9.0 的
    show_speed 死配置就是这么脱节的）。GUI 循环不便起 Tk，故用 AST 锁接线。"""

    @staticmethod
    def _body(name):
        with open(_SRC_GUI, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        return ast.get_source_segment(src, fn) or ""

    def test_refresh_once_routes_speed_through_hold(self):
        body = self._body("refresh_once")
        self.assertIn("speed_hold(", body,
                      "刷新循环未过 speed_hold，收尾后仍会当帧丢速度")
        self.assertIn('info["status_speed_held"]', body,
                      "held 标记没接进 info，渲染层无从标注「（上次）」")

    def test_render_uses_speed_display_with_both_flags(self):
        body = self._body("render_ui")
        self.assertIn("speed_display(", body)
        self.assertIn('cfg.get("show_speed", True)', body,
                      "show_speed 又变成死配置")
        self.assertNotIn('status == "generating"', body,
                         "渲染层不得再单判 generating（工具中也要显示速度）")


if __name__ == "__main__":
    unittest.main()
