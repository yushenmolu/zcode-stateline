# -*- coding: utf-8 -*-
"""0.9.4：让命中率读数可自证（累计段 hit + 本轮请求级画像）。

用户反馈「中转上命中率 80 多，这边显示 49.8%」——核对下来两边**公式完全相同**
（缓存读取 ÷ 输入总量），差在总体不同：中转的小时曲线以 subagent 流量为主
（本机该小时 145 行里 134 行、命中率 86.2%，同期主会话交互调用 51.2%），而小条
只看主会话。数字是对的，但**看不出为什么对**，所以补两处：
  ① 会话累计段补 hit（收起态把手本来就显示它，展开反而没有）；
  ② 本轮 tooltip 补「本轮 n 次请求，其中冷读 k 次」——一轮 2 次请求里 1 次冷读
     （实测 cr=64/161403）就足以把读数打到 49.7%，没有这句只能怀疑算错。
冷读判据取 input 的 1%：本机 9386 条非后台 completed 调用实测双峰（<1% 占
16.6%、1%~5% 仅 0.4%、>=90% 占 75.4%），阈值正落在波谷里。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb

S = dsb.time_ms  # 便于按「距今多少秒」构造时刻


def _mkdb(path, mu_rows, tu_rows):
    """按真实 schema 的关键列建临时 sqlite（列序同 _mu / _tu）。"""
    conn = sqlite3.connect(path)
    try:
        for t in ("model_usage", "turn_usage", "tool_usage"):
            conn.execute("DROP TABLE IF EXISTS %s" % t)
        conn.execute(
            "CREATE TABLE model_usage (id TEXT, session_id TEXT, turn_id TEXT, "
            "query_source TEXT, model_id TEXT, status TEXT, started_at INTEGER, "
            "first_token_at INTEGER, completed_at INTEGER, duration_ms INTEGER, "
            "tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
            "reasoning_tokens INTEGER, cache_creation_input_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, provider_total_tokens INTEGER, "
            "computed_total_tokens INTEGER)")
        conn.execute(
            "CREATE TABLE turn_usage (session_id TEXT, turn_id TEXT, "
            "status TEXT, started_at INTEGER, first_token_at INTEGER, "
            "completed_at INTEGER, duration_ms INTEGER, "
            "time_to_first_token_ms INTEGER, model_request_count INTEGER, "
            "tool_call_count INTEGER, tool_error_count INTEGER, "
            "input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, "
            "cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER, "
            "computed_total_tokens INTEGER, error_type TEXT, "
            "cancelled_by_user INTEGER, context_exceeded INTEGER, "
            "PRIMARY KEY (session_id, turn_id))")
        conn.execute(
            "CREATE TABLE tool_usage (session_id TEXT, turn_id TEXT, "
            "tool_name TEXT, status TEXT, started_at INTEGER, "
            "completed_at INTEGER, duration_ms INTEGER, exit_code INTEGER, "
            "error_type TEXT)")
        for r in mu_rows:
            conn.execute("INSERT INTO model_usage (session_id, query_source, "
                         "status, started_at, first_token_at, completed_at, "
                         "duration_ms, tool_call_count, input_tokens, "
                         "output_tokens, cache_read_input_tokens, "
                         "computed_total_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         r)
        for r in tu_rows:
            conn.execute("INSERT INTO turn_usage (session_id, turn_id, status, "
                         "started_at, completed_at, duration_ms, tool_call_count, "
                         "tool_error_count, input_tokens, output_tokens, "
                         "cache_read_input_tokens, computed_total_tokens, "
                         "error_type, cancelled_by_user, context_exceeded) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
        conn.commit()
    finally:
        conn.close()


def _mu(session="sess_a", source="main_turn", started=8, completed=4,
        inp=1000, cache=900, out=800, tools=0, duration=4000, total=2300,
        status="completed"):
    """model_usage 一行（started/completed 为「距今秒数」，默认暖读 90%）。"""
    return (session, source, status, S() - started * 1000,
            S() - started * 1000 + 200, S() - completed * 1000, duration,
            tools, inp, out, cache, total)


def _cold(**kw):
    """一次**冷读**调用（真机那把 cr=64/161403 的等比缩小版）。"""
    kw.setdefault("inp", 160000)
    kw.setdefault("cache", 64)
    return _mu(**kw)


def _tu(session="sess_a", turn="t1", status="completed", started=10,
        completed=3, inp=2600, cache=964, out=1600, tools=2, errors=0,
        duration=7000, total=4300):
    return (session, turn, status, S() - started * 1000, S() - completed * 1000,
            duration, tools, errors, inp, out, cache, total,
            None, None, None)


class TestColdReadPredicate(unittest.TestCase):
    """冷读判据：cache_read 不足 input 的 1%，NULL 行既不计数也不炸查询。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "t.sqlite")

    def _count(self, *mu_rows):
        _mkdb(self.db, list(mu_rows), [])
        return dsb.db_turn_request_profile(self.db, "sess_a",
                                           S() - 60 * 1000, S())

    def test_threshold_sits_in_the_measured_valley(self):
        self.assertEqual(dsb.COLD_READ_RATIO, 0.01)
        self.assertIn("input_tokens * 0.01", dsb.COLD_READ_COUNT_SQL)

    def test_exact_boundary_is_not_cold(self):
        """cr 恰为 input 的 1% 不算冷读（判据是「不足」）。"""
        self.assertEqual(self._count(_mu(inp=10000, cache=100)), (1, 0))
        self.assertEqual(self._count(_mu(inp=10000, cache=99)), (1, 1))

    def test_zero_cache_read_is_cold(self):
        self.assertEqual(self._count(_mu(cache=0)), (1, 1))

    def test_null_tokens_are_neither_cold_nor_fatal(self):
        """input / cache_read 为 NULL：比较结果 NULL 走 ELSE 0，不能计成冷读。"""
        self.assertEqual(self._count(_mu(inp=None, cache=None)), (1, 0))

    def test_error_calls_count_like_any_other(self):
        """失败那一次照样进 n，也照样按 token 占比判冷读：它的 input 已计进
        本轮 token 总量、cache_read 却是 0，正是本轮命中率掉下去的那一半。"""
        self.assertEqual(self._count(_mu(status="error", cache=0), _mu()), (2, 1))

    def test_background_sources_are_part_of_this_turns_profile(self):
        """画像**不过滤** query_source：turn_usage 的 token 总量含 compact /
        session_title（真机对账 12/12 轮相等），解释它的句子必须同总体。
        一次 194k 输入的 compact 冷读，正是「这一轮命中率怎么这么低」的答案。
        4 次调用（3 冷 1 暖）全在 n 里。"""
        _mkdb(self.db, [_cold(), _cold(source="compact"),
                        _cold(source="session_title"),
                        _mu(source="main_turn")], [])
        self.assertEqual(
            dsb.db_turn_request_profile(self.db, "sess_a", S() - 60 * 1000, S()),
            (4, 3))


