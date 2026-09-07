# -*- coding: utf-8 -*-
"""Stage 1 信号驱动 + 粘滞判定：read_mark_raw / tail_session_resume 等单元测试。"""
import datetime
import json
import os
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
