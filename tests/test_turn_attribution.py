# -*- coding: utf-8 -*-
"""0.9.0 批 3 + 批 4：本轮统计归属、实时聚合与 tok/s 口径。

两条实测平台事实决定了这里的全部设计：
  1. turn_usage 行**只在轮次结束时落库**（本机 3741 行里 completed_at 为 NULL
     的行数为 0）——生成中读 recent_turn_stats 拿到的必然是上一轮的数字，而徽标
     同时显示「生成中」，这就是「本轮统计和状态对不上」。改为按本轮起点聚合
     model_usage（每次模型调用完成即落库），得到本轮到当前的真实累计。
  2. model_usage 里后台来源占绝大多数（本机 query_source 计数：subagent 55711 /
     main_turn 9627 / session_title 98 / compact 54）。把后台调用混进 tok/s 会把
     速度拉偏，故所有「本轮/最近一次速度」查询统一走 INTERACTIVE_SOURCE_SQL。
"""
import ast
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "docked_statusbar.py")

S = dsb.time_ms  # 便于按「距今多少秒」构造时刻


def _mkdb(path, mu_rows, tu_rows):
    """按真实 schema 的关键列建临时 sqlite。

    mu_rows: (session_id, query_source, status, started_at, first_token_at,
              completed_at, duration_ms, tool_call_count, input_tokens,
              output_tokens, cache_read_input_tokens, computed_total_tokens)
    tu_rows: (session_id, turn_id, status, started_at, completed_at,
              duration_ms, tool_call_count, tool_error_count, input_tokens,
              output_tokens, cache_read_input_tokens, computed_total_tokens,
              error_type, cancelled_by_user, context_exceeded)
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE IF EXISTS model_usage")
        conn.execute("DROP TABLE IF EXISTS turn_usage")
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
            "DROP TABLE IF EXISTS tool_usage")
        # 真实库里 tool_usage 在工具开始时就落行（status='running'、
        # completed_at 为空）——0.9.2 的「工具中」权威证据源。
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


def _mu(session="sess_a", source="main_turn", started=60, completed=56,
        duration=4000, out=800, inp=1000, cache=500, tools=0, total=2300,
        status="completed"):
    """model_usage 一行（started/completed 为「距今秒数」）。"""
    return (session, source, status, S() - started * 1000,
            S() - started * 1000 + 200, S() - completed * 1000, duration,
            tools, inp, out, cache, total)


def _tu(session="sess_a", turn="t_prev", status="completed", started=60,
        completed=56, duration=4000, out=800, inp=1000, cache=500,
        tools=2, errors=0, total=2300, err_type=None, cancelled=None,
        ctx=None):
    return (session, turn, status, S() - started * 1000, S() - completed * 1000,
            duration, tools, errors, inp, out, cache, total,
            err_type, cancelled, ctx)


class TestLiveTurnStats(unittest.TestCase):
    """本轮实时聚合：只算本轮窗口内的交互来源调用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "t.sqlite")

    def _turn_started(self):
        return S() - 10 * 1000          # 本轮起点：10s 前

    def test_aggregates_only_rows_after_turn_start(self):
        _mkdb(self.db_path,
              [_mu(started=60, completed=56, out=800, inp=1000),   # 上一轮
               _mu(started=4, completed=2, out=300, inp=400, cache=100,
                   tools=3, total=800)],                            # 本轮
              [])
        st = self._turn_started()
        live = dsb.live_turn_stats(self.db_path, "sess_a", st)
        self.assertIsNotNone(live)
        self.assertTrue(live["live"])
        self.assertEqual(live["status"], "running")
        self.assertEqual(live["outputTokens"], 300)      # 不含上一轮的 800
        self.assertEqual(live["inputTokens"], 400)
        self.assertEqual(live["cacheReadTokens"], 100)
        self.assertEqual(live["computedTotalTokens"], 800)
        self.assertEqual(live["toolCallCount"], 3)
        self.assertEqual(live["modelCallCount"], 1)
        self.assertIsNone(live["turn_id"])
        self.assertIsNone(live["completedAt"])
        # TTFT 是**时长**（first_token_at - started_at），不是绝对落库时刻——
        # 与 recent_turn_stats 的同名键同义，渲染层才能共用一条路径
        self.assertEqual(live["timeToFirstTokenMs"], 200)
        # 耗时 = now - 本轮首行 started_at（不是行内 duration 之和）
        self.assertAlmostEqual(live["durationMs"] / 1000.0, 4.0, delta=1.0)

    def test_background_sources_excluded(self):
        """本轮内只有 subagent / compact / session_title 调用 -> 无本轮数据。

        这些行是后台任务产生的，计进「本轮」就是「我什么都没干，本轮统计却
        在涨」。
        """
        st = self._turn_started()
        _mkdb(self.db_path,
              [_mu(started=4, completed=2, out=999, source="subagent"),
               _mu(started=3, completed=2, out=999, source="compact"),
               _mu(started=2, completed=1, out=999, source="session_title")],
              [])
        self.assertIsNone(dsb.live_turn_stats(self.db_path, "sess_a", st))

    def test_other_sessions_excluded(self):
        _mkdb(self.db_path,
              [_mu(started=4, completed=2, out=300),
               _mu(session="sess_b", started=3, completed=1, out=7000)], [])
        live = dsb.live_turn_stats(self.db_path, "sess_a", self._turn_started())
        self.assertEqual(live["outputTokens"], 300)
        self.assertEqual(live["modelCallCount"], 1)

    def test_no_rows_in_turn_returns_none(self):
        _mkdb(self.db_path, [_mu(started=60, completed=56)], [])
        self.assertIsNone(
            dsb.live_turn_stats(self.db_path, "sess_a", S() - 5 * 1000))

    def test_guards_return_none(self):
        _mkdb(self.db_path, [_mu()], [])
        self.assertIsNone(
            dsb.live_turn_stats(self.db_path, "sess_subagent_1", self._turn_started()))
        self.assertIsNone(
            dsb.live_turn_stats(os.path.join(self._tmp.name, "nope.sqlite"),
                                "sess_a", self._turn_started()))

    def test_since_ms_none_means_no_lower_bound(self):
        """0.9.1：第 3 参改为纯下界，None = 不设界（会话首轮）。

        0.9.0 在这里返回 None——因为它把「没有钩子起点」当成无法定窗，
        结果是 v1 写端与首轮永远拿不到实时数据。
        """
        _mkdb(self.db_path, [_mu()], [])
        live = dsb.live_turn_stats(self.db_path, "sess_a", None)
        self.assertIsNotNone(live)
        self.assertEqual(live["outputTokens"], 800)

    def test_keys_match_recent_turn_stats(self):
        """与 recent_turn_stats 同键（渲染层共用一条代码路径的前提）。

        例外只有 live 独有的两个请求级计数：model_usage 逐次调用才数得出「几次
        请求 / 几次冷读」，turn_usage 一行汇总里没有。resolve_turn_status 会用
        db_turn_request_profile 给 turn_usage 那两路补上同名键（见
        test_cache_hit_explain.TestRequestProfileWiring），到这里才真正同形。
        """
        _mkdb(self.db_path, [_mu(started=4, completed=2)],
              [_tu()])
        live = dsb.live_turn_stats(self.db_path, "sess_a", self._turn_started())
        recent = dsb.recent_turn_stats(self.db_path, "sess_a")
        _REQ = {"live", "modelCallCount", "coldReadCount"}
        self.assertEqual(set(recent) - _REQ, set(live) - _REQ)
        self.assertEqual(live["modelCallCount"], 1)
        self.assertEqual(live["coldReadCount"], 0)   # cache 500 / input 1000 = 50%


