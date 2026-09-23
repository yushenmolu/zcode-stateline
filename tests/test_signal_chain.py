# -*- coding: utf-8 -*-
"""粘滞信号链修复测试：resume tailer 过滤 subagent + mark 候选新鲜度门槛。

根因 1：tail_session_resume 单槽逐行覆盖，日志最后一条 resume 常是
sess_subagent_*，判定层过滤后整次判空 -> 主会话切换信号丢失。
修复：tailer 逐行扫描时跳过 subagent resume（单槽只记主会话，
log_offset 仍推进）。

根因 2：mark 候选用 read_mark_raw（无新鲜度判断），陈旧 mark 可顶位、
与 db 信号竞争造成摆动。修复：超过 MARK_FRESH_MS 的 mark 不入池
（mark 是瞬时确认信号，过期由粘滞保持）。
"""
import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


def _today_name():
    return "zcode-%s.jsonl" % datetime.date.today().isoformat()


def _resume_line(sid, ts_ms):
    return json.dumps({"event": "session.resumed", "sessionId": sid,
                       "ts": ts_ms}) + "\n"


class TestTailerSkipsSubagentResume(unittest.TestCase):
    """修复 1：tailer 单槽只记主会话 resume。"""

    def test_tailer_skips_subagent_resume(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, _today_name())
            now = dsb.time_ms()
            ts_main = now - 5000
            with open(path, "w", encoding="utf-8") as f:
                f.write(_resume_line("sess_a", ts_main))
                f.write(_resume_line("sess_subagent_x", now))  # 更晚，旧行为会覆盖
            state = {}
            sid, ts = dsb.tail_session_resume(state, d)
            self.assertEqual((sid, ts), ("sess_a", ts_main))
            # 单槽被跳过不等于停止读文件：log_offset 必须推进到文件尾
            self.assertEqual(state["log_offset"], os.path.getsize(path))


class TestMarkFreshnessGate(unittest.TestCase):
    """修复 2：resolve_session_sticky 的 mark 候选加 MARK_FRESH_MS 门槛。"""

    def _write_mark(self, d, sid, updated):
        with open(os.path.join(d, dsb.MARK_FILE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"session_id": sid, "updated_at": updated}, f)

    def _sticky(self, d, state, resume=(None, 0)):
        db_path = os.path.join(d, "nope.sqlite")  # db 信号缺席（静默降级）
        # 依赖注入（round2 step4 起）：resume 注入确定性信号；mark 走真实
        # 文件读取（本组测试验的就是 mark 文件与注入 resume 的竞争规则）。
        # 抽离后 patch.object(dsb, "tail_session_resume") 不再截获
        # statusbar_session 的模块全局名——注入是更直接的契约。
        return dsb.resolve_session_sticky(
            state, [], d, db_path, conn=None,
            read_resume=lambda _s: resume)

    def test_stale_mark_does_not_override_sticky(self):
        """陈旧 mark（14 分钟前）不得顶掉既有粘滞。

        注：sticky_set_at 取 20 分钟前（早于 mark ts）——这才是修复前
        "陈旧 mark 仍可赢"的顶位路径；若 sticky_set_at 更新，切换规则
        本身就会拒绝，测不到该 bug。
        """
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self._write_mark(d, "sess_b", now - 14 * 60 * 1000)
            state = {"sticky_sid": "sess_a",
                     "sticky_set_at": now - 20 * 60 * 1000}
            self.assertEqual(self._sticky(d, state), ("sess_a", "sticky"))

    def test_stale_mark_loses_to_fresher_resume(self):
        """陈旧 mark 指向 sess_b + 新 resume 指向 sess_a -> 切到 resume 信号。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self._write_mark(d, "sess_b", now - 14 * 60 * 1000)
            state = {}
            self.assertEqual(
                self._sticky(d, state, resume=("sess_a", now - 10 * 1000)),
                ("sess_a", "resume"))

    def test_fresh_mark_still_wins(self):
        """新鲜 mark（30 秒内）仍正常参与竞争（门槛不误伤正常路径）。"""
        with tempfile.TemporaryDirectory() as d:
            now = dsb.time_ms()
            self._write_mark(d, "sess_b", now - 5 * 1000)
            state = {}
            self.assertEqual(self._sticky(d, state), ("sess_b", "mark"))


if __name__ == "__main__":
    unittest.main()
