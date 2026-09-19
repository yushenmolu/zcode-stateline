# -*- coding: utf-8 -*-
"""0.9.0 批 5：status-state.json 按会话分片（写端 status_event.py + 读端视图）。

根因：ZCode 只有一份 status-state.json，v1 是「全局单条、整体覆盖」。多窗口/
多会话并发时后写者抹掉前写者——A 的徽标会被 B 的事件污染，读端加「sid 不符即
空闲」的校验只是掩盖，副作用是 B 在干活时 A 恒显示空闲（用户看到的「状态不稳」
有一半是这个）。分片后每会话各占一个槽位，写端合并、读端各取各的。

写端用真实临时目录（不 mock 文件 IO），因为原子替换与锁语义正是被测对象。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb
import status_event as se


def _read_doc(d):
    with open(os.path.join(d, se.STATUS_STATE_NAME), encoding="utf-8") as f:
        return json.load(f)


class TestShardedWriter(unittest.TestCase):
    """写端：分片合并、本轮起点、裁剪与降级路径。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name

    def test_two_sessions_do_not_clobber_each_other(self):
        """核心回归：A、B 各写一条 -> 两个槽位都在（v1 会只剩后写者）。"""
        self.assertTrue(se.write_status_event("generating", "sess_a", self.d,
                                              hook="UserPromptSubmit")[0])
        self.assertTrue(se.write_status_event("tool", "sess_b", self.d,
                                              hook="PreToolUse")[0])
        doc = _read_doc(self.d)
        self.assertEqual(doc["version"], 2)
        self.assertEqual(sorted(doc["sessions"]), ["sess_a", "sess_b"])
        self.assertEqual(doc["sessions"]["sess_a"]["event"], "generating")
        self.assertEqual(doc["sessions"]["sess_b"]["event"], "tool")
        # order 尾部最新：后写者排在最后
        self.assertEqual(doc["order"][-1], "sess_b")

    def test_same_session_overwrites_own_slot_only(self):
        """同会话再写：只刷新自己那条（ts 更新），别的会话原样保留。"""
        se.write_status_event("generating", "sess_a", self.d,
                              hook="UserPromptSubmit")
        first = _read_doc(self.d)["sessions"]["sess_a"]["ts"]
        time.sleep(0.002)
        se.write_status_event("tool", "sess_b", self.d, hook="PreToolUse")
        time.sleep(0.002)
        se.write_status_event("idle", "sess_a", self.d, hook="Stop")
        doc = _read_doc(self.d)
        self.assertEqual(doc["sessions"]["sess_a"]["event"], "idle")
        self.assertGreater(doc["sessions"]["sess_a"]["ts"], first)
        self.assertEqual(doc["sessions"]["sess_b"]["event"], "tool")

    def test_turn_started_at_set_only_on_user_prompt_submit(self):
        """本轮起点只在 UserPromptSubmit 置位；同轮的 tool / generating 沿用。"""
        se.write_status_event("generating", "sess_a", self.d,
                              hook="UserPromptSubmit")
        t0 = _read_doc(self.d)["sessions"]["sess_a"]["turn_started_at"]
        time.sleep(0.002)
        se.write_status_event("tool", "sess_a", self.d, hook="PreToolUse")
        self.assertEqual(
            _read_doc(self.d)["sessions"]["sess_a"]["turn_started_at"], t0)
        time.sleep(0.002)
        se.write_status_event("generating", "sess_a", self.d,
                              hook="PostToolUse")
        self.assertEqual(
            _read_doc(self.d)["sessions"]["sess_a"]["turn_started_at"], t0)
        # 新一轮：UserPromptSubmit 重新置位
        time.sleep(0.002)
        se.write_status_event("generating", "sess_a", self.d,
                              hook="UserPromptSubmit")
        self.assertGreater(
            _read_doc(self.d)["sessions"]["sess_a"]["turn_started_at"], t0)

    def test_hook_field_recorded(self):
        """hook 字段落盘（读端据此区分本轮开始与工具回落）。"""
        se.write_status_event("tool", "sess_a", self.d, hook="PermissionRequest")
        rec = _read_doc(self.d)["sessions"]["sess_a"]
        self.assertEqual(rec["hook"], "PermissionRequest")
        self.assertEqual(rec["event"], "tool")

    def test_prune_keeps_newest_slots(self):
        """超出 MAX_SESSION_SLOTS 时按 order 裁剪最旧，防文件无界增长。"""
        for i in range(se.MAX_SESSION_SLOTS + 5):
            se.write_status_event("generating", "sess_%02d" % i, self.d)
        doc = _read_doc(self.d)
        self.assertEqual(len(doc["sessions"]), se.MAX_SESSION_SLOTS)
        self.assertEqual(len(doc["order"]), se.MAX_SESSION_SLOTS)
        self.assertNotIn("sess_00", doc["sessions"])
        self.assertIn("sess_%02d" % (se.MAX_SESSION_SLOTS + 4), doc["sessions"])

    def test_v1_doc_upgrades_in_place_without_losing_record(self):
        """老 v1 文件就地升级：那条记录回到自己的槽位，新写入不抹掉它。"""
        with open(os.path.join(self.d, se.STATUS_STATE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"event": "tool", "ts": 123456, "session_id": "sess_old"},
                      f)
        se.write_status_event("generating", "sess_new", self.d)
        sessions = _read_doc(self.d)["sessions"]
        self.assertEqual(sorted(sessions), ["sess_new", "sess_old"])
        self.assertEqual(sessions["sess_old"]["ts"], 123456)
        self.assertEqual(sessions["sess_old"]["event"], "tool")

    def test_subagent_and_unknown_event_rejected(self):
        """子代理会话与未知事件不写入（返回 ok=False，仍不抛异常）。"""
        self.assertFalse(
            se.write_status_event("generating", "sess_subagent_1", self.d)[0])
        self.assertFalse(se.write_status_event("nope", "sess_a", self.d)[0])
        self.assertFalse(se.write_status_event("", "sess_a", self.d)[0])
        self.assertFalse(os.path.exists(os.path.join(self.d,
                                                     se.STATUS_STATE_NAME)))

    def test_lock_timeout_still_records_own_slot(self):
        """拿不到锁时降级为「只写自己那条」：自己的事件绝不丢。

        锁被别的进程持有且未过期（< STALE_LOCK_S）-> _acquire_lock 超时返回
        None；此时若放弃写入，用户就看不到本轮状态；若整体覆盖，就退回 v1
        的互踩。取舍是只保证自己这条。
        """
        lock = os.path.join(self.d, se.LOCK_NAME)
        with open(lock, "w") as f:
            f.write(str(int(time.time() * 1000)))
        with open(os.path.join(self.d, se.STATUS_STATE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"version": 2,
                       "sessions": {"sess_other": {"event": "tool",
                                                    "ts": 1, "session_id":
                                                    "sess_other"}},
                       "order": ["sess_other"]}, f)
        ok, err = se.write_status_event("generating", "sess_a", self.d)
        self.assertTrue(ok, err)
        rec = _read_doc(self.d)["sessions"]["sess_a"]
        self.assertEqual(rec["event"], "generating")
        os.remove(lock)

    def test_stale_lock_is_preempted(self):
        """陈旧锁（持锁进程已死）被抢占 -> 合并写恢复，别人的槽位不丢。"""
        lock = os.path.join(self.d, se.LOCK_NAME)
        with open(lock, "w") as f:
            f.write("0")
        old = (time.time() - se.STALE_LOCK_S - 5)
        os.utime(lock, (old, old))
        with open(os.path.join(self.d, se.STATUS_STATE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"version": 2,
                       "sessions": {"sess_other": {"event": "tool", "ts": 1,
                                                   "session_id": "sess_other"}},
                       "order": ["sess_other"]}, f)
        ok, err = se.write_status_event("generating", "sess_a", self.d)
        self.assertTrue(ok, err)
        self.assertEqual(sorted(_read_doc(self.d)["sessions"]),
                         ["sess_a", "sess_other"])
        self.assertFalse(os.path.exists(lock))   # 用完即释


