# -*- coding: utf-8 -*-
"""Stage 2: SQLite 拍级连接复用——db 查询 helper 的可选 conn 参数测试。

Task 2.1（本文件初始范围）：
  - helper 传入共享 conn：结果正确且 conn 不被关闭（复用语义）；
  - 不传 conn（自开自关路径）结果正确（向后兼容）。
Task 2.2 追加：一拍数据组装链路（resolve_gui_info + resolve_turn_status）
  只开一次连接的计数测试（R4）。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


def _make_fixture(dirpath):
    """最小 schema + 样例行的临时 sqlite（model_usage/session/turn_usage/
    tool_usage）。

    返回 (db_path, data_dir, started_at)。data_dir 为空目录（无 mark/jsonl）。
    """
    db_path = os.path.join(dirpath, "t.sqlite")
    data_dir = os.path.join(dirpath, "data")
    os.makedirs(data_dir, exist_ok=True)
    now = dsb.time_ms()
    started_at = now - 10 * 1000
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE model_usage (session_id TEXT, started_at INTEGER, "
            "model_id TEXT, status TEXT, query_source TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, "
            "reasoning_tokens INTEGER, duration_ms INTEGER, tool_call_count INTEGER)")
        conn.execute("CREATE TABLE session (id TEXT, title TEXT, time_updated INTEGER)")
        conn.execute(
            "CREATE TABLE turn_usage (turn_id TEXT, session_id TEXT, status TEXT, "
            "started_at INTEGER, completed_at INTEGER, duration_ms INTEGER, "
            "time_to_first_token_ms INTEGER, tool_call_count INTEGER, "
            "tool_error_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, computed_total_tokens INTEGER, "
            "error_type TEXT, cancelled_by_user INTEGER, context_exceeded INTEGER)")
        conn.execute(
            "CREATE TABLE tool_usage (session_id TEXT, turn_id TEXT, "
            "tool_name TEXT, status TEXT, started_at INTEGER, "
            "completed_at INTEGER, duration_ms INTEGER, exit_code INTEGER, "
            "error_type TEXT)")
        conn.execute(
            "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sess_main", started_at, "model_a", "completed", "main_turn",
             100, 200, 50, 0, 10, 2000, 3))
        conn.execute("INSERT INTO session VALUES (?,?,?)",
                     ("sess_main", "My Title", now))
        conn.execute(
            "INSERT INTO turn_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("turn_1", "sess_main", "completed", started_at, now - 8 * 1000,
             2000, 500, 3, 0, 100, 200, 50, 350, None, None, None))
        # 已收尾的工具行：不参与 0.9.2 的 running 身份信号（正是该断言要的语义）
        conn.execute(
            "INSERT INTO tool_usage VALUES (?,?,?,?,?,?,?,?,?)",
            ("sess_main", "turn_1", "Bash", "completed", now - 9 * 1000,
             now - 8 * 1000, 1000, 0, None))
        conn.commit()
    finally:
        conn.close()
    return db_path, data_dir, started_at


class TestDbHelpersConnParam(unittest.TestCase):
    """Task 2.1: db 查询 helper 的可选 conn 参数。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        (self.db_path, self.data_dir,
         self.started_at) = _make_fixture(self._tmp.name)

    def test_helpers_accept_shared_conn(self):
        """传入共享 conn：全部 9 个 helper 结果正确且 conn 不被关闭。"""
        shared = dsb._db_connect(self.db_path)
        try:
            self.assertIsNotNone(shared)
            self.assertEqual(
                dsb.db_latest_model_id(self.db_path, "sess_main", conn=shared),
                "model_a")
            self.assertEqual(
                dsb.db_session_title(self.db_path, "sess_main", conn=shared),
                "My Title")
            self.assertEqual(
                dsb.db_tool_activity(self.db_path, "sess_main",
                                     conn=shared)["toolName"], "Bash")
            self.assertEqual(
                dsb.db_recent_tool_activity(self.db_path, conn=shared),
                (None, 0))  # 夹具里唯一一把工具已收尾 -> 无 running 身份信号
            self.assertEqual(
                dsb.db_recent_session_activity(self.db_path, conn=shared),
                ("sess_main", self.started_at))
            self.assertEqual(
                dsb.db_latest_session_id(self.db_path, conn=shared),
                ("sess_main", None))
            agg = dsb.db_aggregate_session(self.db_path, "sess_main",
                                           conn=shared)
            self.assertEqual(agg["inputTokens"], 100)
            self.assertEqual(agg["outputTokens"], 200)
            recent, avg = dsb.db_session_speed(self.db_path, "sess_main",
                                               conn=shared)
            self.assertEqual(recent, 100.0)  # 200 tok / 2s
            self.assertEqual(avg, 100.0)     # 200/2000*1000
            tu = dsb.recent_turn_stats(self.db_path, "sess_main", conn=shared)
            self.assertEqual(tu["turn_id"], "turn_1")
            self.assertEqual(tu["inputTokens"], 100)
            self.assertEqual(dsb.db_latest_speed(self.db_path, conn=shared),
                             100.0)
            # conn 未被 helper 关闭（own=False 复用语义）
            shared.execute("SELECT 1").fetchone()
        finally:
            shared.close()

    def test_helpers_backward_compat_without_conn(self):
        """测试 B：不传 conn（自开自关路径）结果正确。"""
        self.assertEqual(dsb.db_latest_model_id(self.db_path, "sess_main"),
                         "model_a")
        self.assertEqual(dsb.db_session_title(self.db_path, "sess_main"),
                         "My Title")
        agg = dsb.db_aggregate_session(self.db_path, "sess_main")
        self.assertEqual(agg["inputTokens"], 100)
        recent, avg = dsb.db_session_speed(self.db_path, "sess_main")
        self.assertEqual(recent, 100.0)
        # 不存在的 db：静默降级
        self.assertIsNone(
            dsb.db_latest_model_id(os.path.join(self._tmp.name, "nope.sqlite"),
                                   "sess_main"))


