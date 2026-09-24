# -*- coding: utf-8 -*-
"""第二轮 Stage 5 微优化测试：DB 连接单例 + FS_POLL_MS 常量。

项1（连接单例）：
  - _db_connect_cached 同路径复用同一连接对象（不新开）；
  - 复用前 rollback 刷新 WAL 读快照：另一连接新提交的行能被读到
    （长持只读连接不得把数据钉在连接建立/首次读时）；
  - _db_close_cached 关闭并清空单例（幂等）；
  - 单例建立失败（路径不存在）返回 None -> 调用方降级每拍自开；
  - 单例 busy_timeout PRAGMA 生效。
项2（FS_POLL_MS）：常量值为 200 且在 1 秒兜底链内（<= REFRESH_MS_DEFAULT）。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


def _make_db(dirpath):
    db_path = os.path.join(dirpath, "t.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE model_usage (session_id TEXT, started_at INTEGER, "
            "model_id TEXT, status TEXT, query_source TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, "
            "reasoning_tokens INTEGER, duration_ms INTEGER, tool_call_count INTEGER)")
        conn.execute(
            "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sess_a", dsb.time_ms() - 5000, "model_a", "completed",
             "main_turn", 100, 200, 50, 0, 10, 2000, 0))
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestDbConnSingleton(unittest.TestCase):
    """项1：GUI 生命周期级只读连接单例。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = _make_db(self._tmp.name)
        self.addCleanup(dsb._db_close_cached)  # 用例间隔离：清空单例

    def test_same_path_reuses_same_connection(self):
        """同一路径两次调用返回同一连接对象（不新开）。"""
        c1 = dsb._db_connect_cached(self.db_path)
        c2 = dsb._db_connect_cached(self.db_path)
        self.assertIsNotNone(c1)
        self.assertIs(c1, c2)
        # 复用连接确实可用
        self.assertEqual(
            dsb.db_latest_model_id(self.db_path, "sess_a", conn=c1), "model_a")

    def test_singleton_sees_new_commits(self):
        """WAL 快照刷新：单例长持期间，另一连接新提交的行必须可读。

        坑：长持连接在 WAL 下首次读后快照被钉住，新提交读不到。
        _db_connect_cached 每次返回前 rollback 清旧读事务，下一条
        SELECT 必须拿到最新快照。"""
        cached = dsb._db_connect_cached(self.db_path)
        self.assertIsNotNone(cached)
        # 首次读（建立快照）
        self.assertEqual(
            dsb.db_latest_model_id(self.db_path, "sess_a", conn=cached),
            "model_a")
        # 另一写入连接提交新行（新会话 model_b）
        w = sqlite3.connect(self.db_path)
        try:
            w.execute(
                "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("sess_b", dsb.time_ms(), "model_b", "completed", "main_turn",
                 10, 300, 0, 0, 0, 1000, 0))
            w.commit()
        finally:
            w.close()
        # 再次取单例（触发 rollback 刷新）-> 必须读到新提交的 sess_b
        cached2 = dsb._db_connect_cached(self.db_path)
        self.assertIs(cached2, cached)
        self.assertEqual(
            dsb.db_latest_model_id(self.db_path, "sess_b", conn=cached2),
            "model_b")
        # 全库最新一条也必须是 sess_b 的速度（300 tok/s），不是旧快照的 100
        self.assertEqual(
            dsb.db_latest_speed(self.db_path, conn=cached2), 300.0)

    def test_close_cached_resets_singleton(self):
        """_db_close_cached 关闭并清空：再取是新连接对象。"""
        c1 = dsb._db_connect_cached(self.db_path)
        dsb._db_close_cached()
        dsb._db_close_cached()  # 幂等：重复关闭不炸
        c2 = dsb._db_connect_cached(self.db_path)
        self.assertIsNotNone(c2)
        self.assertIsNot(c1, c2)

    def test_missing_db_returns_none_for_fallback(self):
        """单例建立失败（路径不存在）返回 None -> GUI 降级每拍自开。"""
        missing = os.path.join(self._tmp.name, "nope.sqlite")
        self.assertIsNone(dsb._db_connect_cached(missing))

    def test_busy_timeout_pragma_applied(self):
        """单例连接带 busy_timeout（写侧短暂锁表时等待而非立刻报错）。"""
        cached = dsb._db_connect_cached(self.db_path)
        row = cached.execute("PRAGMA busy_timeout").fetchone()
        self.assertGreaterEqual(row[0], 1000)

    def test_query_only_pragma_still_on(self):
        """单例仍是只读（query_only）：写操作必须被拒。"""
        cached = dsb._db_connect_cached(self.db_path)
        with self.assertRaises(sqlite3.OperationalError):
            cached.execute(
                "INSERT INTO model_usage VALUES ('x',0,'m','done','s',0,0,0,0,0,0,0)")


class TestFsPollMsConstant(unittest.TestCase):
    """项2：FS_POLL_MS 调整到 200ms 且仍在 1 秒兜底链内。"""

    def test_fs_poll_ms_is_200(self):
        self.assertEqual(dsb.FS_POLL_MS, 200)

    def test_fs_poll_ms_within_fallback_chain(self):
        """事件驱动排空间隔不得慢于 1 秒兜底刷新（refresh_tick 独立保底，
        FS_POLL_MS 只是事件响应上限，必须 <= REFRESH_MS_DEFAULT）。"""
        self.assertLessEqual(dsb.FS_POLL_MS, dsb.REFRESH_MS_DEFAULT)


if __name__ == "__main__":
    unittest.main()