class TestSpeedScope(unittest.TestCase):
    """批 4：tok/s 限定本轮窗口 + 排除后台来源。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "t.sqlite")

    def test_since_ms_restricts_to_current_turn(self):
        """上一轮 200 tok/s、本轮 300 tok/s -> 本轮窗口内取 300（不是 200）。"""
        _mkdb(self.db_path,
              [_mu(started=60, completed=56, out=800, duration=4000),    # 200
               _mu(started=4, completed=2, out=300, duration=1000)], []) # 300
        self.assertEqual(
            dsb.db_latest_speed(self.db_path, "sess_a",
                                since_ms=S() - 10 * 1000), 300.0)
        # 不设窗口时仍是全库最近一条（向后兼容旧签名）
        self.assertEqual(dsb.db_latest_speed(self.db_path, "sess_a"), 300.0)

    def test_no_rows_in_window_returns_none(self):
        """本轮尚无完成的调用 -> None（渲染层显示「生成中…」，不拿上一轮冒充）。"""
        _mkdb(self.db_path, [_mu(started=60, completed=56)], [])
        self.assertIsNone(dsb.db_latest_speed(self.db_path, "sess_a",
                                              since_ms=S() - 5 * 1000))

    def test_background_rows_never_supply_speed(self):
        """本轮内只有 compact 调用 -> None（不得用后台调用的短输出充数）。"""
        _mkdb(self.db_path,
              [_mu(started=4, completed=2, out=60, duration=1000,
                   source="compact"),
               _mu(started=4, completed=3, out=60, duration=1000,
                   source="session_title"),
               _mu(started=4, completed=3, out=60, duration=1000,
                   source="subagent")],
              [])
        self.assertIsNone(dsb.db_latest_speed(self.db_path, "sess_a",
                                              since_ms=S() - 10 * 1000))

    def test_session_avg_speed_also_excludes_background(self):
        """会话平均 tok/s 同口径：只算交互来源，且不被别的会话污染。"""
        _mkdb(self.db_path,
              [_mu(started=60, completed=56, out=800, duration=4000),      # 200
               _mu(started=20, completed=16, out=4000, duration=4000,
                   source="subagent")],                                    # 1000
              [])
        recent, avg = dsb.db_session_speed(self.db_path, "sess_a")
        self.assertEqual(recent, 200.0)
        self.assertEqual(avg, 200.0)


class TestSpeedWhileBusyFamily(unittest.TestCase):
    """0.9.1：速度覆盖整个 busy 族（工具中也取），非 busy 仍交回 None。

    实测一轮里 PreToolUse/PostToolUse 交替（本机单轮 model_request_count
    p50=7 / p95=67），0.9.0 只在 status=='generating' 时取速度，于是工具执行
    期间（往往占一轮的大半时间）数字整段消失，用户读作「速度没显示」。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "t.sqlite")
        self.turn_started = S() - 30 * 1000

    def _state(self, event="tool", age_s=2):
        return {"event": event, "ts": S() - age_s * 1000,
                "hook": "PreToolUse" if event == "tool" else "PostToolUse",
                "turn_started_at": self.turn_started, "session_id": "sess_a"}

    def test_tool_state_still_reports_turn_speed(self):
        _mkdb(self.db_path,
              [_mu(started=60, completed=56, out=800, duration=4000),    # 上一轮 200
               _mu(started=10, completed=8, out=600, duration=2000)],    # 本轮 300
              [_tu(status="completed", started=90, completed=60)])
        status, spd, _ts = dsb.resolve_turn_status(
            self._state("tool"), self.db_path, "sess_a")
        self.assertEqual(status, "tool")
        self.assertEqual(spd, 300.0)   # 本轮速度，与 generating 时同值

    def test_tool_state_without_row_in_window_is_none(self):
        """本轮还没有任何完成的调用 -> 工具中也不得拿上一轮速度充数。"""
        _mkdb(self.db_path, [_mu(started=60, completed=56, out=800,
                                 duration=4000)], [_tu()])
        status, spd, _ts = dsb.resolve_turn_status(
            self._state("tool"), self.db_path, "sess_a")
        self.assertEqual(status, "tool")
        self.assertIsNone(spd)

    def test_idle_state_returns_none_for_speed_hold_layer(self):
        """非 busy 一律 None：短时保留最后已知值是渲染层（speed_hold）的事，
        resolve_turn_status 不掺和，否则 --once 与 GUI 的口径会分叉。"""
        _mkdb(self.db_path, [_mu(started=10, completed=8, out=600,
                                 duration=2000)],
              [_tu(status="completed", started=30, completed=8, out=600)])
        status, spd, _ts = dsb.resolve_turn_status(
            self._state("idle"), self.db_path, "sess_a")
        self.assertEqual(status, "idle")
        self.assertIsNone(spd)