class TestDbLatestSpeedSessionFilter(unittest.TestCase):
    """新行为锁定：db_latest_speed 新增 session_id 参数（按会话过滤速度）。

    背景：该 helper 签名改为 db_latest_speed(db_path, session_id=None,
    conn=None)——session_id 非 None 时只取**该会话**最近一条 completed 行，
    避免生成中把别的会话的速度显示成本会话的；不传（None）保持旧行为
    （全库最近一条）。同时防回退：返回值从无消费者的死函数
    db_latest_model_usage_status 已被删除，不得被误加回来。
    """

    def _make_db(self):
        """最小 model_usage 临时 sqlite：两个主会话各有 completed 行。

        sess_a: 100 tok / 1000ms -> 100.0 tok/s（较早）
        sess_b: 300 tok / 1000ms -> 300.0 tok/s（较新，即全库最近一条）
        """
        db_path = os.path.join(self._tmp.name, "two_sessions.sqlite")
        now = dsb.time_ms()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE model_usage (session_id TEXT, started_at INTEGER, "
                "model_id TEXT, status TEXT, query_source TEXT, "
                "input_tokens INTEGER, output_tokens INTEGER, "
                "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, "
                "reasoning_tokens INTEGER, duration_ms INTEGER, tool_call_count INTEGER)")
            conn.execute(
                "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("sess_a", now - 5 * 1000, "model_a", "completed", "main_turn",
                 10, 100, 0, 0, 0, 1000, 0))
            conn.execute(
                "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("sess_b", now - 1 * 1000, "model_b", "completed", "main_turn",
                 10, 300, 0, 0, 0, 1000, 0))
            conn.commit()
        finally:
            conn.close()
        return db_path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = self._make_db()

    def test_session_filter_returns_own_speed(self):
        """过滤生效：各会话返回自己的速度，不跨会话取最新（sess_a 不得得 300）。"""
        self.assertEqual(dsb.db_latest_speed(self.db_path, "sess_a"), 100.0)
        self.assertEqual(dsb.db_latest_speed(self.db_path, "sess_b"), 300.0)

    def test_no_session_id_keeps_global_latest(self):
        """向后兼容：不传 session_id 仍返回全库最近一条（sess_b 的速度）。"""
        self.assertEqual(dsb.db_latest_speed(self.db_path), 300.0)

    def test_unknown_session_returns_none(self):
        """未知会话：返回 None，不退化成别的会话的最近一条。"""
        self.assertIsNone(dsb.db_latest_speed(self.db_path, "sess_no_such"))

    def test_dead_helper_stays_removed(self):
        """防回退：无生产调用方的死函数不得被误加回模块。"""
        self.assertFalse(hasattr(dsb, "db_latest_model_usage_status"))


