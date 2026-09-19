#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dir_watcher.py — Windows 目录变更监听（纯标准库 ctypes，无第三方依赖）。

用途
----
给常驻状态条（每秒轮询读若干 JSON）用，改为"文件一变就被叫醒"，把秒级轮询
降成毫秒级事件驱动；本模块不读任何业务文件，只回答"哪个文件刚变了"。

模块可安全 import（不建线程、不建句柄、无副作用）；非 Windows 或 kernel32
加载失败时 AVAILABLE=False 且不抛，调用方据此静默降级回轮询。

实现要点（均按本机实测结论写死，勿凭直觉改）
--------------------------------------------
1. 打开目录必须带 FILE_FLAG_BACKUP_SEMANTICS(0x02000000)，否则 CreateFileW
   对目录返回 ACCESS_DENIED；同时带 FILE_FLAG_OVERLAPPED(0x40000000) 走异步 I/O。
2. 所有 Win32 函数显式设置 argtypes/restype。64 位下 HANDLE 是 8 字节指针，
   不声明 restype 会被当 32 位 int，句柄值被截断 → 表现为随机失败。
3. ReadDirectoryChangesW 用自建 OVERLAPPED 结构体 + 手工 CreateEventW 的事件
   等待（不依赖完成例程）。过滤器 = FILE_NAME | SIZE | LAST_WRITE。
4. ReadDirectoryChangesW 返回 False 且 get_last_error() != ERROR_IO_PENDING(997)
   时必须 break 退出循环——否则目录被删/句柄失效时会空转烧 CPU。
5. 解析 FILE_NOTIFY_INFORMATION：FileNameLength 是**字节数**不是字符数，文件名
   是 UTF-16LE 且**不带结尾 \\0**，必须按字节切片再 decode；链表按 NextEntryOffset
   （已 4 字节对齐）前进，0 表示最后一条。
6. 缓冲区溢出：GetOverlappedResult 得到 nBytesReturned == 0 表示"一次变更太多，
   缓冲区装不下"，**不是"没有事件"**。此时必须发一次全量信号 on_change("*")，
   否则会永久丢事件（调用方收到 "*" 应做一次全量刷新）。

去抖策略
--------
一次文件写入通常会连发多条事件（打开/写入/关闭/杀软扫描各一条），故用
debounce_ms 窗口合并：把窗口内收集到的名字放进 set，每次收到新事件都把
"截止时刻"往后顺延 debounce_ms，直到窗口内再无新事件，再统一回调一次
（同名的多条合并成一次）。等待用 WaitForSingleObject(停止事件, 剩余毫秒)，
既攒批又能被 stop() 立刻打断。

资源所有权与退出时序（硬性）
----------------------------
主线程 stop():
  1. SetEvent(self._stop)          # 只发信号，绝不碰其它句柄
  2. thread.join(timeout)          # 等工作线程自己清理并退出
  3. 仅当线程已死：CloseHandle(self._stop)，返回 True
     否则：不关任何句柄，写 stderr，返回 False
     （线程是 daemon，随进程退出；绝不 use-after-close）

工作线程 _run() 的 finally（关自己创建的资源）：
  CancelIoEx(dir_handle, byref(ov))   # 先取消挂起的 I/O
  CloseHandle(ov.hEvent)
  CloseHandle(dir_handle)

铁律：join 之前禁止任何 CloseHandle。谁创建谁关闭——_stop 由主线程在 join 成功
后关，目录句柄与 ov.hEvent 由工作线程 finally 关。

stop() 可重复调用；未 start 时调用也安全；回调抛异常只记 stderr，不杀线程。

用法
----
    from dir_watcher import DirWatcher, AVAILABLE

    if AVAILABLE:
        w = DirWatcher(data_dir, on_change=on_file_changed,
                       names={"status.json", "usage.json"}, debounce_ms=150)
        w.start()          # 打开目录失败会抛 OSError（不静默）
        ...
        w.stop()           # 返回 True 表示线程已干净退出
