# -*- coding: utf-8 -*-
"""第二轮 Stage 4 功能项 4-5 单测：

- 项4 历史趋势迷你图：db_stats.query_daily_stats 按自然日聚合（token 仅
  completed、排除 subagent、窗口外不计）、_spark_char 8 档块字符分档、
  _fmt_daily 行格式 `MM-DD ▅ in 1.2M hit 63%`、history 汇总带 daily 键。
- 项5 多会话速览：statusbar_db.db_recent_sessions 最近 N 个活跃主会话
  （标题/状态/本轮耗时、排序与排除规则）、docked_statusbar.
  build_sessions_overview_text 文本、_fmt_round_dur 耗时缩写、菜单项与
  窗口 handler 存在性（源码静态断言，沿用项1-3 的同款断言方式）。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db_stats
import docked_statusbar as dsb

_SRC_GUI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "statusbar_gui.py")


def _day_ms(y, m, d, hh=12):
    """本地时区某日某时刻的 epoch ms（用 time.mktime 与库内 localtime 同口径）。"""
    import time as _t
    return int(_t.mktime((y, m, d, hh, 0, 0, 0, 0, -1)) * 1000)


def _make_db(dirpath, rows, sessions=None):
    """model_usage + session 最小临时 sqlite。rows 为
    (session_id, started_at, model_id, status, query_source, input, output,
     cache_read, cache_cre, reasoning, duration_ms, tool_call_count)；
    sessions 为 (id, title) 列表。返回 db_path。"""
    db_path = os.path.join(dirpath, "t.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE model_usage (session_id TEXT, started_at INTEGER, "
            "model_id TEXT, status TEXT, query_source TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, "
             "reasoning_tokens INTEGER, duration_ms INTEGER, tool_call_count INTEGER, "
            "time_to_first_token_ms INTEGER)")
        for r in rows:
            conn.execute(
                "INSERT INTO model_usage (session_id, started_at, model_id, status, "
                "query_source, input_tokens, output_tokens, cache_read_input_tokens, "
                "cache_creation_input_tokens, reasoning_tokens, duration_ms, "
                "tool_call_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", r)
        conn.execute("CREATE TABLE session (id TEXT, title TEXT, time_updated INTEGER)")
        for s in (sessions or []):
            conn.execute("INSERT INTO session VALUES (?,?,?)", (s[0], s[1], 0))
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestQueryDailyStats(unittest.TestCase):
    """query_daily_stats：按本地自然日聚合，token 仅 completed，排除 subagent。"""

    def test_groups_by_day_and_sums_completed_only(self):
        with tempfile.TemporaryDirectory() as d:
            day1, day2 = _day_ms(2026, 9, 20), _day_ms(2026, 9, 21)
            db = _make_db(d, [
                ("sess_a", day1, "m", "completed", "main_turn",
                 1000, 500, 600, 0, 0, 1000, 0),
                ("sess_a", day1 + 3600_000, "m", "completed", "main_turn",
                 200, 100, 0, 0, 0, 1000, 0),
                ("sess_b", day2, "m", "completed", "main_turn",
                 3000, 900, 1500, 0, 0, 1000, 0),
                ("sess_b", day2, "m", "error", "main_turn",
                 999, 999, 999, 0, 0, 1000, 0),        # 非 completed -> 不计 token
                ("sess_b", day2, "m", "completed", "subagent",
                 777, 777, 0, 0, 0, 1000, 0),          # subagent -> 不计
                ("sess_c", day1 - 8 * 86400_000, "m", "completed", "main_turn",
                 555, 555, 0, 0, 0, 1000, 0),          # 窗口外 -> 不计
            ])
            rows = db_stats.query_daily_stats(db, since_ts=day1 - 1000)
            self.assertEqual([r["day"] for r in rows], ["09-20", "09-21"])
            self.assertEqual(rows[0]["input_tokens"], 1200)
            self.assertEqual(rows[0]["cache_hit_rate"], round(600 / 1200.0, 4))
            self.assertEqual(rows[1]["input_tokens"], 3000)
            self.assertEqual(rows[1]["model_request_count"], 2)  # error 行计入请求数

    def test_empty_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db = _make_db(d, [])
            self.assertEqual(db_stats.query_daily_stats(db, since_ts=0), [])

    def test_history_stats_carries_daily(self):
        with tempfile.TemporaryDirectory() as d:
            day1 = _day_ms(2026, 9, 20)
            db = _make_db(d, [
                ("sess_a", day1, "m", "completed", "main_turn",
                 1000, 500, 600, 0, 0, 1000, 0),
            ])
            agg = db_stats.query_history_stats(db, since_ts=day1 - 1000)
            self.assertIn("daily", agg)
            self.assertEqual(len(agg["daily"]), 1)
            self.assertEqual(agg["daily"][0]["input_tokens"], 1000)


class TestSparkChar(unittest.TestCase):
    """_spark_char：相对最高日量级映射 8 档块字符。"""

    def test_max_gets_full_block(self):
        self.assertEqual(db_stats._spark_char(100, 100), u"█")

    def test_zero_or_bad_gets_lowest(self):
        self.assertEqual(db_stats._spark_char(0, 100), u"▁")
        self.assertEqual(db_stats._spark_char(50, 0), u"▁")
        self.assertEqual(db_stats._spark_char("x", 100), u"▁")

    def test_middle_tiers_ordered(self):
        # 越大档位字符在块字符序列里越靠后
        blocks = db_stats.SPARK_BLOCKS
        c_low = db_stats._spark_char(10, 100)
        c_mid = db_stats._spark_char(50, 100)
        c_high = db_stats._spark_char(90, 100)
        self.assertLess(blocks.index(c_low), blocks.index(c_mid))
        self.assertLess(blocks.index(c_mid), blocks.index(c_high))
        # 有量至少第二档（▂），与「没量」区分
        self.assertNotEqual(c_low, u"▁")

    def test_over_max_clamped(self):
        self.assertEqual(db_stats._spark_char(200, 100), u"█")


class TestFmtDaily(unittest.TestCase):
    """_fmt_daily：`09-20 ▅ in 1.2M hit 63%` 行格式。"""

    def test_line_format(self):
        daily = [
            {"day": "09-20", "input_tokens": 600_000, "output_tokens": 0,
             "cache_read_input_tokens": 300_000, "cache_hit_rate": 0.5,
             "model_request_count": 3},
            {"day": "09-21", "input_tokens": 1_200_000, "output_tokens": 0,
             "cache_read_input_tokens": 756_000, "cache_hit_rate": 0.63,
             "model_request_count": 5},
        ]
        text = db_stats._fmt_daily(daily)
        lines = text.split(u"\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("09-20", lines[0])
        self.assertIn("in 600.0k", lines[0])
        self.assertIn("hit 50%", lines[0])
        self.assertIn("in 1.2M", lines[1])
        self.assertIn("hit 63%", lines[1])
        # 最高日给满格 █
        self.assertIn(u"█", lines[1])

    def test_empty_returns_empty(self):
        self.assertEqual(db_stats._fmt_daily([]), u"")


class TestDbRecentSessions(unittest.TestCase):
    """db_recent_sessions：最近 N 个活跃主会话，标题/状态/本轮耗时。"""

    def test_order_and_fields(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            db = _make_db(d, [
                ("sess_a", now - 5000, "m", "completed", "main_turn",
                 100, 50, 0, 0, 0, 12000, 0),
                ("sess_b", now - 1000, "m", "completed", "main_turn",
                 100, 50, 0, 0, 0, 8000, 0),   # 最新
                ("sess_c", now - 9000, "m", "error", "main_turn",
                 100, 50, 0, 0, 0, None, 0),
            ], sessions=[("sess_a", u"旧会话"), ("sess_b", u"新会话")])
            rows = dsb.db_recent_sessions(db, limit=3)
            self.assertEqual([r["session_id"] for r in rows],
                             ["sess_b", "sess_a", "sess_c"])
            self.assertEqual(rows[0]["title"], u"新会话")
            self.assertEqual(rows[0]["status"], "completed")
            self.assertEqual(rows[0]["last_duration_ms"], 8000)
            self.assertEqual(rows[2]["status"], "error")
            self.assertIsNone(rows[2]["last_duration_ms"])

    def test_limit_and_exclusions(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            db = _make_db(d, [
                ("sess_a", now - 3000, "m", "completed", "main_turn",
                 100, 50, 0, 0, 0, 1000, 0),
                ("sess_b", now - 2000, "m", "completed", "main_turn",
                 100, 50, 0, 0, 0, 1000, 0),
                ("sess_c", now - 1000, "m", "completed", "main_turn",
                 100, 50, 0, 0, 0, 1000, 0),
                ("sess_subagent_x", now - 500, "m", "completed", "main_turn",
                 100, 50, 0, 0, 0, 1000, 0),                  # 子代理 -> 排除
                ("sess_d", now - 100, "m", "completed", "compact",
                 100, 50, 0, 0, 0, 1000, 0),                  # 后台来源 -> 排除
            ])
            rows = dsb.db_recent_sessions(db, limit=3)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["session_id"], "sess_c")
            sids = [r["session_id"] for r in rows]
            self.assertNotIn("sess_subagent_x", sids)
            self.assertNotIn("sess_d", sids)

    def test_running_duration_is_elapsed(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            db = _make_db(d, [
                ("sess_a", now - 30_000, "m", "running", "main_turn",
                 100, 50, 0, 0, 0, None, 0),
            ])
            rows = dsb.db_recent_sessions(db, limit=3)
            self.assertEqual(rows[0]["status"], "running")
            # 已耗时 >= 30s（允许少量执行耗时漂移）
            self.assertGreaterEqual(rows[0]["last_duration_ms"], 30_000)

    def test_bad_db_returns_empty(self):
        self.assertEqual(dsb.db_recent_sessions("definitely_not_exists_xyz.sqlite"), [])


class TestSessionsOverviewText(unittest.TestCase):
    """build_sessions_overview_text / _fmt_round_dur 文本。"""

    def test_fmt_round_dur(self):
        self.assertEqual(dsb._fmt_round_dur(12345), "12.3s")
        self.assertEqual(dsb._fmt_round_dur(83_000), "1m23s")
        self.assertEqual(dsb._fmt_round_dur(None), u"—")

    def test_overview_text_full(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            db = _make_db(d, [
                ("sess_a", now - 5000, "m", "completed", "main_turn",
                 100, 50, 0, 0, 0, 12000, 0),
                ("sess_b", now - 1000, "m", "running", "main_turn",
                 100, 50, 0, 0, 0, None, 0),
            ], sessions=[("sess_a", u"写报告"), ("sess_b", u"")])
            text = dsb.build_sessions_overview_text(db, limit=3)
            self.assertIn(u"最近 3 个活跃会话", text)
            self.assertIn(u"写报告", text)
            self.assertIn(u"已完成", text)
            self.assertIn("12.0s", text)
            self.assertIn(u"生成中", text)
            # 无标题会话兜底显示 session_id
            self.assertIn("sess_b", text)

    def test_overview_text_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db = _make_db(d, [])
            text = dsb.build_sessions_overview_text(db, limit=3)
            self.assertIn(u"暂无", text)

    def test_overview_text_bad_db_no_raise(self):
        text = dsb.build_sessions_overview_text("definitely_not_exists_xyz.sqlite")
        self.assertIn(u"暂无", text)


class TestSessionsOverviewMenuWiring(unittest.TestCase):
    """右键菜单「最近会话速览」项与 handler 存在性（静态断言，沿用项1-3 方式）。"""

    def setUp(self):
        with open(_SRC_GUI, "r", encoding="utf-8") as f:
            self.src = f.read()

    def test_menu_item_and_handler(self):
        self.assertIn(u"最近会话速览", self.src)
        self.assertIn("show_sessions_overview_window", self.src)
        self.assertIn("build_sessions_overview_text", self.src)
        self.assertIn("add_command", self.src)


if __name__ == "__main__":
    unittest.main()
