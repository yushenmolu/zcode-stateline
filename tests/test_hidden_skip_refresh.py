# -*- coding: utf-8 -*-
"""Stage 2 性能：窗口完全不可见（shown=False）时 refresh_once 跳过数据组装。

三层钉住：
1. 纯函数 hidden_skip_refresh 的判定语义（只认 shown，收起把手 shown 恒 True
   不跳过）；
2. 动态行为：模拟 shown=False 的拍级入口，_db_connect 不得被调用；shown=True
   时正常调用（用纯函数守卫复刻 refresh_once 的入口结构，不真起 Tk）；
3. 静态接线（AST）：守卫确实插在 refresh_once 的 _db_connect 之前，且
   set_visible 恢复可见时强制 db_read_count=0（下一拍全量重读）。
"""
import ast
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import docked_statusbar as dsb

_SRC = os.path.join(os.path.dirname(__file__), "..", "scripts",
                    "docked_statusbar.py")


class TestHiddenSkipDecision(unittest.TestCase):
    def test_hidden_state_skips(self):
        self.assertTrue(dsb.hidden_skip_refresh({"shown": False}))

    def test_visible_state_does_not_skip(self):
        self.assertFalse(dsb.hidden_skip_refresh({"shown": True}))

    def test_collapsed_handle_still_refreshes(self):
        """收起把手要显示缓存率数字，shown 恒 True，不得被跳过。"""
        self.assertFalse(dsb.hidden_skip_refresh(
            {"shown": True, "collapsed": True}))

    def test_missing_key_defaults_to_visible(self):
        """缺 shown 键按可见处理（宁多查一拍，不漏刷新）。"""
        self.assertFalse(dsb.hidden_skip_refresh({}))


class TestDynamicBehavior(unittest.TestCase):
    """复刻 refresh_once 入口结构：热加载 -> 守卫 -> 数据组装（_db_connect）。
    不真起 Tk，只验证守卫的拦截语义。"""

    def _tick(self, state):
        if dsb.hidden_skip_refresh(state):
            return "skipped"
        dsb._db_connect(":memory:")
        return "refreshed"

    def test_hidden_tick_never_touches_db(self):
        with mock.patch.object(dsb, "_db_connect") as m:
            self.assertEqual(self._tick({"shown": False}), "skipped")
            m.assert_not_called()

    def test_visible_tick_calls_db(self):
        with mock.patch.object(dsb, "_db_connect") as m:
            self.assertEqual(self._tick({"shown": True}), "refreshed")
            m.assert_called_once()


class TestWiring(unittest.TestCase):
    """AST 锁接线：守卫位置与恢复重读逻辑（GUI 循环不便起 Tk）。"""

    @staticmethod
    def _body(name):
        with open(_SRC, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        return ast.get_source_segment(src, fn) or ""

    def test_guard_precedes_db_connect_in_refresh_once(self):
        body = self._body("refresh_once")
        self.assertIn("hidden_skip_refresh(state)", body)
        self.assertIn("return", body)
        self.assertLess(body.index("hidden_skip_refresh(state)"),
                        body.index("_db_connect(db_path)"),
                        "守卫必须插在拍级连接 _db_connect 之前")

    def test_guard_after_hot_reload(self):
        """配置热加载在守卫之前：隐藏期间配置仍可热更新。"""
        body = self._body("refresh_once")
        self.assertLess(body.index("hot_reload_config("),
                        body.index("hidden_skip_refresh(state)"))

    def test_restore_visible_forces_full_reread(self):
        body = self._body("set_visible")
        self.assertIn('state["db_read_count"] = 0', body,
                      "恢复可见不强制重读会残留隐藏前的旧缓存")


if __name__ == "__main__":
    unittest.main()