class TestShardedReaderViews(unittest.TestCase):
    """读端视图：_status_slots / status_state_for / status_state_latest。"""

    def _doc2(self, **entries):
        return {"version": 2,
                "sessions": dict((k, dict(v, session_id=k))
                                 for k, v in entries.items()),
                "order": list(entries)}

    def test_for_picks_own_session_only(self):
        doc = self._doc2(sess_a={"event": "generating", "ts": 100},
                         sess_b={"event": "tool", "ts": 200})
        self.assertEqual(dsb.status_state_for(doc, "sess_a")["event"],
                         "generating")
        self.assertEqual(dsb.status_state_for(doc, "sess_b")["event"], "tool")
        self.assertIsNone(dsb.status_state_for(doc, "sess_c"))

    def test_missing_session_yields_none_not_latest(self):
        """本会话没有分片 -> None（回落 idle），绝不借别的会话的记录显示状态。"""
        doc = self._doc2(sess_b={"event": "tool", "ts": 200})
        self.assertIsNone(dsb.status_state_for(doc, "sess_a"))

    def test_v1_flat_doc_still_readable(self):
        """v1 老文件：status_state_for 仍走 sid 匹配，latest 取那条。"""
        doc = {"event": "generating", "ts": 500, "session_id": "sess_x"}
        self.assertEqual(dsb.status_state_for(doc, "sess_x")["ts"], 500)
        self.assertIsNone(dsb.status_state_for(doc, "sess_y"))
        self.assertEqual(dsb.status_state_latest(doc), ("sess_x", 500))

    def test_no_sid_slot_is_adopted_as_fallback(self):
        """空 sid 槽（stdin 与模板变量双双取空）按「任一侧为空即采信」兼容。"""
        doc = {"version": 2,
               "sessions": {"": {"event": "tool", "ts": 400,
                                 "session_id": None}},
               "order": [""]}
        rec = dsb.status_state_for(doc, "sess_a")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["event"], "tool")
        self.assertEqual(rec["session_id"], "sess_a")   # 回填后不误伤下游

    def test_latest_takes_newest_ts_across_shards(self):
        doc = self._doc2(sess_a={"event": "generating", "ts": 100},
                         sess_b={"event": "tool", "ts": 900},
                         sess_c={"event": "idle", "ts": 300})
        self.assertEqual(dsb.status_state_latest(doc), ("sess_b", 900))

    def test_latest_empty_or_dirty(self):
        self.assertEqual(dsb.status_state_latest(None), (None, 0))
        self.assertEqual(dsb.status_state_latest({}), (None, 0))
        self.assertEqual(dsb.status_state_latest({"sessions": {}}), (None, 0))
        # ts 非数字的记录跳过，不因脏数据抛
        self.assertEqual(
            dsb.status_state_latest({"version": 2,
                                     "sessions": {"a": {"event": "tool",
                                                        "ts": "abc"},
                                                  "b": {"event": "tool",
                                                        "ts": 7}},
                                     "order": ["a", "b"]}),
            ("b", 7))

    def test_slots_ignores_record_without_event(self):
        doc = {"version": 2,
               "sessions": {"a": {"ts": 1}, "b": {"event": "tool", "ts": 2}},
               "order": ["a", "b"]}
        self.assertEqual(list(dsb._status_slots(doc)), ["b"])

    def test_turn_started_at_reader(self):
        self.assertEqual(
            dsb.status_turn_started_at({"turn_started_at": 123}), 123)
        self.assertIsNone(dsb.status_turn_started_at({"turn_started_at": None}))
        self.assertIsNone(dsb.status_turn_started_at({"turn_started_at": "x"}))
        self.assertIsNone(dsb.status_turn_started_at(None))

    def test_end_to_end_write_then_read_is_per_session(self):
        """端到端：两个会话各写各的，读端各自只看到自己那条（互不污染）。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            se.write_status_event("generating", "sess_a", d,
                                  hook="UserPromptSubmit")
            se.write_status_event("tool", "sess_b", d, hook="PreToolUse")
            doc = dsb._read_status_state(d)
            a = dsb.status_state_for(doc, "sess_a")
            b = dsb.status_state_for(doc, "sess_b")
            self.assertEqual((a["event"], b["event"]),
                             ("generating", "tool"))
            self.assertGreater(a["ts"], now - 1000)
            self.assertEqual(a["turn_started_at"], a["ts"])
            self.assertIsNone(b["turn_started_at"])   # 工件事件不置位
            # B 在干活不影响 A 的状态判定
            self.assertEqual(
                dsb.status_detector(b, None, None, current_sid="sess_a"),
                ("idle", None))


if __name__ == "__main__":
    unittest.main()
