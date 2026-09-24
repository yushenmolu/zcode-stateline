# -*- coding: utf-8 -*-
"""第二轮 Stage 4 功能项 1-3 单测：

- 项1 今日用量：db_today_stats（本地 0 点起全库交互来源聚合）+ today_text /
  handle_tip / cum_tooltip 的「今日：in X · out Y · hit Z%」显示；跨日边界
  （昨日行不计入）。
- 项2 上下文占用率：context_window_for（精确/前缀/缺省回落）+
  context_occupancy_text 文本与缺数据不显示；turn_tooltip 追加占用行。
- 项3 成本估算：show_cost 开关（关=完全不显示）、按 model_prices 单价表计算、
  缺单价不显示不报错；turn_tooltip / cum_tooltip 成本行。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


def _make_db(dirpath, rows):
    """最小 model_usage 临时 sqlite。rows 为
    (session_id, started_at, model_id, status, query_source, input, output,
     cache_read, cache_cre, reasoning, duration_ms, tool_call_count) 元组列表。
    返回 db_path。"""
    db_path = os.path.join(dirpath, "t.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE model_usage (session_id TEXT, started_at INTEGER, "
            "model_id TEXT, status TEXT, query_source TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, "
            "reasoning_tokens INTEGER, duration_ms INTEGER, tool_call_count INTEGER)")
        for r in rows:
            conn.execute("INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", r)
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestTodayStartMs(unittest.TestCase):
    """today_start_ms：本地 0 点边界；同注入时刻的今天 0 点 <= now < 明天 0 点。"""

    def test_midnight_boundary(self):
        now = dsb.time_ms()
        start = dsb.today_start_ms(now)
        self.assertLessEqual(start, now)
        self.assertLess(now - start, 24 * 3600 * 1000)
        # 明天 0 点 = 今天 0 点 + 24h
        self.assertEqual(dsb.today_start_ms(now + 24 * 3600 * 1000) >= start, True)

    def test_start_of_day_is_midnight(self):
        import datetime
        now = dsb.time_ms()
        start = dsb.today_start_ms(now)
        dt = datetime.datetime.fromtimestamp(start / 1000.0)
        self.assertEqual((dt.hour, dt.minute, dt.second), (0, 0, 0))


class TestDbTodayStats(unittest.TestCase):
    """db_today_stats：本地 0 点起全库聚合；昨日行/后台来源行不计入。"""

    def test_aggregates_today_only(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            today0 = dsb.today_start_ms(now)
            # 今日 2 行（不同会话）+ 昨日 1 行 + 今日后台来源 1 行
            db = _make_db(d, [
                ("sess_a", today0 + 1000, "m", "completed", "main_turn",
                 1000, 2000, 500, 0, 0, 1000, 0),
                ("sess_b", now - 1000, "m", "completed", "main_turn",
                 3000, 1000, 1500, 0, 0, 1000, 0),
                ("sess_a", today0 - 5000, "m", "completed", "main_turn",
                 999, 999, 0, 0, 0, 1000, 0),      # 昨日 -> 不计
                ("sess_a", today0 + 2000, "m", "completed", "compact",
                 777, 777, 0, 0, 0, 1000, 0),      # 后台来源 -> 不计
            ])
            stats = dsb.db_today_stats(db, now_ms=now)
            self.assertIsNotNone(stats)
            self.assertEqual(stats["inputTokens"], 4000)
            self.assertEqual(stats["outputTokens"], 3000)
            self.assertEqual(stats["cacheReadTokens"], 2000)

    def test_no_today_rows_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            today0 = dsb.today_start_ms(now)
            db = _make_db(d, [
                ("sess_a", today0 - 5000, "m", "completed", "main_turn",
                 999, 999, 0, 0, 0, 1000, 0),  # 只有昨日行
            ])
            self.assertIsNone(dsb.db_today_stats(db, now_ms=now))

    def test_bad_db_returns_none(self):
        self.assertIsNone(dsb.db_today_stats("definitely_not_exists_xyz.sqlite"))

    def test_conn_reuse_not_closed(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            today0 = dsb.today_start_ms(now)
            db = _make_db(d, [
                ("sess_a", today0 + 1000, "m", "completed", "main_turn",
                 100, 200, 50, 0, 0, 1000, 0),
            ])
            conn = sqlite3.connect(db)
            try:
                stats = dsb.db_today_stats(db, conn=conn, now_ms=now)
                self.assertEqual(stats["inputTokens"], 100)
                # conn 复用语义：传入的 conn 不被关闭，仍可执行
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()


class TestDbLatestModelInput(unittest.TestCase):
    """db_latest_model_input：会话最近一次交互来源调用的 input_tokens。"""

    def test_latest_row_wins(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            db = _make_db(d, [
                ("sess_a", now - 5000, "m", "completed", "main_turn",
                 111, 0, 0, 0, 0, 1000, 0),
                ("sess_a", now - 1000, "m", "completed", "main_turn",
                 222, 0, 0, 0, 0, 1000, 0),   # 最新
            ])
            self.assertEqual(dsb.db_latest_model_input(db, "sess_a"), 222)

    def test_excludes_background_and_subagent(self):
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            db = _make_db(d, [
                ("sess_a", now - 5000, "m", "completed", "main_turn",
                 111, 0, 0, 0, 0, 1000, 0),   # 最新交互行
                ("sess_a", now - 1000, "m", "completed", "compact",
                 999, 0, 0, 0, 0, 1000, 0),   # 后台来源 -> 不算
            ])
            self.assertEqual(dsb.db_latest_model_input(db, "sess_a"), 111)
            self.assertIsNone(dsb.db_latest_model_input(db, "sess_subagent_x"))

    def test_no_session_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            db = _make_db(d, [])
            self.assertIsNone(dsb.db_latest_model_input(db, "sess_a"))
            self.assertIsNone(dsb.db_latest_model_input(db, None))


class TestTodayText(unittest.TestCase):
    """today_text / handle_tip / cum_tooltip 的今日行。"""

    def test_today_text_full(self):
        td = {"inputTokens": 2046123, "outputTokens": 5678, "cacheReadTokens": 1023000}
        t = dsb.today_text(td)
        self.assertIn(u"今日", t)
        self.assertIn("2.0M", t)
        self.assertIn("5.7k", t)
        self.assertIn("hit 50.0%", t)

    def test_today_text_zero_input_no_hit(self):
        td = {"inputTokens": 0, "outputTokens": 100}
        t = dsb.today_text(td)
        self.assertNotIn("hit", t)

    def test_today_text_none(self):
        self.assertEqual(dsb.today_text(None), u"")

    def test_handle_tip_appends_today(self):
        td = {"inputTokens": 2046123, "outputTokens": 5678, "cacheReadTokens": 1023000}
        tip = dsb.handle_tip(td)
        self.assertIn(u"今日", tip)
        self.assertTrue(tip.startswith(dsb.HANDLE_TIP))

    def test_handle_tip_none_is_plain(self):
        self.assertEqual(dsb.handle_tip(None), dsb.HANDLE_TIP)

    def test_cum_tooltip_includes_today(self):
        stats = {"inputTokens": 2046123, "outputTokens": 5678}
        td = {"inputTokens": 3000, "outputTokens": 1000, "cacheReadTokens": 1500}
        tip = dsb.cum_tooltip(stats, today=td)
        self.assertIn(u"今日", tip)
        self.assertIn("hit 50.0%", tip)

    def test_cum_tooltip_backward_compatible(self):
        # 旧调用（只传 stats）行为不变
        stats = {"inputTokens": 2046123, "outputTokens": 5678}
        self.assertEqual(dsb.cum_tooltip(stats), dsb.cum_tooltip(stats, None, None, None))
        self.assertNotIn(u"今日", dsb.cum_tooltip(stats))


class TestContextWindow(unittest.TestCase):
    """context_window_for：精确/前缀/缺省回落；occupancy 文本。"""

    def test_exact_match(self):
        cfg = {"context_window": {"kimi-k3": 262144}}
        self.assertEqual(dsb.context_window_for("kimi-k3", cfg), 262144)

    def test_prefix_match_longest_wins(self):
        cfg = {"context_window": {"gpt-5": 400000, "gpt-5.6": 500000}}
        self.assertEqual(dsb.context_window_for("gpt-5.6-terra", cfg), 500000)
        self.assertEqual(dsb.context_window_for("gpt-5-other", cfg), 400000)

    def test_default_fallback(self):
        self.assertEqual(dsb.context_window_for("unknown", {}), dsb.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(dsb.context_window_for(None, {"context_window": {"a": 1}}),
                         dsb.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(dsb.context_window_for("unknown", None), dsb.DEFAULT_CONTEXT_WINDOW)

    def test_occupancy_text(self):
        cfg = {"context_window": {"kimi-k3": 262144}}
        t = dsb.context_occupancy_text(131072, "kimi-k3", cfg)
        self.assertIn("131,072", t)
        self.assertIn("262,144", t)
        self.assertIn("50.0%", t)

    def test_occupancy_none_input_no_text(self):
        cfg = {"context_window": {"kimi-k3": 262144}}
        self.assertEqual(dsb.context_occupancy_text(None, "kimi-k3", cfg), u"")

    def test_turn_tooltip_appends_occupancy(self):
        cfg = {"context_window": {"kimi-k3": 262144}}
        ts = {"inputTokens": 1000, "outputTokens": 500}
        tip = dsb.turn_tooltip(ts, latest_input=131072, model="kimi-k3", cfg=cfg)
        self.assertIn(u"上下文窗口", tip)
        self.assertIn("50.0%", tip)

    def test_turn_tooltip_no_occupancy_without_input(self):
        ts = {"inputTokens": 1000, "outputTokens": 500}
        tip = dsb.turn_tooltip(ts, model="kimi-k3", cfg={})
        self.assertNotIn(u"上下文窗口", tip)


class TestCostEstimation(unittest.TestCase):
    """estimate_cost / cost_text + tooltip 成本行（默认关）。"""

    def test_cost_calculation(self):
        cfg = {"show_cost": True,
               "model_prices": {"kimi-k3": {"input": 4.0, "output": 16.0}}}
        st = {"inputTokens": 1000000, "outputTokens": 500000}
        # 1M in * 4 + 0.5M out * 16 = 4 + 8 = 12
        self.assertAlmostEqual(dsb.estimate_cost(st, "kimi-k3", cfg), 12.0)

    def test_cost_prefix_model(self):
        cfg = {"show_cost": True,
               "model_prices": {"kimi-k3": {"input": 4.0, "output": 16.0}}}
        st = {"inputTokens": 1000000, "outputTokens": 500000}
        self.assertAlmostEqual(dsb.estimate_cost(st, "kimi-k3-pro", cfg), 12.0)

    def test_cost_switch_off_returns_none(self):
        cfg = {"show_cost": False,
               "model_prices": {"kimi-k3": {"input": 4.0, "output": 16.0}}}
        st = {"inputTokens": 1000000, "outputTokens": 500000}
        self.assertIsNone(dsb.estimate_cost(st, "kimi-k3", cfg))

    def test_cost_missing_price_returns_none_no_error(self):
        cfg = {"show_cost": True, "model_prices": {"other": {"input": 1, "output": 1}}}
        st = {"inputTokens": 1000000, "outputTokens": 500000}
        self.assertIsNone(dsb.estimate_cost(st, "kimi-k3", cfg))
        # 空单价表
        self.assertIsNone(dsb.estimate_cost(st, "kimi-k3", {"show_cost": True}))
        # 单价非数
        bad = {"show_cost": True, "model_prices": {"kimi-k3": {"input": "x"}}}
        self.assertIsNone(dsb.estimate_cost(st, "kimi-k3", bad))

    def test_cost_text(self):
        self.assertEqual(dsb.cost_text(12.5), u"≈¥12.50")
        self.assertEqual(dsb.cost_text(None), u"")

    def test_turn_tooltip_cost_on(self):
        cfg = {"show_cost": True, "context_window": {},
               "model_prices": {"kimi-k3": {"input": 4.0, "output": 16.0}}}
        ts = {"inputTokens": 1000000, "outputTokens": 500000}
        tip = dsb.turn_tooltip(ts, model="kimi-k3", cfg=cfg)
        self.assertIn(u"≈¥12.00", tip)
        self.assertIn(u"本轮成本", tip)

    def test_turn_tooltip_cost_off_no_text(self):
        cfg = {"show_cost": False,
               "model_prices": {"kimi-k3": {"input": 4.0, "output": 16.0}}}
        ts = {"inputTokens": 1000000, "outputTokens": 500000}
        tip = dsb.turn_tooltip(ts, model="kimi-k3", cfg=cfg)
        self.assertNotIn(u"≈¥", tip)
        self.assertNotIn(u"成本", tip)

    def test_turn_tooltip_cost_missing_price_no_text_no_error(self):
        cfg = {"show_cost": True, "model_prices": {"other": {"input": 1, "output": 1}}}
        ts = {"inputTokens": 1000000, "outputTokens": 500000}
        tip = dsb.turn_tooltip(ts, model="kimi-k3", cfg=cfg)
        self.assertNotIn(u"≈¥", tip)

    def test_cum_tooltip_cost_on(self):
        cfg = {"show_cost": True,
               "model_prices": {"kimi-k3": {"input": 4.0, "output": 16.0}}}
        st = {"inputTokens": 1000000, "outputTokens": 500000}
        tip = dsb.cum_tooltip(st, model="kimi-k3", cfg=cfg)
        self.assertIn(u"≈¥12.00", tip)
        self.assertIn(u"累计成本", tip)


class TestConfigParsing(unittest.TestCase):
    """_apply_raw_config：show_cost / context_window / model_prices 合并。"""

    def test_show_cost_bool(self):
        cfg = dict(dsb.DEFAULT_CONFIG)
        dsb._apply_raw_config(cfg, {"show_cost": True})
        self.assertTrue(cfg["show_cost"])
        cfg = dict(dsb.DEFAULT_CONFIG)
        dsb._apply_raw_config(cfg, {"show_cost": 1})
        self.assertTrue(cfg["show_cost"])

    def test_context_window_merge_and_override(self):
        cfg = dict(dsb.DEFAULT_CONFIG)
        dsb._apply_raw_config(cfg, {"context_window": {"my-model": 128000}})
        self.assertEqual(cfg["context_window"]["my-model"], 128000)
        # 默认键仍保留（合并非替换）
        self.assertIn("kimi-k3", cfg["context_window"])

    def test_context_window_bad_value_ignored(self):
        cfg = dict(dsb.DEFAULT_CONFIG)
        dsb._apply_raw_config(cfg, {"context_window": "not-a-dict"})
        self.assertIsInstance(cfg["context_window"], dict)

    def test_model_prices_merge(self):
        cfg = dict(dsb.DEFAULT_CONFIG)
        dsb._apply_raw_config(cfg, {"model_prices": {"my-m": {"input": 1.5, "output": 2.5}}})
        self.assertEqual(cfg["model_prices"]["my-m"], {"input": 1.5, "output": 2.5})
        self.assertIn("kimi-k3", cfg["model_prices"])

    def test_model_prices_bad_entry_ignored(self):
        cfg = dict(dsb.DEFAULT_CONFIG)
        dsb._apply_raw_config(cfg, {"model_prices": {"bad": "nope", "good": {"input": 1, "output": 2}}})
        self.assertNotIn("bad", cfg["model_prices"])
        self.assertIn("good", cfg["model_prices"])

    def test_default_config_has_new_keys(self):
        self.assertIn("show_cost", dsb.DEFAULT_CONFIG)
        self.assertIn("context_window", dsb.DEFAULT_CONFIG)
        self.assertIn("model_prices", dsb.DEFAULT_CONFIG)
        self.assertFalse(dsb.DEFAULT_CONFIG["show_cost"])  # 默认关


if __name__ == "__main__":
    unittest.main()
