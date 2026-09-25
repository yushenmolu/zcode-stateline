#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_alive.py — 贴边状态条看门狗：保证 docked_statusbar.py 单实例存活。

0.12.1 起所有拉起路径统一收口到本脚本（互斥锁防双开/重叠心跳打架）：
  - hooks/ensure-docked-statusbar.cmd：SessionStart 钩子原样转发；
  - Windows 计划任务 ZcodeTokenStatsAlive：install.cmd 注册，每 5 分钟心跳。

互斥：
  先对 <data_dir>/statusbar.lock 加排他文件锁（msvcrt.locking LK_NBLCK，
  锁 1 字节）。拿不到锁说明另一个看门狗正在清场/拉起 —— 直接 exit 0
  （心跳重叠无害）。锁只挡看门狗彼此之间，不挡状态条自身；try/finally
  保证释放。

持锁后的流程：
  1) 读 statusbar.pid：
       - PID 活 且 版本与磁盘 docked_statusbar.py 的 STATUSBAR_VERSION 一致
         -> 解锁 exit 0（已有一个活的当前版实例）；
       - PID 活 但 boot.log 反查到的运行版本 != 磁盘版本（陈旧实例，旧版
         还吊着则新版永远起不来）-> 强杀后按死亡处理，走下方清场+拉起；
       - 版本判定证据不足一律按"版本未知"处理：不杀，保持存活，无证据不杀；
  2) PID 死 / pidfile 不存在（或上一步已杀陈旧实例）-> 清场：
       wmic process where "CommandLine like '%docked_statusbar%'" get
       ProcessId 找出所有命令行含 docked_statusbar 的残留 pythonw 并强杀
       （映像名两层安全校验，wmic 自身/无关进程不杀），wmic 再查一遍复核；
  3) 删 pidfile；
  4) 用 pythonw（与本脚本解释器同目录的 pythonw.exe，找不到回退
     sys.executable）以 DETACHED_PROCESS 拉起同目录 docked_statusbar.py
     —— 与父进程完全脱钩，钩子/计划任务退出都不带走状态条；
  5) 等 pidfile 出现（最多 5 秒）；
  6) 校验 pidfile 里的 PID 确为活进程 -> exit 0；否则 exit 1。

