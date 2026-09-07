# -*- coding: utf-8 -*-
"""Stage 3: UIA warm 切换探测——resolve_sid_by_title 标题反查单元测试。

probe_active_tab_title 本体依赖真实 ZCode 窗口 UIA 树（实测标签栏不可达，
返回 None），不做自动化断言；本文件只测与 UIA 无关的标题反查逻辑。
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import uia_tab_probe as probe


def _mkedb():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE session (id TEXT, title TEXT)")
    return conn


class TestResolveSidByTitle(unittest.TestCase):
    """标题反查：精确唯一命中；0 行 None；重名歧义 None；截断退化前缀。"""

    def test_exact_match_unique(self):
        conn = _mkedb()
        try:
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_a", "我的小说项目"))
            conn.commit()
            self.assertEqual(probe.resolve_sid_by_title(conn, "我的小说项目"),
                             "sess_a")
        finally:
            conn.close()

    def test_no_match_returns_none(self):
        conn = _mkedb()
        try:
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_a", "我的小说项目"))
            conn.commit()
            self.assertIsNone(probe.resolve_sid_by_title(conn, "不存在的标题"))
            self.assertIsNone(probe.resolve_sid_by_title(conn, ""))
            self.assertIsNone(probe.resolve_sid_by_title(conn, None))
        finally:
            conn.close()

    def test_ambiguous_returns_none(self):
        """精确匹配多行（重名）-> None（宁可不切，不可错切）。"""
        conn = _mkedb()
        try:
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_a", "untitled"))
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_b", "untitled"))
            conn.commit()
            self.assertIsNone(probe.resolve_sid_by_title(conn, "untitled"))
        finally:
            conn.close()

    def test_truncated_ellipsis_falls_back_to_prefix(self):
        """标题以 …（U+2026）或 ... 结尾 -> LIKE '前缀%'；前缀多行 -> None。"""
        conn = _mkedb()
        try:
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_a", "这是一个很长的会话标题abc"))
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_b", "这是另一个会话标题xyz"))
            conn.commit()
            # 截断唯一命中
            self.assertEqual(
                probe.resolve_sid_by_title(conn, "这是一个很长的会话…"),
                "sess_a")
            self.assertEqual(
                probe.resolve_sid_by_title(conn, "这是一个很长的会话..."),
                "sess_a")
            # 前缀仍多行 -> None
            self.assertIsNone(probe.resolve_sid_by_title(conn, "这是…"))
            # LIKE 转义：前缀含 % 时按字面匹配（防 % 变通配扩大匹配面）。
            # 若未转义，前缀 "100%" 会变成 LIKE '100%%'（= 以 100 开头），
            # 将同时命中 "100%完成" 与 "100x完成" -> 歧义 None；
            # 转义正确则只命中字面前缀 "100%" -> sess_c。
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_c", "100%完成"))
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_d", "100x完成"))
            conn.commit()
            self.assertEqual(probe.resolve_sid_by_title(conn, "100%完成"),
                             "sess_c")
            self.assertEqual(probe.resolve_sid_by_title(conn, "100%…"),
                             "sess_c")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
