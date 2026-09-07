# -*- coding: utf-8 -*-
"""Stage 1 信号驱动 + 粘滞判定：read_mark_raw / tail_session_resume 等单元测试。"""
import datetime
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


class TestReadMarkRaw(unittest.TestCase):
    """Task 1.1: read_mark_raw 拆分。"""

    def _write_mark(self, data_dir, obj):
        path = os.path.join(data_dir, dsb.MARK_FILE_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_read_mark_raw_fresh_and_stale(self):
        with tempfile.TemporaryDirectory() as d:
            # 新鲜 mark：返回 (sid, updated)
            now = dsb.time_ms()
            self._write_mark(d, {"session_id": "sess_fresh", "updated_at": now})
            sid, updated = dsb.read_mark_raw(d)
            self.assertEqual(sid, "sess_fresh")
            self.assertEqual(updated, now)

            # 过期 mark（1 小时前）：仍返回 (sid, updated)，而非 None
            old = now - 3600 * 1000
            self._write_mark(d, {"session_id": "sess_stale", "updated_at": old})
            sid, updated = dsb.read_mark_raw(d)
            self.assertEqual(sid, "sess_stale")
            self.assertEqual(updated, old)

            # 坏 JSON：返回 (None, 0)
            with open(os.path.join(d, dsb.MARK_FILE_NAME), "w", encoding="utf-8") as f:
                f.write("{not json")
            sid, updated = dsb.read_mark_raw(d)
            self.assertIsNone(sid)
            self.assertEqual(updated, 0)

            # 兼容包装 read_mark_file 保留 30 秒新鲜度语义
            self._write_mark(d, {"session_id": "sess_fresh2", "updated_at": dsb.time_ms()})
            self.assertEqual(dsb.read_mark_file(d), "sess_fresh2")
            self._write_mark(d, {"session_id": "sess_old2", "updated_at": dsb.time_ms() - 60 * 1000})
            self.assertIsNone(dsb.read_mark_file(d))


def _iso_to_ms(ts_iso):
    """ISO 8601（UTC，Z 后缀）-> epoch 毫秒。"""
    dt = datetime.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


class TestTailSessionResume(unittest.TestCase):
    """Task 1.2: session.resumed 日志 tailer（增量读）。"""

    def _today_name(self):
        return "zcode-%s.jsonl" % datetime.date.today().isoformat()

    def _append(self, path, text):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)

    def _resume_line(self, sid, ts_iso):
        return json.dumps(
            {"timestamp": ts_iso, "level": "info", "event": "session.resumed",
             "sessionId": sid, "status": "completed"}
        ) + "\n"

    def test_tail_session_resume_incremental(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, self._today_name())
            state = {}

            # 文件不存在：返回 (None, 0)，不抛
            sid, ts = dsb.tail_session_resume(state, log_dir=d)
            self.assertIsNone(sid)
            self.assertEqual(ts, 0)

            # 1) 追加两行 resume 事件：第一次调用返回最新 sid 和 ts
            self._append(path, self._resume_line("sess_aaa", "2026-09-07T01:00:00.000Z"))
            self._append(path, self._resume_line("sess_bbb", "2026-09-07T02:00:00.000Z"))
            sid, ts = dsb.tail_session_resume(state, log_dir=d)
            self.assertEqual(sid, "sess_bbb")
            self.assertEqual(ts, _iso_to_ms("2026-09-07T02:00:00.000Z"))
            offset_after_first = state["log_offset"]
            self.assertGreater(offset_after_first, 0)

            # 2) 不追加再调：返回相同结果且 offset 不回头
            sid2, ts2 = dsb.tail_session_resume(state, log_dir=d)
            self.assertEqual(sid2, "sess_bbb")
            self.assertEqual(ts2, _iso_to_ms("2026-09-07T02:00:00.000Z"))
            self.assertEqual(state["log_offset"], offset_after_first)

            # 3) 追加不完整行（模拟写入中途：半截 JSON 且无换行结尾）：
            #    不报错、offset 不越界、仍返回上次结果
            self._append(path, '{"timestamp":"2026-09-07T03:00:00.000Z",'
                               '"event":"session.resumed","sessionId":"sess_')
            sid3, ts3 = dsb.tail_session_resume(state, log_dir=d)
            self.assertEqual(sid3, "sess_bbb")
            self.assertEqual(ts3, _iso_to_ms("2026-09-07T02:00:00.000Z"))
            self.assertEqual(state["log_offset"], offset_after_first)

            # 补全剩余内容与换行后再调：能读到 sess_ccc
            self._append(path, 'ccc"}\n')
            sid4, ts4 = dsb.tail_session_resume(state, log_dir=d)
            self.assertEqual(sid4, "sess_ccc")
            self.assertEqual(ts4, _iso_to_ms("2026-09-07T03:00:00.000Z"))

            # 4) state 的 log_date 为昨天：跨天重置 offset，重新打开今日文件
            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            state2 = {"log_date": yesterday, "log_offset": 999999}
            sid5, ts5 = dsb.tail_session_resume(state2, log_dir=d)
            self.assertEqual(sid5, "sess_ccc")  # 今日文件内最新 resume
            self.assertEqual(ts5, _iso_to_ms("2026-09-07T03:00:00.000Z"))
            self.assertEqual(state2["log_date"], datetime.date.today().isoformat())


