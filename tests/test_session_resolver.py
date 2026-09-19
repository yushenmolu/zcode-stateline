# -*- coding: utf-8 -*-
"""Stage 1 信号驱动 + 粘滞判定：read_mark_raw / tail_session_resume 等单元测试。

Week 3 补充：status-state.json 候选、resume 新鲜度门槛。
"""
import ast
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

    # 0.7.1 mark 新鲜度门槛（MARK_FRESH_MS=30s）：T1/T2 改为基于当前时刻的
    # 「较旧/较新」对。旧值 1_000_000/2_000_000 是 1970 年代毫秒，会被门槛
    # 正确拒绝——那批用法编码的是修复前"陈旧 mark 入池"的旧语义。
    T1 = dsb.time_ms() - 20_000
    T2 = dsb.time_ms() - 10_000

    def _run(self, state, rows, mark=(None, 0), resume=(None, 0), db=(None, 0)):
        with mock.patch.object(dsb, "read_mark_raw", return_value=mark), \
             mock.patch.object(dsb, "tail_session_resume", return_value=resume), \
             mock.patch.object(dsb, "db_recent_session_activity", return_value=db):
            return dsb.resolve_session_sticky(state, rows, "dummy_dir", "dummy_db")

    def test_sticky_keeps_session_after_mark_expires(self):
        """R1：mark 超龄后不入池（0.7.1 新鲜度门槛），sticky 保持，不漂移。"""
        state = {}
        # 第一拍：新鲜 mark 建立 sticky
        sid, source = self._run(state, [],
                                mark=("sess_a", dsb.time_ms() - 5 * 1000))
        self.assertEqual((sid, source), ("sess_a", "mark"))
        # 第二拍：mark 过期（1 小时前），无更新信号 -> 入池被拒，保持 sticky
        sid, source = self._run(state, [],
                                mark=("sess_a", dsb.time_ms() - 3600 * 1000))
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


class TestGuiInfoStickyIntegration(unittest.TestCase):
    """Task 1.4: resolve_gui_info 接入 sticky resolver 的集成测试。"""

    def test_gui_info_uses_sticky_session(self):
        """连续两次调用 resolve_gui_info：第二次 mark 过期但 session_id 不变。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            mark_path = os.path.join(d, dsb.MARK_FILE_NAME)
            with open(mark_path, "w", encoding="utf-8") as f:
                json.dump({"session_id": "sess_x", "updated_at": now}, f)
            state = {}
            # db 不存在：db 信号/model/title/统计全部静默降级
            db_path = os.path.join(d, "nope.sqlite")
            with mock.patch.object(dsb, "tail_session_resume",
                                   return_value=(None, 0)):
                info1 = dsb.resolve_gui_info([], d, db_path, sess_state=state)
                self.assertEqual(info1["session_id"], "sess_x")
                self.assertEqual(info1["source"], "mark")

                # mark 过期（updated_at 改写为 1 小时前）：
                # 旧机制会漂移，sticky 应保持 sess_x
                with open(mark_path, "w", encoding="utf-8") as f:
                    json.dump({"session_id": "sess_x",
                               "updated_at": now - 3600 * 1000}, f)
                info2 = dsb.resolve_gui_info([], d, db_path, sess_state=state)
                self.assertEqual(info2["session_id"], "sess_x")
                self.assertEqual(info2["source"], "sticky")

    def test_gui_info_backward_compat_no_sess_state(self):
        """sess_state 缺省（None）时向后兼容：内部自建临时 dict，可正常返回。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            with open(os.path.join(d, dsb.MARK_FILE_NAME), "w",
                      encoding="utf-8") as f:
                json.dump({"session_id": "sess_y", "updated_at": now}, f)
            db_path = os.path.join(d, "nope.sqlite")
            with mock.patch.object(dsb, "tail_session_resume",
                                   return_value=(None, 0)):
                info = dsb.resolve_gui_info([], d, db_path)
                self.assertEqual(info["session_id"], "sess_y")
                self.assertEqual(info["source"], "mark")


