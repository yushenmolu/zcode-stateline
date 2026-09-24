# -*- coding: utf-8 -*-
"""dir_watcher 单元测试：ReadDirectoryChangesW 监听 + 去抖 + 干净退出契约。

覆盖
----
1. 启动 + 目标文件写入触发回调（start() 返回即"已布防"，不漏首个事件）
2. names 白名单外文件不触发回调
3. 去抖窗口合并突发写入（5 连写 < 5 次回调）
4. stop() 正常路径：True + 线程退出 + 句柄关闭
5. stop() 超时分支：工作线程卡在回调里 -> join 超时 -> 返回 False 不抛
6. stop() 重复调用 / 未 start 时调用均安全
7. 静态契约：stop() 方法体内 CloseHandle 不得出现在 join 之前（use-after-close）
8. import guard：ctypes.WinDLL 失败时模块降级 AVAILABLE=False 且不抛（子进程验证）
9. GUI 延迟预算算术：去抖 + FS_POLL_MS + Tk 余量 <= 200ms

平台：仅 Windows（行为用例在非 Windows / kernel32 不可用时跳过）。
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import dir_watcher as dw
import docked_statusbar as dsb

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")

_requires_win32 = unittest.skipUnless(
    dw.AVAILABLE, "DirWatcher requires Windows kernel32 (module reports AVAILABLE=False)")


class _WatcherTestBase(unittest.TestCase):
    """公共：起 watcher 的上下文管理器，保证 with 块内 stop、目录删除前线程已退出。"""

    @contextmanager
    def watching(self, d, on_change, names, debounce_ms=150):
        w = dw.DirWatcher(d, on_change=on_change, names=names, debounce_ms=debounce_ms)
        w.start()
        try:
            yield w
        finally:
            try:
                w.stop(timeout=2.0)
            except Exception:
                pass


class TestWatchBehavior(_WatcherTestBase):
    """监听行为：回调触发 / 白名单过滤 / 去抖合并。"""

    @_requires_win32
    def test_start_and_callback_on_target_file(self):
        """启动后写入目标文件 -> 3 秒内收到回调。"""
        with tempfile.TemporaryDirectory() as d:
            got = threading.Event()
            with self.watching(d, lambda name: got.set(), names={"a.json"}):
                with open(os.path.join(d, "a.json"), "w", encoding="utf-8") as f:
                    f.write("{}")
                self.assertTrue(got.wait(3.0), "no callback within 3s after writing a.json")

    @_requires_win32
    def test_non_target_file_no_callback(self):
        """写入不在 names 集合内的文件 -> 0.5 秒内无回调。"""
        with tempfile.TemporaryDirectory() as d:
            got = threading.Event()
            with self.watching(d, lambda name: got.set(), names={"a.json"}):
                with open(os.path.join(d, "other.txt"), "w", encoding="utf-8") as f:
                    f.write("noise")
                self.assertFalse(got.wait(0.5),
                                 "callback fired for non-target file other.txt")

    @_requires_win32
    def test_debounce_merges_burst_writes(self):
        """5 次快速连写（间隔 <30ms）同一目标文件 -> 回调次数 < 5（去抖合并）。"""
        with tempfile.TemporaryDirectory() as d:
            calls = []
            lock = threading.Lock()

            def on_change(name):
                with lock:
                    calls.append(name)

            with self.watching(d, on_change, names={"a.json"}, debounce_ms=150):
                target = os.path.join(d, "a.json")
                for i in range(5):
                    with open(target, "w", encoding="utf-8") as f:
                        f.write('{"i": %d}' % i)
                    time.sleep(0.02)
                time.sleep(0.6)  # 等去抖窗口关闭 + 回调落地
                with lock:
                    n = len(calls)
                self.assertGreaterEqual(n, 1, "no callback at all for 5 writes")
                self.assertLess(n, 5, "debounce should merge burst writes into < 5 callbacks")


class TestStop(_WatcherTestBase):
    """stop() 的正常 / 超时 / 幂等路径。"""

    @_requires_win32
    def test_stop_normal_path(self):
        """stop() -> True，线程退出，主线程句柄已关闭。"""
        with tempfile.TemporaryDirectory() as d:
            w = dw.DirWatcher(d, on_change=None, names={"a.json"})
            w.start()
            thread = w._thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
            self.assertTrue(w.stop(timeout=3.0))
            self.assertFalse(thread.is_alive())
            self.assertIsNone(w._stop_handle, "stop handle must be closed after clean exit")

    @_requires_win32
    def test_stop_timeout_returns_false(self):
        """stop() 超时分支：返回 False 且不抛异常。

        构造方式说明：任务原建议 monkeypatch thread.join 为空操作，但 stop()
        的第一步 SetEvent 会让健康的工作线程很快自行退出——空 join 返回后
        is_alive() 可能在微秒级翻成 False，断言不稳定（racy）。故改用确定性
        等价形式：让工作线程阻塞在 on_change 回调里（消费不到 stop 信号），
        stop(timeout=0.1) 的 join 必然超时 -> False。timeout 本身就是 stop()
        的可注入参数。
        """
        with tempfile.TemporaryDirectory() as d:
            entered = threading.Event()
            release = threading.Event()

            def on_change(name):
                entered.set()
                release.wait(10.0)  # 卡住工作线程，stop 信号无人消费

            w = dw.DirWatcher(d, on_change=on_change, names={"a.json"},
                              debounce_ms=30)
            w.start()
            with open(os.path.join(d, "a.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            self.assertTrue(entered.wait(3.0), "worker never entered the callback")
            try:
                self.assertFalse(w.stop(timeout=0.1),
                                 "stop() must return False when join times out")
                self.assertTrue(w._thread.is_alive())
            finally:
                release.set()  # 解除阻塞，让工作线程走正常退出路径
                if w._thread is not None:
                    w._thread.join(2.0)
            # 线程真正退出后再次 stop：应成功并补关句柄（也验证超时路径可恢复）
            self.assertTrue(w.stop(timeout=2.0))
            self.assertIsNone(w._stop_handle)

    @_requires_win32
    def test_stop_twice_is_safe(self):
        """连续两次 stop() 不抛异常，第二次仍返回 True。"""
        with tempfile.TemporaryDirectory() as d:
            w = dw.DirWatcher(d, on_change=None, names={"a.json"})
            w.start()
            self.assertTrue(w.stop(timeout=3.0))
            self.assertTrue(w.stop(timeout=3.0))

    def test_stop_before_start_is_safe(self):
        """未 start 时 stop() 安全返回 True（不碰任何句柄）。"""
        w = dw.DirWatcher("Z:\\no\\such\\dir", on_change=None)
        self.assertTrue(w.stop())


class TestStaticContract(unittest.TestCase):
    """源码级契约：退出时序铁律的机械化检查（不依赖运行时）。"""

    def _stop_method_source(self):
        path = os.path.join(_SCRIPTS_DIR, "dir_watcher.py")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        start = text.index("    def stop(")
        end = text.index("\n    def ", start + 1)  # 下一个同级方法
        return text[start:end]

    def test_no_closehandle_before_join_in_stop(self):
        """stop() 体内 CloseHandle 必须全部位于 thread.join 之后（use-after-close 铁律）。"""
        body = self._stop_method_source()
        join_idx = body.find("thread.join")
        self.assertGreaterEqual(join_idx, 0, "stop() must call thread.join")
        idx = body.find("CloseHandle")
        while idx != -1:
            self.assertGreater(
                idx, join_idx,
                "CloseHandle inside stop() must not appear before thread.join "
                "(closing handles before join risks use-after-close)")
            idx = body.find("CloseHandle", idx + 1)


class TestImportGuard(unittest.TestCase):
    """import guard：kernel32 不可用时模块降级且 import 不抛。"""

    def test_win_dll_failure_degrades_to_unavailable(self):
        """monkeypatch ctypes.WinDLL 抛 OSError -> AVAILABLE=False 且不抛。

        用子进程验证：模块级探测发生在 import 时，进程内 reload 会重复执行
        模块级 kernel32 探测（副作用在测试进程内不可控）。
        """
        code = (
            "import ctypes\n"
            "def _boom(*a, **k):\n"
            "    raise OSError('kernel32 unavailable')\n"
            "ctypes.WinDLL = _boom\n"
            "import dir_watcher\n"
            "assert dir_watcher.AVAILABLE is False, dir_watcher.AVAILABLE\n"
            "print('AVAILABLE=%s' % dir_watcher.AVAILABLE)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=_SCRIPTS_DIR, timeout=30)
        self.assertEqual(proc.returncode, 0,
                         "import must not raise; stderr=%r" % proc.stderr)
        self.assertIn("AVAILABLE=False", proc.stdout)


class TestLatencyBudget(unittest.TestCase):
    """GUI 文件事件刷新的延迟预算算术。"""

    def test_fs_event_latency_budget_within_300ms(self):
        """去抖 60ms + 队列排空间隔 FS_POLL_MS + Tk 余量 20ms <= 300ms。

        分量来源：GUI 去抖为设计值 60ms（计划口径）；FS_POLL_MS 从
        docked_statusbar 实际导入。第二轮 Stage5 将 FS_POLL_MS 50→200
        （空转 CPU 降到 1/4），事件响应上限 130→280ms 仍远低于 1 秒兜底
        刷新链，无感知变慢；预算上限同步 200→300。将来再调大 FS_POLL_MS
        或去抖导致总预算超 300ms 时，本测试报警。
        """
        GUI_DEBOUNCE_MS = 60
        TK_MARGIN_MS = 20
        TOTAL_BUDGET_MS = 300
        total = GUI_DEBOUNCE_MS + dsb.FS_POLL_MS + TK_MARGIN_MS
        self.assertLessEqual(
            total, TOTAL_BUDGET_MS,
            "file-event refresh latency budget exceeded: "
            "%d(debounce) + %d(FS_POLL_MS) + %d(Tk) = %d > %d"
            % (GUI_DEBOUNCE_MS, dsb.FS_POLL_MS, TK_MARGIN_MS,
               total, TOTAL_BUDGET_MS))


if __name__ == "__main__":
    unittest.main()
