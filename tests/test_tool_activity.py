# -*- coding: utf-8 -*-
"""0.9.2 批 1/2/3：tool_usage 作为「工具中」的权威实时源。

平台约束（实测，非推断）：
  - tool_usage 在工具**开始**时就落行（status='running'、completed_at 为空），
    结束时才补 completed_at —— 是长工具运行期唯一仍在更新的实时源；
  - 一把工具实测能跑 154s+，其间 model_usage 无新行、钩子文件无新事件、
    current-session.json 标记停在 30 秒窗口外，于是「工具中」（900s 硬窗口）
    与「当前是哪个会话」（三路信号全部过窗）两套判定同时失去依据；
  - 进程被强杀时该 running 行永不收尾（本机实测最老一条挂了 27 天），
    所以采信必须按起点封顶（TOOL_LIVE_MAX_MS）。

覆盖：tool_live_ms 采信条件、status_detector 的 A 例外与 C 续期、
db_tool_activity / db_recent_tool_activity 查询语义、会话身份竞争里的 tool
信号、徽标文案与 tooltip（含失败原因）、本轮工具计数分段、
「（最近会话累计）」标注的来源判定。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb

S = dsb.time_ms


def _row(name="Bash", status="running", started_ago=30, completed_ago=None,
         duration=None, exit_code=None, error_type=None,
         session="sess_a", turn="t1"):
    """tool_usage 一行（INSERT 用元组；*_ago 为「距今秒数」）。"""
    return (session, turn, name, status, S() - started_ago * 1000,
            None if completed_ago is None else S() - completed_ago * 1000,
            duration, exit_code, error_type)


def _trow(**kw):
    """db_tool_activity 的返回形状（判定链接受的是这个 dict，不是裸行）。"""
    completed_ago = kw.get("completed_ago")
    return {
        "toolName": kw.get("name", "Bash"),
        "status": kw.get("status", "running"),
        "startedAt": S() - kw.get("started_ago", 30) * 1000,
        "completedAt": None if completed_ago is None else S() - completed_ago * 1000,
        "errorType": kw.get("error_type"),
    }


def _tustat(status="completed", completed_ago=None, started_ago=None):
    """recent_turn_stats 的返回形状（status_detector 读 completedAt/status）。"""
    return {"status": status,
            "startedAt": None if started_ago is None else S() - started_ago * 1000,
            "completedAt": None if completed_ago is None
            else S() - completed_ago * 1000}


def _make_db(path, tool_rows=(), tu_rows=(), mu_rows=(), sessions=("sess_a",)):
    """按真实 schema 的关键列建临时 sqlite（tool_usage / turn_usage /
    model_usage / session）。列名显式写出，避免位置错位。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE IF EXISTS tool_usage")
        conn.execute("DROP TABLE IF EXISTS turn_usage")
        conn.execute("DROP TABLE IF EXISTS model_usage")
        conn.execute("DROP TABLE IF EXISTS session")
        conn.execute(
            "CREATE TABLE tool_usage (session_id TEXT, turn_id TEXT, "
            "tool_name TEXT, status TEXT, started_at INTEGER, "
            "completed_at INTEGER, duration_ms INTEGER, exit_code INTEGER, "
            "error_type TEXT)")
        conn.execute(
            "CREATE TABLE turn_usage (session_id TEXT, turn_id TEXT, "
            "status TEXT, started_at INTEGER, first_token_at INTEGER, "
            "completed_at INTEGER, duration_ms INTEGER, "
            "time_to_first_token_ms INTEGER, model_request_count INTEGER, "
            "tool_call_count INTEGER, tool_error_count INTEGER, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "reasoning_tokens INTEGER, cache_creation_input_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, computed_total_tokens INTEGER, "
            "error_type TEXT, cancelled_by_user INTEGER, "
            "context_exceeded INTEGER, PRIMARY KEY (session_id, turn_id))")
        conn.execute(
            "CREATE TABLE model_usage (id TEXT, session_id TEXT, "
            "query_source TEXT, model_id TEXT, status TEXT, "
            "started_at INTEGER, first_token_at INTEGER, completed_at INTEGER, "
            "duration_ms INTEGER, tool_call_count INTEGER, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "reasoning_tokens INTEGER, cache_creation_input_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, computed_total_tokens INTEGER)")
        conn.execute("CREATE TABLE session (id TEXT, title TEXT, "
                     "time_updated INTEGER)")
        for r in tool_rows:
            conn.execute("INSERT INTO tool_usage (session_id, turn_id, "
                         "tool_name, status, started_at, completed_at, "
                         "duration_ms, exit_code, error_type) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", r)
        for r in tu_rows:
            conn.execute("INSERT INTO turn_usage (session_id, turn_id, status, "
                         "started_at, first_token_at, completed_at, duration_ms, "
                         "time_to_first_token_ms, model_request_count, "
                         "tool_call_count, tool_error_count, input_tokens, "
                         "output_tokens, reasoning_tokens, "
                         "cache_creation_input_tokens, cache_read_input_tokens, "
                         "computed_total_tokens, error_type, cancelled_by_user, "
                         "context_exceeded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,"
                         "?,?,?,?,?,?,?,?)", r)
        for r in mu_rows:
            conn.execute("INSERT INTO model_usage (id, session_id, "
                         "query_source, model_id, status, started_at, "
                         "first_token_at, completed_at, duration_ms, "
                         "tool_call_count, input_tokens, output_tokens, "
                         "reasoning_tokens, cache_creation_input_tokens, "
                         "cache_read_input_tokens, computed_total_tokens) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
        for sid in sessions:
            conn.execute("INSERT INTO session VALUES (?,?,?)",
                         (sid, u"会话" + sid[-1], S()))
        conn.commit()
    finally:
        conn.close()


def _tu(session="sess_a", turn="t1", status="completed", started=90,
        completed=60, duration=30000, ttft=1200, reqs=3, tools=2, errors=0,
        inp=1000, out=800, cache=500, total=2300, err_type=None,
        cancelled=None, ctx=None):
    return (session, turn, status, S() - started * 1000, S() - started * 1000,
            S() - completed * 1000, duration, ttft, reqs, tools, errors,
            inp, out, 0, 0, cache, total, err_type, cancelled, ctx)


def _mu(session="sess_a", source="main_turn", started=40, completed=36,
        duration=4000, out=800, inp=1000, cache=500, tools=1, total=2300,
        model="glm-5.3", status="completed"):
    return (session + "-id", session, source, model, status,
            S() - started * 1000, S() - started * 1000 + 200,
            S() - completed * 1000, duration, tools, inp, out, 0, 0, cache,
            total)


def _state(event, age_s, sid="sess_a", hook=None):
    st = {"event": event, "ts": S() - age_s * 1000, "session_id": sid}
    if hook:
        st["hook"] = hook
    return st


def _tu_obj(**kw):
    """turn_usage 形状的 dict（只带 tooltip 关心的字段）。"""
    return {"errorType": kw.get("err_type"),
            "cancelledByUser": kw.get("cancelled"),
            "contextExceeded": kw.get("ctx"),
            "toolErrorCount": kw.get("errors", 0)}


class TestToolLiveMs(unittest.TestCase):
    """「仍在跑」的采信条件——僵尸 running 行不得算活性。"""

    def test_running_without_completion_is_live(self):
        self.assertEqual(dsb.tool_live_ms(_trow(started_ago=30)), 30 * 1000)

    def test_completed_row_is_not_live(self):
        row = _trow(status="completed", started_ago=30, completed_ago=25)
        self.assertIsNone(dsb.tool_live_ms(row))

    def test_running_status_with_completed_at_is_not_live(self):
        """status 滞后于 completed_at 的行（收尾只写了 completed_at）不算在跑。"""
        row = _trow(status="running", started_ago=30, completed_ago=25)
        self.assertIsNone(dsb.tool_live_ms(row))

    def test_future_started_at_is_not_live(self):
        """起点在将来（时钟回拨）不采信，免得倒着算出负寿命。"""
        self.assertIsNone(dsb.tool_live_ms(_trow(started_ago=-5)))

    def test_zombie_running_row_is_not_live(self):
        """本机实测有挂了 27 天的 running 僵尸行：起点超上限即不采信。"""
        self.assertIsNone(dsb.tool_live_ms(_trow(started_ago=90000)))
        # 上限内一格即采信，边界由常量说话
        edge = int(dsb.TOOL_LIVE_MAX_MS / 1000) - 1
        self.assertIsNotNone(dsb.tool_live_ms(_trow(started_ago=edge)))

    def test_none_and_garbage_are_not_live(self):
        self.assertIsNone(dsb.tool_live_ms(None))
        self.assertIsNone(dsb.tool_live_ms({"status": "running"}))
        self.assertIsNone(dsb.tool_live_ms({"status": "running",
                                            "startedAt": "x"}))


class TestStatusDetectorToolEvidence(unittest.TestCase):
    """步骤 A 的工具例外 + 步骤 C 的工具有效期续期。"""

    def test_tool_row_renews_beyond_hook_window(self):
        """钩子事件超 900s 窗，但期间又开了一把仍未收尾的工具 -> 工具中。"""
        st = _state("tool", 1000, hook="PreToolUse")
        self.assertEqual(dsb.status_detector(st, None, None,
                                             tool_row=_trow(started_ago=300)),
                         ("tool", None))

    def test_stale_tool_row_before_event_does_not_renew(self):
        """工具行起点早于最后一条事件（上一轮遗留）-> 不替本轮说话。"""
        st = _state("generating", 400)
        self.assertEqual(dsb.status_detector(st, None, None,
                                             tool_row=_trow(started_ago=500)),
                         ("idle", None))

    def test_zombie_running_row_capped_by_lifetime(self):
        """起点超 TOOL_LIVE_MAX_MS 的僵尸 running 行 -> 仍按 idle。"""
        st = _state("generating", 400)
        self.assertEqual(dsb.status_detector(st, None, None,
                                             tool_row=_trow(started_ago=700)),
                         ("idle", None))

    def test_completed_tool_row_does_not_renew(self):
        st = _state("generating", 400)
        row = _trow(status="completed", started_ago=300, completed_ago=290)
        self.assertEqual(dsb.status_detector(st, None, None, tool_row=row),
                         ("idle", None))

    def test_tool_row_overrides_stale_generating_event(self):
        """generating 事件超 120s 窗、无心跳，但确有工具在跑 -> 工具中。"""
        st = _state("generating", 400, hook="PostToolUse")
        self.assertEqual(dsb.status_detector(st, None, None,
                                             tool_row=_trow(started_ago=390)),
                         ("tool", None))

    def test_tool_opened_after_turn_closure_means_new_turn(self):
        """turn 行已收尾（权威 idle），但收尾之后又开了一把工具 -> 新一轮在跑。"""
        st = _state("tool", 70)                     # 事件 70s 前
        tu = _tustat("completed", completed_ago=65)  # 收尾 65s 前 -> 权威
        row = _trow(started_ago=40)                  # 收尾之后开的
        self.assertEqual(dsb.status_detector(st, None, tu, tool_row=row),
                         ("tool", None))

    def test_tool_row_before_turn_closure_keeps_authoritative_idle(self):
        """工具行早于 turn 收尾（上一轮遗留）-> 权威收尾结论照常生效。"""
        st = _state("tool", 70)
        tu = _tustat("completed", completed_ago=65)
        row = _trow(started_ago=70)
        self.assertEqual(dsb.status_detector(st, None, tu, tool_row=row),
                         ("idle", None))

    def test_error_outcome_not_overridden_by_old_tool_row(self):
        """失败结论不被遗留 running 行压掉：工具行起点早于收尾时仍报 error。"""
        st = _state("tool", 20, hook="PostToolUseFailure")
        tu = _tustat("error", completed_ago=15)
        row = _trow(started_ago=20)
        self.assertEqual(dsb.status_detector(st, None, tu, tool_row=row),
                         ("error", None))

    def test_no_tool_row_is_091_behaviour(self):
        """不传 tool_row（老调用方 / 表不存在）-> 与 0.9.1 完全一致。"""
        st = _state("tool", 1000)
        self.assertEqual(dsb.status_detector(st, None, None), ("idle", None))


class TestDbToolActivity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "t.sqlite")

    def test_latest_row_by_started_at(self):
        """取起点最新的一把工具（不按 rowid：并发轮次交叠时插入序不可靠）。"""
        rows = [_row(name="Read", status="completed", started_ago=200,
                     completed_ago=190),
                _row(name="Bash", status="running", started_ago=100)]
        _make_db(self.db, tool_rows=rows)
        got = dsb.db_tool_activity(self.db, "sess_a")
        self.assertEqual(got["toolName"], "Bash")
        self.assertEqual(got["status"], "running")
        self.assertEqual(got["startedAt"], rows[1][4])

    def test_subagent_and_missing_are_none(self):
        """子代理会话的工具行不代表主会话在跑（子代理由 Task 工具自己管）。"""
        _make_db(self.db, tool_rows=[_row(session="sess_subagent_x")])
        self.assertIsNone(dsb.db_tool_activity(
            self.db, "sess_subagent_agent_x"))
        self.assertIsNone(dsb.db_tool_activity(self.db, None))

    def test_missing_table_returns_none(self):
        """ZCode 老版本无 tool_usage 表 -> None，不抛（判定链退化为 0.9.1）。"""
        self.assertIsNone(dsb.db_tool_activity(self.db, "sess_a"))
        _make_db(self.db)
        self.assertIsNone(dsb.db_tool_activity(self.db, "sess_a"))

    def test_carries_error_type(self):
        _make_db(self.db, tool_rows=[
            _row(name="Bash", status="error", started_ago=50, completed_ago=40,
                 error_type="tool_failure")])
        got = dsb.db_tool_activity(self.db, "sess_a")
        self.assertEqual(got["errorType"], "tool_failure")
        self.assertIsNone(dsb.tool_live_ms(got))