class TestStickyNewSignals(unittest.TestCase):
    """Week 3 Task 3.2 / 3.3: status-state.json 候选 + resume 新鲜度门槛
    （status 走真实文件读取，其余信号全 mock 保证确定性）。

    0.9.0：status-state.json 已改为按会话分片的 v2 文档，身份候选取
    「全部分片中最新一条」；默认 helper 写 v2，另保留 v1 老文件兼容用例。
    """

    def _write_status_state(self, d, sid, ts):
        """写 v2 分片文档（单会话一条）。"""
        with open(os.path.join(d, dsb.STATUS_STATE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"version": 2,
                       "sessions": {sid: {"event": "generating", "ts": ts,
                                          "hook": "UserPromptSubmit",
                                          "turn_started_at": ts,
                                          "session_id": sid}},
                       "order": [sid]}, f)

    def _write_status_shards(self, d, entries):
        """写 v2 多分片文档：entries = [(sid, ts), ...]。"""
        sessions = dict((sid, {"event": "generating", "ts": ts,
                               "hook": "UserPromptSubmit",
                               "turn_started_at": ts, "session_id": sid})
                        for sid, ts in entries)
        with open(os.path.join(d, dsb.STATUS_STATE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"version": 2, "sessions": sessions,
                       "order": [sid for sid, _ in entries]}, f)

    def _write_status_state_v1(self, d, sid, ts):
        """写 0.8.0 的 v1 全局单条文档（升级窗口兼容）。"""
        with open(os.path.join(d, dsb.STATUS_STATE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"event": "UserPromptSubmit", "ts": ts,
                       "session_id": sid}, f)

    def _run(self, d, state, mark=(None, 0), resume=(None, 0),
             db=(None, 0)):
        # db_path 指向不存在的文件：真实 db 查询不参与（避免测试污染/不确定性）
        db_path = os.path.join(d, "nope.sqlite")
        with mock.patch.object(dsb, "read_mark_raw", return_value=mark), \
             mock.patch.object(dsb, "tail_session_resume", return_value=resume), \
             mock.patch.object(dsb, "db_recent_session_activity",
                               return_value=db):
            return dsb.resolve_session_sticky(state, [], d, db_path,
                                              conn=None, probe_now=False)

    def test_status_signal_wins_over_older_mark(self):
        """status-state.json（新鲜 ts，会话 X）+ 更旧 mark -> (X, "status")。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self._write_status_state(d, "sess_x", now - 2 * 1000)
            state = {}
            self.assertEqual(
                self._run(d, state, mark=("sess_m", now - 10 * 1000)),
                ("sess_x", "status"))

    def test_latest_shard_wins_among_sessions(self):
        """多分片取最新一条：sess_b 更新 -> 候选是 b（不是文件里的第一条）。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self._write_status_shards(d, [("sess_a", now - 20 * 1000),
                                          ("sess_b", now - 3 * 1000)])
            self.assertEqual(self._run(d, {}), ("sess_b", "status"))

    def test_v1_flat_doc_still_adopted(self):
        """v1 老文件兼容：无 sessions 字段的单条记录仍能作为 status 候选。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self._write_status_state_v1(d, "sess_x", now - 2 * 1000)
            self.assertEqual(
                self._run(d, {}, mark=("sess_m", now - 10 * 1000)),
                ("sess_x", "status"))

    def test_stale_status_signal_rejected(self):
        """status ts 距今超 STATUS_SIGNAL_FRESH_MS(60s) -> 该信号缺席。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self._write_status_state(d, "sess_x", now - 61 * 1000)
            # 无其他信号：陈旧 status 不得被采纳
            self.assertEqual(self._run(d, {}), (None, "none"))
            # 有新鲜 mark 时：胜出者是 mark（证明 status 缺席，而非被 mark 压制）
            self.assertEqual(
                self._run(d, {}, mark=("sess_m", now - 5 * 1000)),
                ("sess_m", "mark"))

    def test_stale_resume_rejected_by_freshness_gate(self):
        """resume 距今超 RESUME_FRESH_MS(600s) -> 不采纳该信号。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self.assertEqual(
                self._run(d, {}, resume=("sess_r", now - 601 * 1000)),
                (None, "none"))

    def test_fresh_resume_still_adopted(self):
        """门槛不误伤正常路径：窗口内 resume 仍正常置位 sticky。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self.assertEqual(
                self._run(d, {}, resume=("sess_r", now - 300 * 1000)),
                ("sess_r", "resume"))