"""

import ctypes
import os
import struct
import sys
import threading
import time

__all__ = ["DirWatcher", "AVAILABLE"]

# --------------------------------------------------------------------------
# Win32 常量
# --------------------------------------------------------------------------
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000   # 打开目录必须带，否则 ACCESS_DENIED
FILE_FLAG_OVERLAPPED = 0x40000000

FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
WATCH_FILTER = (FILE_NOTIFY_CHANGE_FILE_NAME |
                FILE_NOTIFY_CHANGE_SIZE |
                FILE_NOTIFY_CHANGE_LAST_WRITE)

ERROR_IO_PENDING = 997
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

BUFFER_SIZE = 64 * 1024
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class OVERLAPPED(ctypes.Structure):
    """Win32 OVERLAPPED（目录变更只用到 hEvent，偏移量恒为 0）。"""
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


def _log(msg):
    """后台线程里出错只记 stderr，绝不抛出。"""
    try:
        sys.stderr.write("[dir_watcher] %s\n" % msg)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 平台探测：非 Windows / kernel32 不可用时静默降级，import 不抛
# --------------------------------------------------------------------------
AVAILABLE = False
_kernel32 = None

if sys.platform == "win32":
    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 显式声明签名：不声明则 64 位下 HANDLE 被截断成 32 位
        _kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        _kernel32.CreateFileW.restype = ctypes.c_void_p
        _kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        _kernel32.CreateEventW.restype = ctypes.c_void_p
        _kernel32.ReadDirectoryChangesW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(OVERLAPPED),
            ctypes.c_void_p]
        _kernel32.ReadDirectoryChangesW.restype = ctypes.c_int
        _kernel32.GetOverlappedResult.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(OVERLAPPED),
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
        _kernel32.GetOverlappedResult.restype = ctypes.c_int
        _kernel32.WaitForMultipleObjects.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
            ctypes.c_uint32]
        _kernel32.WaitForMultipleObjects.restype = ctypes.c_uint32
        _kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        _kernel32.SetEvent.restype = ctypes.c_int
        _kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
        _kernel32.ResetEvent.restype = ctypes.c_int
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32.CloseHandle.restype = ctypes.c_int
        _kernel32.CancelIoEx.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(OVERLAPPED)]
        _kernel32.CancelIoEx.restype = ctypes.c_int
        AVAILABLE = True
    except Exception:
        _kernel32 = None
        AVAILABLE = False


class DirWatcher:
    """监听单个目录的文件名/大小/最后写入变更，带去抖与干净退出。

    参数
    ----
    path        要监听的目录（绝对路径；内部会 abspath）
    on_change   回调 on_change(name) -> None，在**工作线程**里调用；
                已知的短名（不含路径）。缓冲区溢出时以 name="*" 回调一次。
    names       关心的文件名集合（小写比较）；None = 全部
    debounce_ms 去抖窗口毫秒数，窗口内同批变更合并为一次回调
    """

    def __init__(self, path, on_change=None, names=None, debounce_ms=150):
        self.path = os.path.abspath(path)
        self._on_change = on_change
        if names is None:
            self._names = None
        else:
            self._names = set(str(n).lower() for n in names)
        try:
            secs = float(debounce_ms) / 1000.0
        except (TypeError, ValueError):
            secs = 0.15
        self._debounce = secs if secs > 0 else 0.0

        self._lock = threading.Lock()
        self._thread = None
        self._stop_handle = None          # 主线程创建，主线程在 join 成功后关闭
        self._ready = threading.Event()   # 工作线程完成"打开目录"后置位
        self._start_error = None          # 打开目录失败原因，交给 start() 抛

    # ---------------------------------------------------------------- 公开 API
    def start(self):
        """启动后台 daemon 线程。目录打不开时抛 OSError（不静默降级）。

        返回时后台线程已完成首次 ReadDirectoryChangesW 挂起（即"已布防"），
        因此 start() 返回之后发生的任何变更都不会漏报。
        """
        if not AVAILABLE:
            raise OSError("DirWatcher is not available on this platform "
                          "(ctypes.WinDLL('kernel32') missing)")

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return                              # 已在跑，幂等
            self._ready = threading.Event()
            self._start_error = None
            stop_h = _kernel32.CreateEventW(None, 1, 0, None)   # 手动复位
            if not stop_h:
                raise OSError(ctypes.get_last_error(), "CreateEventW(stop) failed")
            self._stop_handle = stop_h
            thread = threading.Thread(target=self._run, name="DirWatcher", daemon=True)
            self._thread = thread
            thread.start()
            ready = self._ready

        # 等工作线程报告"目录已打开"或"打开失败"（把失败同步抛给调用方）
        if not ready.wait(5.0):
            _log("start(): worker thread not ready within 5.0s (path=%r)"
                 % self.path)
        err = self._start_error
        if err is not None:
            self._release_after_failed_start(thread)
            raise err

    def stop(self, timeout=3.0):
        """停止监听。True=线程已干净退出（句柄已关）；False=超时未退出。"""
        if not AVAILABLE:
            return True

        cur = threading.current_thread()
        with self._lock:
            thread = self._thread
            stop_h = self._stop_handle

        if stop_h is None:
            with self._lock:
                self._thread = None
            return True

        # 1. 只发信号，绝不在这里碰其它资源
        try:
            _kernel32.SetEvent(stop_h)
        except Exception as exc:
            _log("stop(): SetEvent failed: %r" % (exc,))

        if thread is not None and thread is cur:
            # 在回调里调用 stop()：不能 join 自己
            _log("stop(): called from the watcher thread itself; cannot join, "
                 "returning False")
            return False

        # 2. 等工作线程自己清理并退出
        if thread is not None:
            thread.join(timeout)
            alive = thread.is_alive()
        else:
            alive = False

        # 3. 线程确实死了，才关主线程自己创建的句柄
        if not alive:
            with self._lock:
                if self._stop_handle == stop_h:
                    _kernel32.CloseHandle(stop_h)
                    self._stop_handle = None
                    self._thread = None
            return True

        _log("stop(): worker thread did not exit within %.1fs; leaving handles "
             "open to avoid use-after-close (daemon thread dies with process)"
             % timeout)
        return False

    # ------------------------------------------------------------ 内部：线程
    def _release_after_failed_start(self, thread):
        """start() 收到工作线程的失败结论后，安全释放主线程的资源。"""
        with self._lock:
            if thread is not None:
                thread.join(2.0)
            if thread is None or not thread.is_alive():
                if self._stop_handle is not None:
                    _kernel32.CloseHandle(self._stop_handle)
                    self._stop_handle = None
                self._thread = None
            else:
                _log("start(): worker reported failure but is still alive; "
                     "leaving handles open")

    def _open_dir(self):
        handle = _kernel32.CreateFileW(
            self.path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
            None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE or handle == -1:
            raise OSError(ctypes.get_last_error(),
                          "CreateFileW failed on %r" % self.path)
        return handle

    def _run(self):
        try:
            dir_handle = self._open_dir()
        except OSError as exc:
            self._start_error = exc
            self._ready.set()
            return

        ov = OVERLAPPED()
        event = _kernel32.CreateEventW(None, 1, 0, None)   # 手动复位
        if not event:
            err = ctypes.get_last_error()
            self._start_error = OSError(err, "CreateEventW(overlapped) failed")
            self._ready.set()
            _kernel32.CloseHandle(dir_handle)
            return
        ov.hEvent = event

        buf = ctypes.create_string_buffer(BUFFER_SIZE)
        buf_ptr = ctypes.cast(buf, ctypes.c_void_p)
        try:
            self._loop(dir_handle, ov, buf, buf_ptr)
        finally:
            # 兜底：_loop 无论因何早退（含首次读取就硬失败），都不能让 start() 空等超时
            self._ready.set()
            # 工作线程关自己创建的目录句柄与事件；先取消挂起的 I/O
            try:
                _kernel32.CancelIoEx(dir_handle, ctypes.byref(ov))
            except Exception:
                pass
            try:
                _kernel32.CloseHandle(event)
            except Exception:
                pass
            try:
                _kernel32.CloseHandle(dir_handle)
            except Exception:
                pass

    def _loop(self, h, ov, buf, buf_ptr):
        pending = set()
        deadline = 0.0
        read_pending = False
        handles = (ctypes.c_void_p * 2)(self._stop_handle, ov.hEvent)
        nbytes = ctypes.c_uint32(0)

        while True:
            if not read_pending:
                _kernel32.ResetEvent(ov.hEvent)
                ctypes.set_last_error(0)
                ok = _kernel32.ReadDirectoryChangesW(
                    h, buf_ptr, BUFFER_SIZE, 0, WATCH_FILTER, None,
                    ctypes.byref(ov), None)
                if not ok:
                    err = ctypes.get_last_error()
                    if err != ERROR_IO_PENDING:
                        # 必须退出：否则目录被删时死循环烧 CPU
                        _log("ReadDirectoryChangesW failed (err=%d); loop exits" % err)
                        return
                read_pending = True
                # 已布防才放行 start()：MSDN 明确"从打开目录句柄到第一次
                # ReadDirectoryChangesW 之间的变更不会被上报"，提前放行会丢首个事件
                self._ready.set()

            if pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._dispatch(pending)
                    pending.clear()
                    continue
                wait_ms = max(1, int(remaining * 1000) + 1)
            else:
                wait_ms = INFINITE

            rc = _kernel32.WaitForMultipleObjects(2, handles, 0, wait_ms)

            if rc == WAIT_OBJECT_0:
                return                                   # 0 = stop 信号
            if rc == WAIT_OBJECT_0 + 1:                  # 1 = I/O 完成
                nbytes.value = 0
                if not _kernel32.GetOverlappedResult(
                        h, ctypes.byref(ov), ctypes.byref(nbytes), 0):
                    err = ctypes.get_last_error()
                    _log("GetOverlappedResult failed (err=%d); loop exits" % err)
                    return
                read_pending = False
                try:
                    self._collect(buf.raw[:nbytes.value], pending)
                except Exception as exc:
                    _log("parse failed: %r" % (exc,))
                deadline = time.monotonic() + self._debounce
                continue
            if rc == WAIT_TIMEOUT:
                if pending:
                    self._dispatch(pending)
                    pending.clear()
                continue

            _log("WaitForMultipleObjects failed (rc=%d); loop exits" % rc)
            return

    def _collect(self, raw, pending):
        """解析 FILE_NOTIFY_INFORMATION 链表，命中的名字塞进 pending。"""
        if not raw:
            # nBytesReturned == 0 = 缓冲区溢出（变更太多），不是没有事件
            pending.add("*")
            return

        off = 0
        limit = len(raw)
        while off + 12 <= limit:
            nxt, _action, name_len = struct.unpack_from("<III", raw, off)
            if name_len:
                name = raw[off + 12: off + 12 + name_len].decode("utf-16-le", "replace")
                if name and (self._names is None or name.lower() in self._names):
                    pending.add(name)
            if nxt == 0:
                break
            off += nxt

    def _dispatch(self, pending):
        cb = self._on_change
        if cb is None:
            return
        for name in pending:
            try:
                cb(name)
            except Exception as exc:
                # 回调抛异常不能杀死工作线程
                _log("on_change(%r) raised: %r" % (name, exc))