仅标准库（msvcrt 文件锁）；stdout 恒空；exit 0=已有活实例或已成功拉起
新实例 / 1=拉起失败。任何异常按 fail-safe 收敛，绝不向调用方抛栈。
"""
import ctypes
import msvcrt
import os
import re
import subprocess
import sys
import time

# 数据目录恒用家目录下真实插件数据目录（与 mark_session.py 同一定义）。
# 注：不读 ZCODE_PLUGIN_DATA——钩子环境下该变量被宿主注入指向另一套空目录
# （data/zcode-token-stats@local/），曾导致标记写错目录、状态条读不到；
# 本脚本若沿用则找不到 pid 文件，恒判"不存活"，SessionStart 每次白拉起。
DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
PID_NAME = "statusbar.pid"
LOCK_NAME = "statusbar.lock"
BOOT_LOG_NAME = "statusbar.boot.log"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
SPAWN_PIDFILE_TIMEOUT_S = 5.0
# 挂死检测：pid 活着但主线程空转（实测 89% CPU）时"无声卡死"。判据：
# 连续两次采样（间隔 HUNG_SAMPLE_INTERVAL_S）CPU 时间增量都超过
# HUNG_CPU_DELTA_S（≈80% 单核），且 kill 前复核进程创建时间未变
# （防 pid 复用误杀）。
HUNG_SAMPLE_INTERVAL_S = 3.0
HUNG_CPU_DELTA_S = 2.5
# boot.log 行格式：2026-08-25 19:56:42 STATUSBAR_VERSION=0.2.2-fix-20260824 pid=13288
BOOT_ENTRY_RE = re.compile(r"STATUSBAR_VERSION=(\S+)\s+pid=(\d+)")


def main():
    data_dir = DATA_DIR_DEFAULT
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception:
        pass
    lock_path = os.path.join(data_dir, LOCK_NAME)
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    except OSError:
        # 数据目录不可写：无法互斥也无法安全拉起 -> 无害退出
        return 0
    locked = False
    try:
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError:
            # 另一个看门狗正在清场/拉起 -> 重叠心跳无害退出
            return 0
        return _watchdog_locked(data_dir)
    except Exception:
        # fail-safe：看门狗自身出错不抛栈，按"拉起失败"退出
        return 1
    finally:
        if locked:
            try:
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _watchdog_locked(data_dir):
    """持锁期间的完整看门狗流程（判活 -> 清场 -> 拉起 -> 校验）。"""
    pidfile = os.path.join(data_dir, PID_NAME)
    pid = _read_pid(pidfile)
    need_spawn = True
    if pid and _alive(pid):
        disk_version = _disk_statusbar_version()
        run_version = _boot_version_for_pid(data_dir, pid)
        if disk_version and run_version and run_version != disk_version:
            # 陈旧实例（旧版本还吊着）-> 强杀后走清场+拉起当前版本
            _terminate(pid)
        else:
            # 版本没问题（或证据不足不杀）——再判一次"假死"：pid 活着但
            # 主线程空转烧 CPU（实测 89%）时 UI 已卡死，必须杀掉重拉。
            hung, _reason = _is_hung(pid)
            if hung:
                try:
                    sys.stderr.write(
                        "statusbar_alive: pid=%d 疑似假死（%s），强杀重拉\n"
                        % (pid, _reason))
                except Exception:
                    pass
                _terminate(pid)
            else:
                need_spawn = False   # 已有一个活的当前版实例（或证据不足不杀）
    if not need_spawn:
        return 0
    # ---- 清场：把单实例守卫之外的残留 docked_statusbar pythonw 全部杀掉 ----
    _kill_residual_statusbars()
    _remove_quiet(pidfile)
    # ---- 拉起新实例（与父进程完全脱钩）----
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "docked_statusbar.py")
    pythonw = _resolve_pythonw()
    try:
        subprocess.Popen(
            [pythonw, script],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception:
        return 1
    # ---- 等 pidfile 出现（最多 5 秒）并校验 PID 存活 ----
    deadline = time.time() + SPAWN_PIDFILE_TIMEOUT_S
    while time.time() < deadline:
        new_pid = _read_pid(pidfile)
        if new_pid and _alive(new_pid):
            return 0
        time.sleep(0.2)
    new_pid = _read_pid(pidfile)
    return 0 if (new_pid and _alive(new_pid)) else 1


def _read_pid(pidfile):
    """读 pidfile；无文件/空/非数字返回 None。"""
    try:
        with open(pidfile, "r", encoding="ascii") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _resolve_pythonw():
    """拉起状态条用的 pythonw.exe：优先取与本脚本解释器同目录的 pythonw.exe
    （本脚本由 pythonw 跑时 sys.executable 即 pythonw；由控制台 python 跑时
    pythonw 就在它旁边）；找不到回退 sys.executable（仍能拉起，只是多一个
    控制台窗——看门狗职责是"必须有状态条"，窗口有无不拦路）。"""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    cand = os.path.join(exe_dir, "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return sys.executable


def _kill_residual_statusbars():
    """清场：wmic 按命令行找所有残留 docked_statusbar 宿主进程并强杀（带
    _safe_to_kill 两层校验，不盲杀）；杀完 wmic 再查一遍复核，仍命中的写
    stderr 留痕（交由下次心跳/状态条自身单实例守卫兜底）。"""
    hit = _find_statusbar_pids_via_wmic()
    if not hit:
        return
    for pid in hit:
        _terminate(pid)
    left = [p for p in _find_statusbar_pids_via_wmic() if _alive(p)]
    if left:
        try:
            sys.stderr.write(
                "statusbar_alive: 清场后仍有 docked_statusbar 残留 pid=%s\n"
                % ",".join(str(p) for p in left))
        except Exception:
            pass


def _find_statusbar_pids_via_wmic():
    """wmic 查询命令行含 docked_statusbar 的进程 PID 列表；查询失败返回 []。

    注：wmic 自身命令行也含该关键词，但其 exe 不是 python(w)，_terminate
    的 _safe_to_kill 校验会拒杀（且输出返回时 wmic 已退出）；本脚本自身
    命令行只含 statusbar_alive.py 不命中，仍加 self 排除兜底。"""
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where",
             "CommandLine like '%docked_statusbar%'", "get", "ProcessId"],
            stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        return []
    pids = []
    try:
        for tok in out.decode("ascii", "replace").split():
            if tok.isdigit():
                pids.append(int(tok))
    except Exception:
        pass
    self_pid = os.getpid()
    return [p for p in pids if p != self_pid]


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


def _image_name(pid):
    """取目标进程 exe 完整路径（QueryFullProcessImageNameW，需
    PROCESS_QUERY_LIMITED_INFORMATION 权限即可）。打不开 / 查询失败
    返回 None -> 一律放弃强杀（不盲杀）。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, False, pid)
        if not h:
            return None
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return buf.value if ok else None
    except Exception:
        return None


def _safe_to_kill(exe_path):
    """映像路径是否像状态条的宿主解释器：basename 必须是
    python(w).exe（硬条件），且完整路径的目录段含常规 Python 安装特征
    （如 ...\\Python312\\、AppData\\Local\\Programs\\Python\\、Anaconda 等，
    统一表现为目录名含 "python"）。防 pid 被系统复用到无关进程
    （explorer.exe / bash 等）时裸 TerminateProcess 误杀。
    注：不比对与脚本同盘符——实测本机状态条由 D:\\Program Files\\python312
    启动而脚本在 C 盘，同盘符硬限会让版本守卫永不生效成死代码；剩两层
    校验 + 拒绝后按"需重启"兜底，风险边界仍在"多留旧实例不杀错"。"""
    base = os.path.basename(exe_path).lower()
    if base not in ("python.exe", "pythonw.exe"):
        return False
    exe_dir = os.path.dirname(exe_path.replace("/", "\\")).lower()
    return "python" in exe_dir