class TestTurnWindowFromDbClock(unittest.TestCase):
    """0.9.1：本轮窗口左沿改用「上一轮 turn 行的收尾时刻」，不再用钩子 ts。

    实测（本机一个主会话轮次，id 不外抄）：UserPromptSubmit 落盘的
    turn_started_at 比该轮 turn_usage/model_usage 的 started_at 晚 **17.0s**，
    而 0.9.0 的归属容差 TURN_ATTRIB_SLACK_MS 只有 15s。滞后量随机器负载变化
    （钩子是宿主异步拉起的 python 进程），任何固定容差都会踩穿，表现为：
      - 本轮首次调用被排除出实时聚合窗口 -> 明明在跑本轮却标「· 上一轮」；
      - 速度同样晚一个调用才出现（本轮窗口里没有行）；
      - 本轮权威 turn 行被误标 stale_turn。
    改用 DB 自身时钟可证的边界后，这些都不再依赖两个时钟的对齐。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "t.sqlite")

    def _state(self, event="generating", age_s=2, turn_started_s=10):
        """turn_started_s：UserPromptSubmit 落盘时刻距今的秒数。"""
        return {"event": event, "ts": S() - age_s * 1000, "hook": "UserPromptSubmit",
                "turn_started_at": None if turn_started_s is None
                else S() - turn_started_s * 1000,
                "session_id": "sess_a"}

    def test_hook_lag_20s_still_aggregates_this_turns_first_call(self):
        """上一轮 40s 前收尾；本轮首次调用 30s 前开始（比钩子落盘早 20s）。"""
        _mkdb(self.db_path,
              [_mu(started=90, completed=85, out=800, duration=5000),   # 上上轮
               _mu(started=30, completed=25, out=300, duration=5000)],  # 本轮首次
              [_tu(turn="t_prev", status="completed", started=120,
                   completed=40, out=800)])
        status, spd, ts = dsb.resolve_turn_status(
            self._state(turn_started_s=10), self.db_path, "sess_a")
        self.assertEqual(status, "generating")
        self.assertTrue(ts["live"], "本轮有已完成的调用，不该再显示上一轮")
        self.assertEqual(ts["outputTokens"], 300)
        self.assertEqual(spd, 60.0)          # 300/5s，本轮速度

    def test_closed_row_closing_after_prompt_is_authoritative(self):
        """行 started_at 早于钩子 ts 20s，但收尾晚于 ts -> 就是本轮的行，不标 stale。"""
        _mkdb(self.db_path, [_mu(started=30, completed=2, out=300)],
              [_tu(turn="t_cur", status="completed", started=30, completed=2,
                   out=300)])
        status, _spd, ts = dsb.resolve_turn_status(
            self._state(event="idle", age_s=1, turn_started_s=10),
            self.db_path, "sess_a")
        self.assertEqual(status, "idle")
        self.assertNotIn("stale_turn", ts or {})
        self.assertEqual(ts["outputTokens"], 300)

    def test_row_closed_before_prompt_is_stale(self):
        """收尾时刻早于本轮起点 -> 本轮没留下行，仍须标「上一轮」。"""
        _mkdb(self.db_path, [_mu(started=90, completed=85, out=800)],
              [_tu(turn="t_prev", status="completed", started=120, completed=85,
                   out=800)])
        status, _spd, ts = dsb.resolve_turn_status(
            self._state(event="idle", age_s=1, turn_started_s=10),
            self.db_path, "sess_a")
        self.assertEqual(status, "idle")
        self.assertTrue(ts["stale_turn"])

    def test_v1_writer_without_turn_started_still_gets_live(self):
        """写端没有 turn_started_at（v1 老文件）时也能实时聚合：左沿来自 DB。"""
        _mkdb(self.db_path,
              [_mu(started=30, completed=25, out=300, duration=5000)],
              [_tu(turn="t_prev", status="completed", started=120,
                   completed=40, out=800)])
        status, spd, ts = dsb.resolve_turn_status(
            self._state(turn_started_s=None), self.db_path, "sess_a")
        self.assertEqual(status, "generating")
        self.assertTrue(ts["live"])
        self.assertEqual(spd, 60.0)

    def test_first_turn_of_session_has_no_lower_bound(self):
        """会话首轮：还没有任何 turn 行 -> 该会话的交互行全属本轮。"""
        _mkdb(self.db_path,
              [_mu(started=60, completed=55, out=200, duration=5000),
               _mu(started=20, completed=15, out=300, duration=5000)], [])
        status, spd, ts = dsb.resolve_turn_status(
            self._state(turn_started_s=10), self.db_path, "sess_a")
        self.assertEqual(status, "generating")
        self.assertTrue(ts["live"])
        self.assertEqual(ts["modelCallCount"], 2)     # 首轮包含全部已落库行
        self.assertEqual(spd, 60.0)                   # 最近一条 = 300/5s

    def test_live_turn_stats_param_is_a_plain_lower_bound(self):
        """live_turn_stats 的第 3 参改名 since_ms：语义就是「不早于此时刻」。"""
        import inspect
        params = list(inspect.signature(dsb.live_turn_stats).parameters)
        self.assertEqual(params[:3], ["db_path", "session_id", "since_ms"])
        _mkdb(self.db_path, [_mu(started=30, completed=25, out=300),
                             _mu(started=90, completed=85, out=800)], [])
        self.assertEqual(
            dsb.live_turn_stats(self.db_path, "sess_a", S() - 40 * 1000)
            ["outputTokens"], 300)
        self.assertIsNone(
            dsb.live_turn_stats(self.db_path, "sess_a", S() - 5 * 1000))

    def test_fixed_attrib_slack_is_gone(self):
        """防回流：不得再引入「钩子 ts - 固定容差」这类归属判定。

        滞后量随负载变化（stats.log 里 Stop ≈0.5s、UserPromptSubmit 实测 17s），
        任何常量都会在某些机器负载下踩穿，只能改成两侧同用 DB 时钟。
        """
        self.assertFalse(hasattr(dsb, "TURN_ATTRIB_SLACK_MS"))
        with open(_SRC, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("TURN_ATTRIB_SLACK_MS", src)


class TestModelActivityHeartbeat(unittest.TestCase):
    """心跳改用 model_usage 完成时刻，取代 session.time_updated。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "t.sqlite")

    def test_returns_max_completed_at_of_interactive_rows(self):
        _mkdb(self.db_path,
              [_mu(started=60, completed=56),
               _mu(started=10, completed=7, source="compact"),   # 后台，忽略
               _mu(started=8, completed=3)], [])
        got = dsb.db_session_model_activity_ts(self.db_path, "sess_a")
        self.assertAlmostEqual(got / 1000.0, S() / 1000.0 - 3, delta=1.0)

    def test_since_ms_filters_older_activity(self):
        """只认「本事件之后」完成的调用：早于 since - 宽限的心跳不算。"""
        _mkdb(self.db_path, [_mu(started=260, completed=200)], [])
        self.assertIsNone(dsb.db_session_model_activity_ts(
            self.db_path, "sess_a", since_ms=S() - 20 * 1000))
        # 落在宽限窗口（ACTIVITY_GRACE_MS 90s）内 -> 仍算心跳
        _mkdb(self.db_path, [_mu(started=60, completed=56)], [])
        got = dsb.db_session_model_activity_ts(
            self.db_path, "sess_a", since_ms=S() - 5 * 1000)
        self.assertAlmostEqual(got / 1000.0, S() / 1000.0 - 56, delta=1.0)

    def test_guards(self):
        _mkdb(self.db_path, [_mu()], [])
        self.assertIsNone(
            dsb.db_session_model_activity_ts(self.db_path, "sess_subagent_1"))
        self.assertIsNone(dsb.db_session_model_activity_ts(self.db_path, None))
        self.assertIsNone(dsb.db_session_model_activity_ts(
            os.path.join(self._tmp.name, "nope.sqlite"), "sess_a"))

    def test_time_updated_reader_is_gone(self):
        """防回流：读端不得再有 session.time_updated 心跳函数。"""
        self.assertFalse(hasattr(dsb, "db_session_activity_ts"))