class TestTurnRequestProfile(unittest.TestCase):
    """db_turn_request_profile：窗口闭区间、上下界必需、失败退 None。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "t.sqlite")

    def test_only_rows_inside_the_window_count(self):
        """上界之外那行属于**下一轮**：进了就会把本轮画像说错。"""
        _mkdb(self.db, [_mu(started=100, completed=96),        # 窗口内（暖读）
                        _cold(started=90, completed=80),       # 窗口内（冷读）
                        _cold(started=5, completed=1)], [])    # 窗口后 = 下一轮
        self.assertEqual(
            dsb.db_turn_request_profile(self.db, "sess_a",
                                        S() - 120 * 1000, S() - 60 * 1000),
            (2, 1))

    def test_other_session_never_leaks_in(self):
        _mkdb(self.db, [_cold(), _cold(session="sess_b")], [])
        self.assertEqual(
            dsb.db_turn_request_profile(self.db, "sess_a", S() - 60 * 1000, S()),
            (1, 1))

    def test_missing_bound_returns_none_instead_of_open_window(self):
        """上界缺失 = 本轮还在跑，那种情况走 live_turn_stats。放开上界会把
        之后的调用算进这一轮，宁可不显示。"""
        _mkdb(self.db, [_cold()], [])
        self.assertIsNone(dsb.db_turn_request_profile(
            self.db, "sess_a", S() - 60 * 1000, None))
        self.assertIsNone(dsb.db_turn_request_profile(
            self.db, "sess_a", None, S()))

    def test_guards_return_none(self):
        _mkdb(self.db, [_cold()], [])
        self.assertIsNone(dsb.db_turn_request_profile(
            self.db, "sess_subagent_1", 1, 2 ** 62))
        self.assertIsNone(dsb.db_turn_request_profile(
            os.path.join(self._tmp.name, "nope.sqlite"), "sess_a", 1, 2 ** 62))
        self.assertIsNone(dsb.db_turn_request_profile(
            self.db, "sess_a", S() - 10, S() - 9))   # 窗口内零行

    def test_reuses_callers_connection(self):
        _mkdb(self.db, [_cold()], [])
        conn = dsb._db_connect(self.db)
        try:
            self.assertEqual(
                dsb.db_turn_request_profile(self.db, "sess_a",
                                            S() - 60 * 1000, S(), conn=conn),
                (1, 1))
            conn.execute("SELECT 1").fetchone()      # 仍可用 = 没被关掉
        finally:
            conn.close()


class TestLiveTurnProfile(unittest.TestCase):
    """live 那一路在同一条聚合里带回两个计数（不额外查一次 db）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "t.sqlite")

    def test_live_excludes_background_rows(self):
        """与画像那条查询相反：live 的 token 总量本来就排除后台来源，计数必须
        同总体（否则「n 次请求」解释的不是眼前这串 in/out）。"""
        _mkdb(self.db, [_mu(started=10, completed=8),
                        _cold(started=9, completed=7, source="compact")], [])
        live = dsb.live_turn_stats(self.db, "sess_a", S() - 30 * 1000)
        self.assertEqual((live["modelCallCount"], live["coldReadCount"]), (1, 0))

    def test_live_counts_cold_reads(self):
        _mkdb(self.db, [_mu(started=10, completed=8), _cold(started=6, completed=4)], [])
        live = dsb.live_turn_stats(self.db, "sess_a", S() - 30 * 1000)
        self.assertEqual((live["modelCallCount"], live["coldReadCount"]), (2, 1))

    def test_live_hit_rate_and_cold_count_agree(self):
        """一次冷读把 2 次请求的本轮命中率打到 50% 附近——tooltip 那句正是
        为了让这个「腰斩」看得见。"""
        _mkdb(self.db, [_mu(started=10, completed=8, inp=160000, cache=160000),
                        _cold(started=6, completed=4)], [])
        live = dsb.live_turn_stats(self.db, "sess_a", S() - 30 * 1000)
        hit = live["cacheReadTokens"] / float(live["inputTokens"]) * 100.0
        self.assertLess(hit, 51.0)
        self.assertGreater(hit, 49.0)
        self.assertEqual(live["coldReadCount"], 1)
        self.assertIn("冷读 1 次", dsb.turn_request_profile_text(live))