class TestTickSingleConnection(unittest.TestCase):
    """Task 2.2: 一拍数据组装链路只开一次连接（R4）。

    refresh_stats 是 run_gui 的嵌套函数不可直接单测，改测同一数据组装链路：
    resolve_gui_info(sess_state, db_conn=tick_conn) + resolve_turn_status(conn=tick_conn)。
    monkeypatch dsb._db_connect 计数包装（内部调原函数）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        (self.db_path, self.data_dir,
         self.started_at) = _make_fixture(self._tmp.name)

    def _counting_connect(self):
        counter = {"n": 0}
        orig_connect = dsb._db_connect

        def counting(p):
            counter["n"] += 1
            return orig_connect(p)

        return counter, counting

    def test_gui_info_tick_single_connection(self):
        """测试 A：tick_conn 由调用方开一次，gui_info + turn_status 链路零新开。"""
        counter, counting = self._counting_connect()
        with mock.patch.object(dsb, "_db_connect", side_effect=counting), \
             mock.patch.object(dsb, "tail_session_resume",
                               return_value=(None, 0)):
            tick_conn = dsb._db_connect(self.db_path)  # 唯一一次连接
            try:
                state = {}
                info = dsb.resolve_gui_info([], self.data_dir, self.db_path,
                                            cfg={}, cur=None,
                                            sess_state=state,
                                            db_conn=tick_conn)
                status, spd, turn_stats = dsb.resolve_turn_status(
                    None, self.db_path, info.get("session_id"),
                    conn=tick_conn)
            finally:
                if tick_conn is not None:
                    tick_conn.close()
        self.assertEqual(counter["n"], 1)
        self.assertEqual(info["session_id"], "sess_main")
        # source 为 "db"：model_usage.started_at（夹具里=now-10s）是当前
        # 合法的会话身份信号（session.time_updated 已不再作为候选信号）——
        # 本用例只验证连接复用，不锁定旧 source 名
        self.assertEqual(info["source"], "db")
        self.assertEqual(info["model"], "model_a")
        self.assertEqual(status, "idle")

    def test_sticky_uses_shared_conn(self):
        """测试 C：resolve_session_sticky 传 conn 时同样零新开连接。"""
        counter, counting = self._counting_connect()
        with mock.patch.object(dsb, "_db_connect", side_effect=counting), \
             mock.patch.object(dsb, "tail_session_resume",
                               return_value=(None, 0)):
            tick_conn = dsb._db_connect(self.db_path)
            try:
                state = {}
                sid, source = dsb.resolve_session_sticky(
                    state, [], self.data_dir, self.db_path, conn=tick_conn)
            finally:
                tick_conn.close()
        self.assertEqual(counter["n"], 1)
        # source 为 "db"：model_usage.started_at（夹具里=now-10s）是当前
        # 合法的会话身份信号（session.time_updated 已不再作为候选信号）——
        # 本用例只验证连接复用，不锁定旧 source 名
        self.assertEqual((sid, source), ("sess_main", "db"))


class TestUiaSignal(unittest.TestCase):
    """Task 3.2: uia 第 4 信号源仅在开关开启且探测拍时参与竞争。"""

    def _mkedb_path(self):
        db_path = os.path.join(self._tmp.name, "uia.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE session (id TEXT, title TEXT)")
            conn.execute("INSERT INTO session VALUES (?,?)",
                         ("sess_tab", "会话甲"))
            conn.commit()
        finally:
            conn.close()
        return db_path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = self._mkedb_path()
        self.data_dir = os.path.join(self._tmp.name, "d")
        os.makedirs(self.data_dir, exist_ok=True)

    def _patch_cfg(self, enabled):
        """patch DEFAULT_CONFIG（调用方不传 cfg 时的回退值）。"""
        cfg = dict(dsb.DEFAULT_CONFIG)
        cfg["enable_uia_tab_probe"] = enabled
        return mock.patch.object(dsb, "DEFAULT_CONFIG", cfg)

    def test_live_cfg_beats_stale_default(self):
        """开关判定看**热加载后的 cfg**：DEFAULT_CONFIG 关着时，改配置文件
        也必须能唤起探测（此前 _uia_signal 只读 DEFAULT_CONFIG，配置文件
        里的 enable_uia_tab_probe 是死开关）。"""
        with mock.patch.object(dsb.uia_tab_probe, "probe_active_tab_title",
                               return_value="会话甲"), \
             mock.patch.object(dsb, "tail_session_resume",
                               return_value=(None, 0)), \
             mock.patch.object(dsb, "time_ms", lambda: 5_000_000):
            with self._patch_cfg(False):
                sid, src = dsb.resolve_session_sticky(
                    {}, [], self.data_dir, self.db_path, conn=None,
                    probe_now=True, cfg={"enable_uia_tab_probe": True})
        self.assertEqual((sid, src), ("sess_tab", "uia"))

    def test_live_cfg_off_beats_stale_default_on(self):
        """反向同理：默认开着时，配置文件关掉就不再探测。"""
        with mock.patch.object(dsb.uia_tab_probe, "probe_active_tab_title") as m_probe, \
             mock.patch.object(dsb, "tail_session_resume",
                               return_value=(None, 0)):
            with self._patch_cfg(True):
                dsb.resolve_session_sticky(
                    {}, [], self.data_dir, self.db_path, conn=None,
                    probe_now=True, cfg={"enable_uia_tab_probe": False})
        m_probe.assert_not_called()

    def test_uia_signal_participates_only_when_enabled(self):
        with mock.patch.object(dsb.uia_tab_probe, "probe_active_tab_title",
                               return_value="会话甲") as m_probe, \
             mock.patch.object(dsb, "tail_session_resume",
                               return_value=(None, 0)), \
             mock.patch.object(dsb, "time_ms", lambda: 5_000_000):
            # 1) 开关关：不调 probe，信号缺席（mark 也无 -> none/jsonl）
            state1 = {}
            with self._patch_cfg(False):
                sid, src = dsb.resolve_session_sticky(
                    state1, [], self.data_dir, self.db_path,
                    conn=None, probe_now=True)
            m_probe.assert_not_called()
            self.assertIsNone(sid)

            # 2) 开关开 + probe_now + 唯一命中：uia 信号参与竞争并设 sticky
            state2 = {}
            with self._patch_cfg(True):
                sid, src = dsb.resolve_session_sticky(
                    state2, [], self.data_dir, self.db_path,
                    conn=None, probe_now=True)
            m_probe.assert_called_once()
            self.assertEqual((sid, src), ("sess_tab", "uia"))
            self.assertEqual(state2["sticky_sid"], "sess_tab")

            # 3) 开关开但非探测拍（probe_now=False）：不调 probe，保持 sticky
            m_probe.reset_mock()
            with self._patch_cfg(True):
                sid, src = dsb.resolve_session_sticky(
                    state2, [], self.data_dir, self.db_path,
                    conn=None, probe_now=False)
            m_probe.assert_not_called()
            self.assertEqual((sid, src), ("sess_tab", "sticky"))

            # 4) 开关开 + 探测拍 + 歧义（反查 None）：信号缺席，保持 sticky
            with mock.patch.object(dsb.uia_tab_probe, "probe_active_tab_title",
                                   return_value="歧义标题"), \
                 mock.patch.object(dsb.uia_tab_probe, "resolve_sid_by_title",
                                   return_value=None):
                with self._patch_cfg(True):
                    sid, src = dsb.resolve_session_sticky(
                        state2, [], self.data_dir, self.db_path,
                        conn=None, probe_now=True)
            self.assertEqual((sid, src), ("sess_tab", "sticky"))


if __name__ == "__main__":
    unittest.main()
