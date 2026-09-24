# -*- coding: utf-8 -*-
"""Stage 4 功能改进（第 1-3 项）单测：

- 项1：右键菜单「复制当前统计到剪贴板」/「打开统计报告」——菜单项存在性
  （源码静态断言，tkinter Menu 无法在无显示环境实例化）+ build_copy_text /
  build_report_text 纯函数行为。
- 项2：收起把手 ◐ 图标颜色跟随状态色——handle_status_color 纯函数映射。
- 项3：tooltip 补精确千分位值——turn_tooltip / cum_tooltip 含千分位精确行，
  主显示 format_tokens 缩写不变。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "docked_statusbar.py")
# Round2 Step 5：右键菜单与其 handler 随 run_gui 闭包提为
# statusbar_gui.StatusBarApp 方法，存在性静态断言改钉 statusbar_gui 源码。
_SRC_GUI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "statusbar_gui.py")


class TestMenuItemsExist(unittest.TestCase):
    """右键菜单必须包含两个新项，并接到对应 handler（静态断言）。"""

    def setUp(self):
        with open(_SRC_GUI, "r", encoding="utf-8") as f:
            self.src = f.read()

    def test_copy_menu_item(self):
        self.assertIn(u"复制当前统计到剪贴板", self.src)
        self.assertIn("copy_current_stats", self.src)
        self.assertIn("clipboard_append", self.src)

    def test_report_menu_item(self):
        self.assertIn(u"打开统计报告", self.src)
        self.assertIn("show_report_window", self.src)


class TestBuildCopyText(unittest.TestCase):
    """复制文本 = 状态徽标行 + 完整统计行 + 累计行；缺数据容错。"""

    def test_full_info(self):
        info = {
            "status": "generating",
            "turn_stats": {},
            "session_label": u"测试会话",
            "model": "kimi-k3",
            "stats": {"inputTokens": 2046123, "outputTokens": 5678,
                      "cacheReadTokens": 1023000, "avgDurationMs": 12345},
        }
        text = dsb.build_copy_text(info)
        lines = text.split(u"\n")
        self.assertEqual(len(lines), 3)
        self.assertIn(u"测试会话", lines[0])
        self.assertIn("kimi-k3", lines[0])
        self.assertIn("2.0M", lines[1])          # 主显示保持缩写
        self.assertIn(u"累计", lines[2])

    def test_none_info_returns_empty(self):
        self.assertEqual(dsb.build_copy_text(None), u"")

    def test_no_stats_no_text_falls_back_to_badge_only(self):
        text = dsb.build_copy_text({"status": "idle"})
        self.assertTrue(text)
        self.assertEqual(len(text.split(u"\n")), 1)


class TestBuildReportText(unittest.TestCase):
    """统计报告：查询失败不抛错，返回错误说明行。"""

    def test_bad_db_returns_error_text(self):
        text = dsb.build_report_text("definitely_not_exists_xyz.sqlite")
        self.assertTrue(text)
        self.assertIsInstance(text, str)


class TestHandleStatusColor(unittest.TestCase):
    """把手 ◐ 颜色 = STATUS_COLORS[status]；未知/缺省回落 idle。"""

    def test_each_status_maps_to_its_color(self):
        for status in ("generating", "tool", "error", "cancelled", "idle"):
            self.assertEqual(dsb.handle_status_color({"status": status}),
                             dsb.STATUS_COLORS[status])

    def test_none_and_unknown_fall_back_to_idle(self):
        self.assertEqual(dsb.handle_status_color(None),
                         dsb.STATUS_COLORS["idle"])
        self.assertEqual(dsb.handle_status_color({}),
                         dsb.STATUS_COLORS["idle"])
        self.assertEqual(dsb.handle_status_color({"status": "bogus"}),
                         dsb.STATUS_COLORS["idle"])


class TestTooltipThousandSeparators(unittest.TestCase):
    """tooltip 文本含精确千分位值；主显示 format_tokens 缩写不变。"""

    def test_format_tokens_exact(self):
        self.assertEqual(dsb.format_tokens_exact(2046123), "2,046,123")
        self.assertEqual(dsb.format_tokens_exact(0), "0")

    def test_turn_tooltip_contains_exact_values(self):
        ts = {"inputTokens": 2046123, "outputTokens": 5678,
              "modelCallCount": 1, "coldReadCount": 0}
        tip = dsb.turn_tooltip(ts)
        self.assertIn("2,046,123", tip)
        self.assertIn("5,678", tip)

    def test_turn_tooltip_none_no_exact_line(self):
        tip = dsb.turn_tooltip(None)
        self.assertNotIn(u"精确", tip)

    def test_cum_tooltip_contains_exact_values(self):
        stats = {"inputTokens": 2046123, "outputTokens": 5678}
        tip = dsb.cum_tooltip(stats)
        self.assertIn("2,046,123", tip)
        self.assertIn("5,678", tip)

    def test_cum_tooltip_none_is_empty(self):
        """0.11.2：口径说明行已删，无数据时 tooltip 为空串（不弹气泡）。"""
        self.assertEqual(dsb.cum_tooltip(None), u"")

    def test_main_display_still_abbreviated(self):
        self.assertEqual(dsb.format_tokens(2046123), "2.0M")
        self.assertEqual(dsb.format_tokens(5678), "5.7k")


if __name__ == "__main__":
    unittest.main()
