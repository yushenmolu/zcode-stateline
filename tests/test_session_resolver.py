# -*- coding: utf-8 -*-
"""Stage 1 信号驱动 + 粘滞判定：read_mark_raw / tail_session_resume 等单元测试。"""
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


if __name__ == "__main__":
    unittest.main()
