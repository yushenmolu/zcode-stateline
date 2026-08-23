#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_alive.py — 检查贴边状态条进程是否存活（供 ensure-docked-statusbar.cmd 用）。

读 <data_dir>/statusbar.pid：
  - pid 存活            -> exit 0（cmd 里 `||` 短路，不重复启动）；
  - 无文件 / 坏内容 /
    pid 已死 / 出任何错 -> exit 1，且顺带删除僵尸 pid 文件
                          （cmd 里 `|| start` 重新拉起状态条）。

背景：旧 ensure cmd 用 `if exist statusbar.pid goto :done`，残留的僵尸 pid
文件会永久阻止状态条启动；本脚本把判断从"文件存在"改为"进程真的活着"。
设计为极小脚本：仅标准库 ctypes + os，无第三方依赖；stdout 恒空（结果只
通过 exit code 表达），任何异常按"不存活"处理（fail-safe 允许重启）。

调用：
  python.exe statusbar_alive.py        （exit 0=存活 / 1=不存活）
"""
import ctypes
import os
import sys

DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
PID_NAME = "statusbar.pid"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def main():
    data_dir = os.environ.get("ZCODE_PLUGIN_DATA") or DATA_DIR_DEFAULT
    pidfile = os.path.join(data_dir, PID_NAME)
    try:
        with open(pidfile, "r", encoding="ascii") as f:
            pid = int(f.read().strip())
    except Exception:
        # 无文件 / 空 / 非 pid 内容：视为不存活；清掉坏文件便于重启。
        _remove_quiet(pidfile)
        return 1
    if not _alive(pid):
        _remove_quiet(pidfile)   # 僵尸 pid 文件 -> 删除，放行重启
        return 1
    return 0


def _alive(pid):
    """pid 进程是否存活。OpenProcess 失败视为死亡（与 docked_statusbar 同口径）。"""
    if not pid or pid <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:
        return False


def _remove_quiet(path):
    try:
        os.remove(path)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