class TestDbRecentToolActivity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "t.sqlite")

    def test_picks_running_main_session(self):
        rows = [_row(session="sess_subagent_b", name="Bash",
                     status="running", started_ago=10),
                _row(session="sess_c", name="Read", status="completed",
                     started_ago=20, completed_ago=19),
                _row(session="sess_a", name="Task", status="running",
                     started_ago=30)]
        _make_db(self.db, tool_rows=rows)
        sid, ts = dsb.db_recent_tool_activity(self.db)
        self.assertEqual(sid, "sess_a")            # 唯一仍在跑的**主会话**工具
        self.assertEqual(ts, rows[2][4])

    def test_zombie_excluded_by_window(self):
        _make_db(self.db, tool_rows=[_row(started_ago=90000)])
        self.assertEqual(dsb.db_recent_tool_activity(self.db), (None, 0))

    def test_missing_table_is_absent_signal(self):
        self.assertEqual(dsb.db_recent_tool_activity(self.db), (None, 0))


class TestStickyUsesToolSignal(unittest.TestCase):
    """身份竞争：三路信号全过窗时，running 工具行决定「当前是哪个会话」。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "t.sqlite")
        self.data = os.path.join(self._tmp.name, "data")
        os.makedirs(self.data)
        now = S()
        # 钩子事件 100s 前（> STATUS_SIGNAL_FRESH_MS 60s）
        with open(os.path.join(self.data, "status-state.json"), "w",
                  encoding="utf-8") as f:
            f.write('{"version":2,"sessions":{"sess_a":{"event":"tool",'
                    '"ts":%d,"hook":"PreToolUse","session_id":"sess_a"}},'
                    '"order":["sess_a"]}' % (now - 100 * 1000))
        # mark 停在 200s 前（> MARK_FRESH_MS 30s）
        with open(os.path.join(self.data, "current-session.json"), "w",
                  encoding="utf-8") as f:
            f.write('{"session_id":"sess_a","updated_at":%d}' % (now - 200000))
        # model_usage 最后一次完成在 300s 前（> DB_ACTIVE_WINDOW_MS 180s）
        self._db_with_tool(True)

    def _db_with_tool(self, with_tool):
        _make_db(self.db, mu_rows=[_mu(started=320, completed=300)],
                 tool_rows=[_row(name="TaskOutput", status="running",
                                 started_ago=150)] if with_tool else [])

    def _resolve(self, rows=()):
        # 直接调 resolve_session_sticky 不经 resolve_gui_info 的 dsb 透传，
        # patch.object(dsb, "tail_session_resume") 落空会读真实日志目录——
        # 注入确定性 read_resume 让测试与机器环境状态无关。
        return dsb.resolve_session_sticky({}, list(rows), self.data,
                                          self.db,
                                          read_resume=lambda state: (None, 0))

    def test_all_other_signals_expired_tool_signal_wins(self):
        self.assertEqual(self._resolve(), ("sess_a", "tool"))

    def test_without_tool_row_falls_back_to_guess(self):
        """没有工具行时退回 jsonl 猜（0.9.1 行为），标注随之出现。"""
        self._db_with_tool(False)
        rows = [{"sessionId": "sess_z", "ts": S() - 1000}]
        self.assertEqual(self._resolve(rows), ("sess_z", "jsonl"))

    def test_newer_mark_still_beats_tool_signal(self):
        """工具行不抢方向盘：更新的真实信号（mark）到了照常切换。"""
        with open(os.path.join(self.data, "current-session.json"), "w",
                  encoding="utf-8") as f:
            f.write('{"session_id":"sess_b","updated_at":%d}' % (S() - 5000))
        self.assertEqual(self._resolve(), ("sess_b", "mark"))


class TestBadgeTextAndTip(unittest.TestCase):
    """徽标带原因：文案内联工具名，tooltip 给出库里的确凿字段。"""

    def test_badge_text_inlines_running_tool(self):
        ts = {"activeTool": {"name": "Bash", "runningMs": 154 * 1000}}
        self.assertEqual(dsb.status_badge_text("tool", ts),
                         dsb.STATUS_TEXT["tool"] + u"\u00b7Bash")

    def test_badge_text_without_tool_row_unchanged(self):
        for ts in (None, {"activeTool": None}, {"activeTool": {"name": ""}},
                   {}):
            self.assertEqual(dsb.status_badge_text("tool", ts),
                             dsb.STATUS_TEXT["tool"])

    def test_badge_text_skips_overlong_name(self):
        """超长工具名不进徽标：徽标槽永不裁剪，会把对话名挤没。"""
        ts = {"activeTool": {"name": "X" * (dsb.BADGE_TOOL_NAME_MAX + 1),
                             "runningMs": 1000}}
        self.assertEqual(dsb.status_badge_text("tool", ts),
                         dsb.STATUS_TEXT["tool"])

    def test_badge_text_only_for_tool_state(self):
        ts = {"activeTool": {"name": "Bash", "runningMs": 1000}}
        for st in ("generating", "idle", "error", "cancelled"):
            self.assertEqual(dsb.status_badge_text(st, ts),
                             dsb.STATUS_TEXT[st])

    def test_tool_tip_names_tool_and_elapsed(self):
        ts = {"activeTool": {"name": "TaskOutput", "runningMs": 154 * 1000}}
        tip = dsb.status_badge_tip("tool", ts)
        self.assertTrue(tip.startswith(dsb.STATUS_TIPS["tool"]))
        self.assertIn("TaskOutput", tip)
        self.assertIn(u"2 \u5206 34 \u79d2", tip)

    def test_error_tip_carries_error_type(self):
        tip = dsb.status_badge_tip("error", _tu_obj(err_type="provider_auth",
                                                    errors=2))
        self.assertIn("provider_auth", tip)
        self.assertIn(u"\u672c\u8f6e\u5de5\u5177\u62a5\u9519 2 \u6b21", tip)

    def test_error_tip_carries_context_exceeded(self):
        tip = dsb.status_badge_tip("error", _tu_obj(ctx=1))
        self.assertIn(u"\u4e0a\u4e0b\u6587\u5df2\u6ea2\u51fa", tip)

    def test_cancelled_tip_distinguishes_user(self):
        by_user = dsb.status_badge_tip("cancelled", _tu_obj(cancelled=1))
        by_sys = dsb.status_badge_tip("cancelled", _tu_obj(cancelled=0))
        self.assertIn(u"\u624b\u52a8\u6253\u65ad", by_user)
        self.assertIn(u"\u975e\u7528\u6237\u53d6\u6d88", by_sys)

    def test_idle_tip_has_no_stale_reason(self):
        """空闲态不带上一次的失败原因（提示不是档案）。"""
        tip = dsb.status_badge_tip("idle", _tu_obj(err_type="boom", errors=3))
        self.assertNotIn("boom", tip)
        self.assertNotIn(u"\u62a5\u9519", tip)

    def test_missing_fields_degrade_to_static_tip(self):
        self.assertEqual(dsb.status_badge_tip("idle", None),
                         dsb.STATUS_TIPS["idle"])


class TestFormatElapsed(unittest.TestCase):
    def test_three_ranges(self):
        self.assertEqual(dsb.format_elapsed_ms(34 * 1000), u"34 \u79d2")
        self.assertEqual(dsb.format_elapsed_ms(154 * 1000),
                         u"2 \u5206 34 \u79d2")
        self.assertEqual(dsb.format_elapsed_ms(3700 * 1000),
                         u"1 \u65f6 1 \u5206")

    def test_garbage_is_empty_string(self):
        self.assertEqual(dsb.format_elapsed_ms(None), u"")
        self.assertEqual(dsb.format_elapsed_ms("x"), u"")


class TestTurnToolSegments(unittest.TestCase):
    """本轮段的「工具 N / 错 M」：计数缺失时整段不出现，不显示 0。"""

    def _text(self, segs):
        return "".join(t for t, _f, _c in segs)

    def test_counts_present(self):
        segs = dsb.turn_tool_segments({"toolCallCount": 4, "toolErrorCount": 2})
        self.assertEqual(self._text(segs), u" \u00b7 \u5de5\u5177 4 \u00b7 \u9519 2")
        colors = [c for _t, _f, c in segs]
        self.assertEqual(colors.count(dsb.STATUS_COLORS["error"]), 1)

    def test_errors_without_tools_not_shown(self):
        """tool_call_count 为 0 却有报错（库里不该有）-> 不画孤立红字。"""
        self.assertEqual(dsb.turn_tool_segments({"toolCallCount": 0,
                                                 "toolErrorCount": 3}), [])

    def test_missing_fields_add_nothing(self):
        for ts in (None, {}, {"toolCallCount": None}):
            self.assertEqual(dsb.turn_tool_segments(ts), [])

    def test_garbage_counts_add_nothing(self):
        self.assertEqual(dsb.turn_tool_segments({"toolCallCount": "x",
                                                 "toolErrorCount": "y"}), [])


class TestRecentNoteSource(unittest.TestCase):
    """「（最近会话累计）」标注看两头：身份来源或数字来源任一路是 jsonl 就标。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "t.sqlite")
        self.data = os.path.join(self._tmp.name, "data")
        os.makedirs(self.data)
        self.rows = [{"sessionId": "sess_a", "ts": S() - 1000,
                      "inputTokens": 1000, "outputTokens": 800,
                      "cacheReadTokens": 500, "cacheCreationTokens": 0,
                      "reasoningTokens": 0, "avgDurationMs": 100}]

    def _info(self):
        with mock.patch.object(dsb, "tail_session_resume",
                               return_value=(None, 0)):
            return dsb.resolve_gui_info(self.rows, self.data, self.db,
                                        cfg={}, sess_state={})

    def test_db_numbers_with_guessed_identity_still_note(self):
        """身份由 jsonl 猜、数字取自 db：仍要标注（猜来的会话不可信）。"""
        _make_db(self.db, mu_rows=[_mu(started=320, completed=300)])
        info = self._info()
        self.assertEqual(info["source"], "jsonl")
        self.assertTrue(info["recent_note"])

    def test_running_tool_identity_removes_wrong_note(self):
        """0.9.2：running 工具行使身份判定不再过窗退猜 -> 标注消失。"""
        _make_db(self.db, mu_rows=[_mu(started=320, completed=300)],
                 tool_rows=[_row(name="TaskOutput", status="running",
                                 started_ago=150)])
        info = self._info()
        self.assertEqual(info["source"], "tool")
        self.assertEqual(info["stats_source"], "db")
        self.assertFalse(info["recent_note"])


if __name__ == "__main__":
    unittest.main()