class TestDbRecentSessionActivity(unittest.TestCase):
    """Task 1.3: db_recent_session_activity（临时文件 sqlite，不碰真实 db）。"""

    def _mkdb(self, path, rows):
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE model_usage (session_id TEXT, started_at INTEGER)")
            for sid, started in rows:
                conn.execute("INSERT INTO model_usage VALUES (?, ?)", (sid, started))
            conn.commit()
        finally:
            conn.close()

    def test_db_recent_session_activity(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "t.sqlite")
            now = dsb.time_ms()
            self._mkdb(db_path, [
                ("sess_old", now - 3600 * 1000),        # 窗口外
                ("sess_subagent_x", now - 5 * 1000),    # subagent，过滤
                ("sess_main", now - 10 * 1000),         # 窗口内最新主会话
            ])
            sid, ts = dsb.db_recent_session_activity(db_path)
            self.assertEqual(sid, "sess_main")
            self.assertEqual(ts, now - 10 * 1000)

            # 窗口内行删除后：无行返回 (None, 0)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DELETE FROM model_usage WHERE session_id = 'sess_main'")
                conn.commit()
            finally:
                conn.close()
            sid, ts = dsb.db_recent_session_activity(db_path)
            self.assertIsNone(sid)
            self.assertEqual(ts, 0)

            # 文件不存在：(None, 0)，不抛
            sid, ts = dsb.db_recent_session_activity(os.path.join(d, "nope.sqlite"))
            self.assertIsNone(sid)
            self.assertEqual(ts, 0)


class TestStickyResolver(unittest.TestCase):
    """Task 1.3: 粘滞判定状态机 resolve_session_sticky（三信号全 mock，确定性）。"""

    T1 = 1_000_000
    T2 = 2_000_000

    def _run(self, state, rows, mark=(None, 0), resume=(None, 0), db=(None, 0)):
        with mock.patch.object(dsb, "read_mark_raw", return_value=mark), \
             mock.patch.object(dsb, "tail_session_resume", return_value=resume), \
             mock.patch.object(dsb, "db_recent_session_activity", return_value=db):
            return dsb.resolve_session_sticky(state, rows, "dummy_dir", "dummy_db")

    def test_sticky_keeps_session_after_mark_expires(self):
        """R1：mark 超龄（updated 为 1 小时前）后 sticky 保持，不漂移。"""
        state = {}
        stale_mark = ("sess_a", dsb.time_ms() - 3600 * 1000)
        sid, source = self._run(state, [], mark=stale_mark)
        self.assertEqual((sid, source), ("sess_a", "mark"))
        # 第二拍：mark 仍是过期值，无更新信号 -> 保持 sticky
        sid, source = self._run(state, [], mark=stale_mark)
        self.assertEqual((sid, source), ("sess_a", "sticky"))
        self.assertEqual(state["sticky_sid"], "sess_a")

    def test_sticky_switches_on_newer_mark(self):
        """R2：出现更新的 mark 信号（ts 更大）时切换。"""
        state = {}
        sid, source = self._run(state, [], mark=("sess_a", self.T1))
        self.assertEqual((sid, source), ("sess_a", "mark"))
        sid, source = self._run(state, [], mark=("sess_b", self.T2))
        self.assertEqual((sid, source), ("sess_b", "mark"))
        self.assertEqual(state["sticky_sid"], "sess_b")
        self.assertEqual(state["sticky_set_at"], self.T2)

    def test_sticky_ignores_older_signal(self):
        """旧信号（ts 早于 sticky_set_at）不得回切（防抖动）。"""
        state = {}
        sid, source = self._run(state, [], mark=("sess_a", self.T2))
        self.assertEqual((sid, source), ("sess_a", "mark"))
        # mark 消失，resume 带来旧时间戳的另一会话 -> 不切换
        sid, source = self._run(state, [], mark=(None, 0),
                                resume=("sess_b", self.T1))
        self.assertEqual((sid, source), ("sess_a", "sticky"))

    def test_sticky_first_boot_falls_back_to_jsonl(self):
        """R6：首次启动无任何信号时走 jsonl 兜底；都没有 -> (None, 'none')。"""
        state = {}
        rows = [{"sessionId": "sess_j", "ts": 123}]
        sid, source = self._run(state, rows)
        self.assertEqual((sid, source), ("sess_j", "jsonl"))
        state2 = {}
        sid, source = self._run(state2, [])
        self.assertEqual((sid, source), (None, "none"))

    def test_db_signal_cannot_override_newer_mark(self):
        """P1 处置：db started_at 早于 mark updated_at 时 mark 赢。"""
        state = {}
        sid, source = self._run(state, [], mark=("sess_a", self.T2),
                                db=("sess_b", self.T1))
        self.assertEqual((sid, source), ("sess_a", "mark"))
        # mark 消失后 db 旧信号仍不得回切
        sid, source = self._run(state, [], mark=(None, 0),
                                db=("sess_b", self.T1))
        self.assertEqual((sid, source), ("sess_a", "sticky"))

    def test_idle_polling_keeps_sticky(self):
        """P1 处置：无任何信号的空闲轮询不得改变 sticky。"""
        state = {}
        sid, source = self._run(state, [], mark=("sess_a", self.T1))
        self.assertEqual((sid, source), ("sess_a", "mark"))
        for _ in range(3):
            sid, source = self._run(state, [])
            self.assertEqual((sid, source), ("sess_a", "sticky"))

    def test_subagent_signals_filtered(self):
        """subagent sid 的信号不参与判定（R6 相关：子代理一律过滤）。"""
        state = {}
        sid, source = self._run(state, [],
                                mark=("sess_subagent_x", self.T2),
                                resume=("sess_subagent_y", self.T2),
                                db=("sess_subagent_z", self.T2))
        self.assertEqual((sid, source), (None, "none"))


if __name__ == "__main__":
    unittest.main()
