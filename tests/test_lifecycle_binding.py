# -*- coding: utf-8 -*-
"""生命周期绑定（0.9.3）：ZCode 进程退出 -> 小条自己退出。

旧行为是小条作为分离进程常驻（`ensure-docked-statusbar.cmd` 用 start +
pythonw 拉起，不归宿主生命周期管），ZCode 关掉后它只是「找不到主窗口就
SW_HIDE」，进程一直挂着。用户要求改成随宿主一起关。

两条判定刻意分开，本文件锁住这个分工：
  - **显隐**看窗口（`_zcode_process_alive` / `find_zcode_window`）：最小化、
    收进托盘时窗口判定失败，但宿主还活着，此时只该隐藏；
  - **退出**看进程表（`zcode_app_running`）：只有进程表里再没有 ZCode.exe
    才退出。窗口那条链上挂着 `OpenProcess`/`QueryFullProcessImageNameW`
    等多个可瞬时失败的调用，用它判退出等于宿主活着时小条随时可能被杀。

`zcode_app_running` 的负路径必须真跑一遍：如果结构体内存布局写错或兜底
过宽，它会恒返回 True，退出永远不触发，而这条只能靠「换一个不存在的
exe 名要判 False」来暴露。
"""
import ctypes
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


class TestGoneExitDecision(unittest.TestCase):
    """纯判定：缺席满阈值才退出，进程一恢复就清零。"""

    def test_alive_clears_timer(self):
        quit_, since = dsb.zcode_gone_exit_due(True, 1234, 9999)
        self.assertFalse(quit_)
        self.assertIsNone(since)

    def test_first_miss_only_stamps(self):
        quit_, since = dsb.zcode_gone_exit_due(False, None, 1000)
        self.assertFalse(quit_)
        self.assertEqual(since, 1000)

    def test_one_ms_short_keeps_running(self):
        hold = dsb.ZCODE_GONE_QUIT_MS
        quit_, since = dsb.zcode_gone_exit_due(False, 1000, 1000 + hold - 1)
        self.assertFalse(quit_)
        self.assertEqual(since, 1000)   # 起点不被后续拍覆盖

    def test_threshold_reached_quits(self):
        hold = dsb.ZCODE_GONE_QUIT_MS
        quit_, _ = dsb.zcode_gone_exit_due(False, 1000, 1000 + hold)
        self.assertTrue(quit_)

    def test_restart_gap_shorter_than_hold_survives(self):
        """升级/重装：缺席 3 秒后新进程起来 -> 计时清零，不退出。"""
        hold = dsb.ZCODE_GONE_QUIT_MS
        _, since = dsb.zcode_gone_exit_due(False, None, 0)
        _, since = dsb.zcode_gone_exit_due(False, since, hold - 2000)
        quit_, since = dsb.zcode_gone_exit_due(True, since, hold - 1000)
        self.assertFalse(quit_)
        self.assertIsNone(since)
        # 之后再缺席是从零重新计，不会补上前面那 3 秒
        quit_, _ = dsb.zcode_gone_exit_due(False, since, 500)
        self.assertFalse(quit_)


class TestProcessTableProbe(unittest.TestCase):
    """进程表探测：真机跑通，且负路径/fail-open 各自成立。"""

    def test_struct_layout_matches_toolhelp_contract(self):
        # x64 上 PROCESSENTRY32W = 568 字节；th32DefaultHeapID 是 ULONG_PTR，
        # 按 4 字节声明会让 szExeFile 整体错位 4 字节（读出的名字全是垃圾，
        # 于是恒判 False -> 小条在宿主活着时就自己退出）。
        self.assertEqual(ctypes.sizeof(dsb._PROCESSENTRY32W), 568)
        self.assertEqual(dsb._PROCESSENTRY32W.szExeFile.offset, 44)
        self.assertEqual(dsb._PROCESSENTRY32W.th32DefaultHeapID.size,
                         ctypes.sizeof(ctypes.c_size_t))

    def test_real_process_table_contains_this_python(self):
        """同一张表里必须能读到自己：证明枚举循环与字段偏移都对。"""
        win = dsb.win()
        snap = win.create_toolhelp32_snapshot(dsb.TH32CS_SNAPPROCESS, 0)
        self.assertNotEqual(snap, ctypes.c_void_p(-1).value)
        try:
            entry = dsb._PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(dsb._PROCESSENTRY32W)
            seen = []
            ok = win.process32_first(snap, ctypes.byref(entry))
            while ok:
                seen.append((entry.th32ProcessID, (entry.szExeFile or "").lower()))
                ok = win.process32_next(snap, ctypes.byref(entry))
        finally:
            win.close_handle(snap)
        self.assertGreater(len(seen), 10)
        self.assertIn(os.getpid(), [p for p, _ in seen])
        self.assertTrue(any(n.startswith("python") for _, n in seen))

    def test_unknown_exe_name_is_not_alive(self):
        with mock.patch.object(dsb, "ZCODE_EXE_NAME", "no-such-proc.exe"):
            self.assertFalse(dsb.zcode_app_running())

    def test_own_exe_name_is_found(self):
        """正向路径：拿本进程自己的 exe 名去查必须命中。

        只有负路径测试的话，「枚举循环一步都没走」和「匹配正常工作」两种
        情况都表现为 False，看不出差别；这条把命中分支钉住。
        """
        name = os.path.basename(sys.executable).lower()
        with mock.patch.object(dsb, "ZCODE_EXE_NAME", name):
            self.assertTrue(dsb.zcode_app_running())

    def test_invalid_snapshot_handle_fails_open(self):
        with mock.patch.object(dsb, "win") as W:
            W.return_value.create_toolhelp32_snapshot.return_value = \
                ctypes.c_void_p(-1).value
            self.assertTrue(dsb.zcode_app_running())

    def test_api_exception_fails_open(self):
        with mock.patch.object(dsb, "win") as W:
            W.side_effect = OSError("snapshot unavailable")
            self.assertTrue(dsb.zcode_app_running())

    def test_snapshot_handle_closed_even_when_enumeration_raises(self):
        with mock.patch.object(dsb, "win") as W:
            W.return_value.create_toolhelp32_snapshot.return_value = 1234
            W.return_value.process32_first.side_effect = OSError("mid-walk")
            self.assertTrue(dsb.zcode_app_running())   # fail-open
            W.return_value.close_handle.assert_called_once_with(1234)


if __name__ == "__main__":
    unittest.main()