class TestResolveTurnStatusTriState(unittest.TestCase):
    """resolve_turn_status 的三态：live / stale_turn / 无标记。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "t.sqlite")
        self.turn_started = S() - 10 * 1000

    def _state(self, event="generating", age_s=2, hook="PostToolUse"):
        return {"event": event, "ts": S() - age_s * 1000, "hook": hook,
                "turn_started_at": self.turn_started, "session_id": "sess_a"}

    def test_generating_with_model_rows_is_live(self):
        """生成中 + 本轮已有完成的模型调用 -> 本轮实时聚合 + 本轮速度。"""
        _mkdb(self.db_path,
              [_mu(started=60, completed=56, out=800, duration=4000),
               _mu(started=4, completed=2, out=300, duration=1000)],
              [_tu()])
        status, spd, ts = dsb.resolve_turn_status(
            self._state(), self.db_path, "sess_a")
        self.assertEqual(status, "generating")
        self.assertTrue(ts["live"])
        self.assertNotIn("stale_turn", ts)
        self.assertEqual(ts["outputTokens"], 300)
        self.assertEqual(spd, 300.0)   # 本轮速度，不是上一轮的 200

    def test_generating_before_first_model_call_is_stale(self):
        """生成中但本轮尚无 model_usage 行 -> 明说展示的是上一轮。"""
        _mkdb(self.db_path, [_mu(started=60, completed=56, out=800)], [_tu()])
        status, spd, ts = dsb.resolve_turn_status(
            self._state(), self.db_path, "sess_a")
        self.assertEqual(status, "generating")
        self.assertTrue(ts["stale_turn"])
        self.assertNotIn("live", ts)
        self.assertEqual(ts["outputTokens"], 800)   # 上一轮的行
        self.assertIsNone(spd)                      # 本轮无速度 -> 占位「生成中…」

    def test_closed_turn_is_authoritative_unmarked(self):
        """Stop 收尾 + turn 行属本轮 -> 权威本轮数据（无 live / stale 标记）。"""
        _mkdb(self.db_path,
              [_mu(started=8, completed=1, out=300, duration=1000)],
              [_tu(status="completed", started=10, completed=1, out=300)])
        status, spd, ts = dsb.resolve_turn_status(
            self._state(event="idle", age_s=1, hook="Stop"),
            self.db_path, "sess_a")
        self.assertEqual(status, "idle")
        self.assertIsNone(spd)
        self.assertNotIn("live", ts or {})
        self.assertNotIn("stale_turn", ts or {})
        self.assertEqual(ts["outputTokens"], 300)

    def test_idle_with_row_from_earlier_turn_is_stale(self):
        """本轮在首次模型调用前就被打断 -> 展示的行属上一轮，须标注。"""
        _mkdb(self.db_path, [_mu(started=60, completed=56, out=800)], [_tu()])
        status, _spd, ts = dsb.resolve_turn_status(
            self._state(event="idle", age_s=1, hook="Stop"),
            self.db_path, "sess_a")
        self.assertEqual(status, "idle")
        self.assertTrue(ts["stale_turn"])

    def test_error_outcome_reported_with_authoritative_row(self):
        """权威收尾：error 轮次给出 error 徽标，数字就是本轮的。"""
        _mkdb(self.db_path,
              [_mu(started=8, completed=2, out=120, duration=2000,
                   status="error")],
              [_tu(status="error", started=10, completed=2, out=120)])
        status, _spd, ts = dsb.resolve_turn_status(
            self._state(event="generating", age_s=4, hook="PostToolUse"),
            self.db_path, "sess_a")
        self.assertEqual(status, "error")
        self.assertEqual(ts["status"], "error")
        self.assertNotIn("stale_turn", ts)

    def test_unknown_session_returns_idle_placeholder(self):
        """会话未识别时不猜：返回 ('idle', None, None) 占位。"""
        _mkdb(self.db_path, [_mu()], [_tu()])
        self.assertEqual(
            dsb.resolve_turn_status(self._state(), self.db_path, None),
            ("idle", None, None))

    def test_no_status_state_falls_back_to_last_turn(self):
        """无钩子文件（老安装/未触发钩子）：idle + 最近一轮行，无标记。"""
        _mkdb(self.db_path, [_mu()], [_tu()])
        status, spd, ts = dsb.resolve_turn_status(None, self.db_path, "sess_a")
        self.assertEqual((status, spd), ("idle", None))
        self.assertEqual(ts["outputTokens"], 800)


class TestSourceFilterContract(unittest.TestCase):
    """静态契约：本轮/速度口径必须走 INTERACTIVE_SOURCE_SQL。

    会话累计（db_aggregate_session）刻意**不**过滤——它要对得上 DB 里该会话
    的全部 token（verify_against_sqlite 的 MATCH ALL 口径），两者语义不同，
    因此这里反向锁定它不得被顺手加上过滤。
    """

    @staticmethod
    def _body(name):
        with open(_SRC, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        return ast.get_source_segment(src, fn) or ""

    def test_speed_and_live_queries_filter_sources(self):
        for name in ("db_latest_speed", "db_session_speed", "live_turn_stats",
                     "db_session_model_activity_ts"):
            self.assertIn("INTERACTIVE_SOURCE_SQL", self._body(name),
                          "%s 未排除后台来源，tok/s 与本轮统计会被污染" % name)

    def test_session_cumulative_keeps_all_sources(self):
        body = self._body("db_aggregate_session")
        self.assertNotIn("INTERACTIVE_SOURCE_SQL", body,
                         "会话累计不得过滤来源：口径要等于该会话全部落库 token")


if __name__ == "__main__":
    unittest.main()