class TestRequestProfileText(unittest.TestCase):
    """tooltip 文案：缺任一计数就整句不出现（0 会把「不知道」说成「没有冷读」）。"""

    def test_no_counts_no_sentence(self):
        for ts in (None, {}, {"modelCallCount": 2}, {"coldReadCount": 1},
                   {"modelCallCount": None, "coldReadCount": None}):
            self.assertEqual(dsb.turn_request_profile_text(ts), u"")

    def test_garbage_counts_no_sentence(self):
        self.assertEqual(
            dsb.turn_request_profile_text({"modelCallCount": "x",
                                           "coldReadCount": "y"}), u"")

    def test_impossible_counts_no_sentence(self):
        self.assertEqual(dsb.turn_request_profile_text(
            {"modelCallCount": 2, "coldReadCount": 3}), u"")
        self.assertEqual(dsb.turn_request_profile_text(
            {"modelCallCount": 0, "coldReadCount": 0}), u"")

    def test_cold_sentence_defines_the_term(self):
        txt = dsb.turn_request_profile_text(
            {"modelCallCount": 2, "coldReadCount": 1})
        self.assertIn(u"本轮 2 次请求", txt)
        self.assertIn(u"冷读 1 次", txt)
        self.assertIn(u"1%", txt)          # 判据写在句子里，不用翻文档
        self.assertIn(u"拉低", txt)

    def test_all_warm_sentence(self):
        txt = dsb.turn_request_profile_text(
            {"modelCallCount": 4, "coldReadCount": 0})
        self.assertIn(u"本轮 4 次请求", txt)
        self.assertIn(u"无冷读", txt)

    def test_turn_tooltip_always_keeps_the_static_part(self):
        self.assertEqual(dsb.turn_tooltip(None), dsb.TIP_TURN)
        self.assertEqual(dsb.turn_tooltip({}), dsb.TIP_TURN)
        full = dsb.turn_tooltip({"modelCallCount": 2, "coldReadCount": 1})
        self.assertTrue(full.startswith(dsb.TIP_TURN))
        self.assertIn(u"\n本轮 2 次请求", full)


