#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_alive.py — 检查贴边状态条进程是否存活（供 ensure-docked-statusbar.cmd 用）。

读 <data_dir>/statusbar.pid：
  - pid 存活 且 版本与磁盘 docked_statusbar.py 的 STATUSBAR_VERSION 一致 -> exit 0
    （cmd 里 `||` 短路，不重复启动）；
  - 无文件 / 坏内容 / pid 已死 / 版本不匹配（陈旧实例） -> exit 1，
    顺带清理僵尸 pid 文件（cmd 里 `|| start` 重新拉起状态条）。

版本守卫（防"旧条不死，新条永远起不来"）：
  状态条单实例守卫只看 pid 是否存活，因此旧版实例存活时，新版永远启动不了
  （boot.log 里会出现新版写 boot 后立刻被守卫杀掉的记录）。本脚本在 pid
  存活的基础上，从 statusbar.boot.log 里反查与该 pid 对应的最近一条
  `STATUSBAR_VERSION=... pid=N` 记录，得到该进程实际跑的代码版本；若版本
  存在且 != 磁盘 docked_statusbar.py 的 STATUSBAR_VERSION，判定为"陈旧
  实例"，直接强杀该进程并清 pid，放行 ensure cmd 拉起当前版本。
  取"与 pid 匹配"而非"boot.log 末条"：末条可能是被守卫杀掉的启动尝试，
  其 pid 与 statusbar.pid 不符，取末条会把旧条误判成"当前版本"。
  任何版本读取失败都按"版本未知"处理（不杀，保持存活）——无证据不杀。

设计为极小脚本：仅标准库 ctypes + os + re，无第三方依赖；stdout 恒空
（结果只通过 exit code 表达），任何异常按"不存活"处理（fail-safe 允许重启）。

调用：
  python.exe statusbar_alive.py        （exit 0=存活 / 1=不存活）
"""
import ctypes
import os
import re
import sys

DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
PID_NAME = "statusbar.pid"
BOOT_LOG_NAME = "statusbar.boot.log"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259
# boot.log 行格式：2026-08-25 19:56:42 STATUSBAR_VERSION=0.2.2-fix-20260824 pid=13288
BOOT_ENTRY_RE = re.compile(r"STATUSBAR_VERSION=(\S+)\s+pid=(\d+)")


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
    # pid 存活：反查该进程实际跑的版本，陈旧则强杀并放行重启。
    disk_version = _disk_statusbar_version()
    run_version = _boot_version_for_pid(data_dir, pid)
    if disk_version and run_version and run_version != disk_version:
        _terminate(pid)
        _remove_quiet(pidfile)
        return 1
    return 0


def _disk_statusbar_version():
    """磁盘 docked_statusbar.py 的 STATUSBAR_VERSION（读源码取常量，不 import，
    避免触发其模块级副作用）。失败返回 None -> 不做版本比对（fail-safe 存活）。"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "docked_statusbar.py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'STATUSBAR_VERSION\s*=\s*"([^"]+)"', src)
        return m.group(1) if m else None
    except Exception:
        return None


def _boot_version_for_pid(data_dir, pid):
    """boot.log 里与该 pid 对应的最近一条版本；无记录 / 读失败返回 None。"""
    try:
        with open(os.path.join(data_dir, BOOT_LOG_NAME), "r",
                  encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return None
    ver = None
    for line in lines:
        m = BOOT_ENTRY_RE.search(line)
        if m and int(m.group(2)) == pid:
            ver = m.group(1)
    return ver


def _terminate(pid):
    """强杀陈旧实例（仅版本不匹配时调用；失败静默，由重启兜底）。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if h:
            k32.TerminateProcess(h, 1)
            k32.CloseHandle(h)
    except Exception:
        pass


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