def _terminate(pid):
    """强杀实例（陈旧版本守卫 / 清场残留时调用；失败静默，由重启兜底）。

    M4 安全守卫：pid 可能被系统复用到无关进程，先经 _image_name 校验映像
    名（python/pythonw 且常规 Python 安装特征）才 TerminateProcess；
    打不开进程 / 取不到映像名 / 校验不过一律放弃强杀、原因写 stderr，
    返回 False——多留一个旧实例好过误杀无辜进程。
    """
    exe = _image_name(pid)
    if exe is None:
        try:
            sys.stderr.write(
                "statusbar_alive: pid=%d 打不开或取不到映像名，"
                "放弃强杀（按需重启处理）\n" % pid)
        except Exception:
            pass
        return False
    if not _safe_to_kill(exe):
        try:
            sys.stderr.write(
                "statusbar_alive: pid=%d 映像 %s 不是 python 解释器"
                "（疑似 pid 复用），拒绝强杀\n" % (pid, exe))
        except Exception:
            pass
        return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if h:
            k32.TerminateProcess(h, 1)
            k32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


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


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong)]


def _filetime_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _process_times(pid):
    """取目标进程的 (cpu_seconds, creation_raw)：
      - cpu_seconds：内核+用户态 CPU 时间（秒，float），等价于
        `(Get-Process -Id pid).CPU`；
      - creation_raw：进程创建时间（FILETIME 原始 64 位整数），等价于
        `(Get-Process -Id pid).StartTime`，用于"同一 pid 是不是同一个进程"
        的身份绑定（pid 复用时创建时间会变）。
    打不开进程 / 查询失败返回 None。
    """
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            c, e, kt, ut = (_FILETIME(), _FILETIME(), _FILETIME(), _FILETIME())
            ok = k32.GetProcessTimes(
                h, ctypes.byref(c), ctypes.byref(e),
                ctypes.byref(kt), ctypes.byref(ut))
            if not ok:
                return None
            # FILETIME 是 100 纳秒计数
            cpu_sec = (_filetime_to_int(kt) + _filetime_to_int(ut)) / 1e7
            return cpu_sec, _filetime_to_int(c)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def _is_hung(pid):
    """假死判定：pid 活着但主线程空转烧 CPU（状态条"无声卡死"症状）。

    判据：连续两次采样（间隔 HUNG_SAMPLE_INTERVAL_S）该 pid 的 CPU 时间，
    两次增量都 > HUNG_CPU_DELTA_S（≈80% 单核）则视为假死。

    误杀防护（关键）：判定与处置必须绑定"同一个进程"。两次采样时同时记录
    该 pid 的创建时间；判定为假死后、返回前**第三次**重新查询创建时间，
    与采样记录不一致（进程已退出 / pid 被系统复用）则放弃本次判定，
    返回 False —— 宁可漏杀不可错杀。

    返回 (hung: bool, reason: str)。任何一步查询失败都返回
    (False, 原因)，按"无证据不杀"处理。
    """
    t0 = _process_times(pid)
    if t0 is None:
        return False, "第一次采样失败（进程可能已退出）"
    cpu0, ct0 = t0
    time.sleep(HUNG_SAMPLE_INTERVAL_S)
    t1 = _process_times(pid)
    if t1 is None:
        return False, "第二次采样失败（进程可能已退出）"
    cpu1, ct1 = t1
    if ct1 != ct0:
        return False, "采样间创建时间变化（pid 复用），放弃判定"
    d1 = cpu1 - cpu0
    if d1 <= HUNG_CPU_DELTA_S:
        return False, "CPU 增量 %.2fs 正常" % d1
    time.sleep(HUNG_SAMPLE_INTERVAL_S)
    t2 = _process_times(pid)
    if t2 is None:
        return False, "第三次采样失败（进程可能已退出）"
    cpu2, ct2 = t2
    if ct2 != ct0:
        return False, "采样间创建时间变化（pid 复用），放弃判定"
    d2 = cpu2 - cpu1
    if d2 <= HUNG_CPU_DELTA_S:
        return False, "第二段 CPU 增量 %.2fs 回落" % d2
    # kill 前最后一次身份复核
    t3 = _process_times(pid)
    if t3 is None or t3[1] != ct0:
        return False, "处置前身份复核失败（进程已变），放弃"
    return True, ("连续两段 CPU 增量 %.2fs/%.2fs 均超 %.2fs"
                  % (d1, d2, HUNG_CPU_DELTA_S))


def _remove_quiet(path):
    try:
        os.remove(path)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