class TestCumulativeHit(unittest.TestCase):
    """累计段 hit：与本轮段、--once 同一个 _hit_rate，不再只有把手能看到。"""

    def test_none_stats(self):
        self.assertIsNone(dsb.cumulative_text(None))

    def test_hit_appended_with_shared_formula(self):
        stats = {"inputTokens": 16653192, "outputTokens": 442900,
                 "cacheReadTokens": 11000000}
        txt = dsb.cumulative_text(stats)
        self.assertTrue(txt.startswith(u"in 16.7M · out 442.9k"))
        self.assertIn(u"hit %.1f%%" % dsb._hit_rate(stats), txt)

    def test_no_input_means_no_hit(self):
        """输入为 0 时 _hit_rate 返回 0.0，画成「hit 0.0%」是把「还没有数据」
        说成「一次都没命中」。"""
        self.assertEqual(
            dsb.cumulative_text({"inputTokens": 0, "outputTokens": 0,
                                 "cacheReadTokens": 0}),
            u"in 0 · out 0")

    def test_show_hit_off(self):
        stats = {"inputTokens": 1000, "outputTokens": 10, "cacheReadTokens": 500}
        self.assertNotIn(u"hit", dsb.cumulative_text(stats, show_hit=False))
        self.assertIn(u"hit", dsb.cumulative_text(stats))


class TestRequestProfileWiring(unittest.TestCase):
    """resolve_turn_status 把两个计数挂到 turn_stats 上（三态都要有）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "t.sqlite")

    def _turn_stats(self, status_state):
        return dsb.resolve_turn_status(status_state, self.db, "sess_a")[2]

    def test_authoritative_turn_row_gets_counts(self):
        """轮次收尾（无新事件 -> idle）：turn_usage 没有逐次分布，按该行自己的
        时间窗回 model_usage 数一遍。"""
        _mkdb(self.db, [_mu(started=100, completed=96),
                        _cold(started=90, completed=80)],
              [_tu(started=102, completed=78)])
        ts = self._turn_stats({})
        self.assertIsNotNone(ts)
        self.assertEqual((ts.get("modelCallCount"), ts.get("coldReadCount")),
                         (2, 1))

    def test_live_turn_gets_counts_from_its_own_query(self):
        _mkdb(self.db, [_cold(started=6, completed=4)], [_tu(started=100, completed=90)])
        # 本轮刚开始（UserPromptSubmit 晚于上一轮收尾），进行中的数字来自
        # live_turn_stats——它一条查询就把两个计数带回来了。
        ts = self._turn_stats({"event": "generating", "hook": "UserPromptSubmit",
                               "ts": S() - 30 * 1000, "session_id": "sess_a",
                               "turn_started_at": S() - 30 * 1000})
        self.assertTrue(ts.get("live"), "应走实时聚合那一路")
        self.assertEqual((ts.get("modelCallCount"), ts.get("coldReadCount")),
                         (1, 1))

    def test_no_usage_rows_leaves_both_keys_absent(self):
        """窗口内数不出调用（老数据 / 全被后台过滤掉）：两个键都不出现，
        tooltip 少一句，而不是显示「0 次请求」。"""
        _mkdb(self.db, [], [_tu(started=102, completed=78)])
        ts = self._turn_stats({})
        self.assertIsNotNone(ts)
        self.assertNotIn("modelCallCount", ts)
        self.assertNotIn("coldReadCount", ts)


class TestRealDbAnchors(unittest.TestCase):
    """真机锚点（缺库即跳过）：口径一旦被人改动，这几条会最先红。"""

    @classmethod
    def setUpClass(cls):
        cls.db = dsb.DB_DEFAULT
        if not os.path.exists(cls.db):
            raise unittest.SkipTest("本机无 ZCode 数据库")
        conn = dsb._db_connect(cls.db)
        try:
            cls.turn = conn.execute(
                "SELECT session_id, started_at, completed_at, "
                "model_request_count, input_tokens, cache_read_input_tokens "
                "FROM turn_usage WHERE COALESCE(session_id,'') NOT LIKE "
                "'sess_subagent_%' AND completed_at IS NOT NULL AND input_tokens>0 "
                "ORDER BY completed_at DESC LIMIT 8").fetchall()
        finally:
            conn.close()

    def test_window_profile_matches_turn_usage_aggregate(self):
        """不过滤 query_source 之后，画像的 n 必须与 turn_usage.model_request_count
        逐轮相等（真机 12/12），否则句子里的「n 次请求」说的不是眼前这串数字。
        这条是那条设计决定的回归闸：谁再把 INTERACTIVE_SOURCE_SQL 加回这个查询，
        含 compact 的那一轮立刻红。"""
        checked = 0
        for sid, started, done, n, _inp, _cr in self.turn:
            prof = dsb.db_turn_request_profile(self.db, sid, started, done)
            self.assertIsNotNone(prof, "session=%s 该轮窗口数不出调用行" % sid)
            self.assertEqual(prof[0], n, "session=%s turn 窗口内调用数对不上" % sid)
            self.assertLessEqual(prof[1], prof[0])
            checked += 1
        self.assertGreaterEqual(checked, 5)


if __name__ == "__main__":
    unittest.main()