class TestStickyStaticContract(unittest.TestCase):
    """H-1 静态契约：session.time_updated 不得回流身份竞争。

    背景：session.time_updated 由后台（消息/part 落库）驱动，任何会话
    有动静都会刷新——若把它加入 resolve_session_sticky 的候选池，后台
    刷新会顶掉真正的当前会话（误切）。已从候选中删除 time_updated，
    本静态测试防止字段回流。
    """

    def test_resolve_session_sticky_body_has_no_time_updated(self):
        """resolve_session_sticky 函数体（含文档串）内不得出现 time_updated。"""
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "docked_statusbar.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "resolve_session_sticky")
        body = ast.get_source_segment(src, func)
        self.assertIsNotNone(body, "未能提取 resolve_session_sticky 函数源码")
        self.assertNotIn("time_updated", body,
                         "resolve_session_sticky 函数体内出现 time_updated："
                         "session 表字段回流身份竞争，会误切当前会话")


class TestStaleDbCandidateNoOverride(unittest.TestCase):
    """H-1：陈旧 db 候选（model_usage.started_at 早于 sticky_set_at）
    不得接管已设定的 sticky 会话。

    db 信号走真实临时 sqlite（不 mock db_recent_session_activity），
    覆盖「model_usage.started_at 真实取值 -> 候选池 -> 粘滞判定」全链路；
    mark/resume 信号 mock 保证确定性。
    """

    def _mkdb(self, path, rows):
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE model_usage (session_id TEXT, started_at INTEGER)")
            for sid, started in rows:
                conn.execute("INSERT INTO model_usage VALUES (?, ?)", (sid, started))
            conn.commit()
        finally:
            conn.close()

    def test_stale_db_started_at_does_not_override_sticky(self):
        """sticky_set_at=T 时，db 候选 started_at < T -> 保持 sticky 会话。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            T = now - 10 * 1000
            db_path = os.path.join(d, "t.sqlite")
            # sess_b 的 model_usage 行在 DB_ACTIVE_WINDOW_MS(180s) 活跃窗口内，
            # 但 started_at（T-5s）早于 sticky_set_at（T）
            self._mkdb(db_path, [("sess_b", T - 5 * 1000)])
            state = {}
            # 第一拍：mark（ts=T）建立 sticky=sess_a、sticky_set_at=T
            with mock.patch.object(dsb, "read_mark_raw",
                                   return_value=("sess_a", T)), \
                 mock.patch.object(dsb, "tail_session_resume",
                                   return_value=(None, 0)), \
                 mock.patch.object(dsb, "db_recent_session_activity",
                                   return_value=(None, 0)):
                sid, source = dsb.resolve_session_sticky(state, [], d, db_path)
            self.assertEqual((sid, source), ("sess_a", "mark"))
            # 第二拍：mark 消失，真实 db 查询带回 sess_b（started_at < T）
            # -> 陈旧候选不得覆盖 sticky
            with mock.patch.object(dsb, "read_mark_raw",
                                   return_value=(None, 0)), \
                 mock.patch.object(dsb, "tail_session_resume",
                                   return_value=(None, 0)):
                sid, source = dsb.resolve_session_sticky(state, [], d, db_path)
            self.assertEqual((sid, source), ("sess_a", "sticky"))
            self.assertEqual(state["sticky_sid"], "sess_a")
            self.assertEqual(state["sticky_set_at"], T)

    def test_fresh_db_started_at_overrides_sticky(self):
        """对照：started_at 晚于 sticky_set_at 的 db 候选正常接管。

        证明上一条「不接管」是粘滞规则在起作用，而非 db 信号失效。
        """
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            T = now - 10 * 1000
            db_path = os.path.join(d, "t.sqlite")
            self._mkdb(db_path, [("sess_b", T + 5 * 1000)])
            state = {}
            with mock.patch.object(dsb, "read_mark_raw",
                                   return_value=("sess_a", T)), \
                 mock.patch.object(dsb, "tail_session_resume",
                                   return_value=(None, 0)), \
                 mock.patch.object(dsb, "db_recent_session_activity",
                                   return_value=(None, 0)):
                sid, source = dsb.resolve_session_sticky(state, [], d, db_path)
            self.assertEqual((sid, source), ("sess_a", "mark"))
            with mock.patch.object(dsb, "read_mark_raw",
                                   return_value=(None, 0)), \
                 mock.patch.object(dsb, "tail_session_resume",
                                   return_value=(None, 0)):
                sid, source = dsb.resolve_session_sticky(state, [], d, db_path)
            self.assertEqual((sid, source), ("sess_b", "db"))
            self.assertEqual(state["sticky_sid"], "sess_b")
            self.assertEqual(state["sticky_set_at"], T + 5 * 1000)


if __name__ == "__main__":
    unittest.main()
