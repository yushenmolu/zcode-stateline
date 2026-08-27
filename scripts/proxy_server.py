#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
proxy_server.py — 本地 SSE 反向代理（zcode-token-stats 实时流计数）

背景：ZCode 模型请求走 http://localhost:8080/v1（openai-compatible，SSE 流，
明文 HTTP）。db 落库是「请求完成」级别的，生成过程中状态条 1 秒轮询 db 拿不到
增量数据，无法实时显示「生成中」。

本脚本在 127.0.0.1:18080 起一个透明反向代理：把请求原样转发到上游
127.0.0.1:8080（保持 method / headers / body；Host 改为上游；为保证 SSE 可
解析，转发时去掉请求的 Accept-Encoding 头，让上游返回明文——SSE 客户端对
非压缩响应完全兼容），响应「边收边转发」不缓存不缓冲（socket 逐 chunk 读、
逐 chunk 写，chunked 帧原样透传，不改响应字节），同时：

  - 对 content-type: text/event-stream 的响应按 **SSE 协议**逐事件解析
    （data:/event: 行 + 空行分隔事件、多行 data 合并；chunked 分帧按协议
    解码后喂入，事件按协议边界切分而非 TCP 段）；
  - 统计 choices[].delta.reasoning_content / delta.content 等字段的
    **字符增量**（近似 token）；
  - 流末尾 usage（input/output/cacheRead/total 等）取精确值；
  - 每事件把实时计数**原子写**到 <data-dir>/live_stream.json，供
    docked_statusbar.py 每秒轮询展示「生成中 token / 速度」；流结束 /
    异常 / 超时（180s 无数据）置 active=false 并保留精确 usage。
    0.5.0：**收到请求即写 active=true**（转发开始就落 active，不等上游
    响应头），首 token 前的等待期状态条也能显示「生成中」。

容错原则：不缓存、不缓冲（边收边转发）；上游断连/转发异常只记日志、关闭
该连接，绝不影响 ZCode 对上游的重试（请求失败由客户端自行重试，语义与
直连 8080 一致）。

纯标准库：http.server / socket / json / threading，无第三方依赖。

CLI：
  python proxy_server.py [--listen-port 18080] [--upstream http://127.0.0.1:8080]
                         [--data-dir <dir>] [--state-file <path>] [--log-file <path>]
  python proxy_server.py --once   # 自测：解析预置 SSE 样例并输出计数，不监听
  python proxy_server.py --alive-check [<--listen-port P>] [<--data-dir D>]
                                  # N5 版本守卫：三重正证据门（pid 文件映像 /
                                  # 端口归属 / 日志 VERSION）核查在跑代理。
                                  # 退出码 0=存活同版 1=无实例 2=旧版已清除
                                  # 3=端口被未知进程占用
"""
import argparse
import ctypes
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 0.7.0：N5 版本守卫（--alive-check 三重正证据门）以磁盘上的 __version__ 为准。
__version__ = VERSION = "0.7.0"

LISTEN_HOST_DEFAULT = "127.0.0.1"
LISTEN_PORT_DEFAULT = 18080
UPSTREAM_DEFAULT = "http://127.0.0.1:8080"
DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
STATE_FILE_NAME = "live_stream.json"
LOG_FILE_NAME = "live-proxy.log"
PID_FILE_NAME = "live-proxy.pid"
LOG_ROTATE_BYTES = 5 * 1024 * 1024  # N6：live-proxy.log 超过 5MB 轮转为 .old
STREAM_READ_TIMEOUT = 180.0  # 上游读超时秒（0.5.0 60->180：模型首 token 延迟可达
                             # 20-33s，大上下文更久；180s 无数据才置 active=false，
                             # 可 --stream-timeout 覆盖）
STATE_THROTTLE_S = 0.15      # 状态文件写限流（每秒最多 ~6 次原子写）

# 转发时跳过的请求头（由本代理重设/管理；Accept-Encoding 去掉以保上游返回明文）
_HOP_HEADERS = frozenset((
    "host", "content-length", "connection", "transfer-encoding",
    "proxy-connection", "keep-alive", "expect", "upgrade", "accept-encoding",
))


# ---------------------------------------------------------------------------
# 日志 / 原子写
# ---------------------------------------------------------------------------

def _log(log_dir, msg):
    """追加写 live-proxy.log（UTF-8）。任何失败静默（pythonw 无 console）。"""
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, LOG_FILE_NAME), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _atomic_write_json(path, obj):
    """原子写 JSON（先 .tmp 再 os.replace），失败静默（不崩转发线程）。"""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _rotate_log_if_needed(log_dir):
    """N6：启动前检查 live-proxy.log，超过 LOG_ROTATE_BYTES 轮转为 .old
    （os.replace 覆盖旧 .old）。轮转被占用/失败时静默继续，不阻塞启动。"""
    try:
        path = os.path.join(log_dir, LOG_FILE_NAME)
        if os.path.isfile(path) and os.path.getsize(path) > LOG_ROTATE_BYTES:
            try:
                os.replace(path, path + ".old")
            except OSError:
                pass  # 旧实例仍持有句柄等情况：放弃本次轮转，照常继续
    except Exception:
        pass


def _write_pid_file(data_dir):
    """N5：成功监听后原子写 <data-dir>/live-proxy.pid（tmp + os.replace）。

    在 boot 日志之后调用——pid 文件存在即暗示日志里已有该 pid 的
    VERSION=... 记录（alive-check 门 c 的隐含顺序）。失败静默。
    """
    try:
        path = os.path.join(data_dir, PID_FILE_NAME)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# usage 标准化
# ---------------------------------------------------------------------------

def _normalize_usage(u):
    """把厂商各异的 usage 字段映射为统一键（input/output/cacheRead/total…）。

    兼容 openai 新版字段（input_tokens/output_tokens）与旧字段
    （prompt_tokens/completion_tokens）；缓存读取兼容顶层
    cache_read_input_tokens 与 prompt_tokens_details.cached_tokens。
    返回 dict（含原文 + 标准化键），供 live_stream.json 的 last_usage 使用。
    """
    out = dict(u)
    out["input_tokens"] = u.get("input_tokens", u.get("prompt_tokens"))
    out["output_tokens"] = u.get("output_tokens", u.get("completion_tokens"))
    out["total_tokens"] = u.get("total_tokens")
    pd = u.get("prompt_tokens_details")
    if isinstance(pd, dict):
        out["cache_read_input_tokens"] = pd.get(
            "cached_tokens", u.get("cache_read_input_tokens"))
    else:
        out["cache_read_input_tokens"] = u.get("cache_read_input_tokens")
    out["reasoning_tokens"] = u.get("reasoning_tokens")
    return out


# ---------------------------------------------------------------------------
# SSE 事件解析器（协议级）
# ---------------------------------------------------------------------------

class SSECounter(object):
    """按 SSE 协议解析事件流，统计 content / reasoning_content 的字符增量。

    - 输入为解码后的 SSE 字节流（chunked 分帧由转发层解码后喂入）；
    - data: 行可多条，事件内用 \\n 合并；空行结束一个事件；
    - data 为 "[DONE]" -> done=True；
    - 每个事件解析完调用 on_event()（供转发线程写状态文件）。
    """

    def __init__(self):
        self.buf = b""
        self._data_lines = []
        self._event = "message"
        self.output_chars = 0        # content 字符增量（近似 token）
        self.reasoning_chars = 0     # reasoning_content 字符增量
        self.input_tokens = None     # usage 精确输入（流中可能多次出现，取最新）
        self.normalized_usage = None  # 最新 usage 标准化 dict
        self.done = False
        self.events = 0
        self.on_event = None

    def feed(self, chunk):
        self.buf += chunk
        while not self.done:
            nl = self.buf.find(b"\n")
            if nl < 0:
                break
            line = self.buf[:nl]
            self.buf = self.buf[nl + 1:]
            if line.endswith(b"\r"):
                line = line[:-1]
            self._process_line(line)
        if self.done:
            self.buf = b""

    def _process_line(self, line):
        if line == b"":
            self._dispatch()
            return
        if line.startswith(b":"):
            return  # 注释行
        if line.startswith(b"data:"):
            self._data_lines.append(line[5:].lstrip(b" ").decode("utf-8", "replace"))
        elif line.startswith(b"event:"):
            self._event = line[6:].strip().decode("utf-8", "replace")
        # id: / retry: 忽略（对计数无影响）

    def _dispatch(self):
        if not self._data_lines:
            return
        data = "\n".join(self._data_lines)
        self._data_lines = []
        self.events += 1
        if data == "[DONE]":
            self.done = True
            return
        try:
            obj = json.loads(data)
        except Exception:
            return
        self._parse(obj)
        if self.on_event:
            try:
                self.on_event()
            except Exception:
                pass

    def _parse(self, obj):
        if not isinstance(obj, dict):
            return
        usage = obj.get("usage")
        if isinstance(usage, dict) and usage:
            self.normalized_usage = _normalize_usage(usage)
            self.input_tokens = self.normalized_usage.get("input_tokens")
        choices = obj.get("choices")
        if isinstance(choices, list):
            for ch in choices:
                if not isinstance(ch, dict):
                    continue
                delta = ch.get("delta")
                if isinstance(delta, dict):
                    rc = delta.get("reasoning_content")
                    if isinstance(rc, str):
                        self.reasoning_chars += len(rc)
                    c = delta.get("content")
                    if isinstance(c, str):
                        self.output_chars += len(c)
                # 兼容非 delta 增量形态（choices[0].text 旧式 / message 整段）
                t = ch.get("text")
                if isinstance(t, str):
                    self.output_chars += len(t)
                msg = ch.get("message")
                if isinstance(msg, dict):
                    rc = msg.get("reasoning_content")
                    if isinstance(rc, str):
                        self.reasoning_chars += len(rc)
                    c = msg.get("content")
                    if isinstance(c, str):
                        self.output_chars += len(c)


# ---------------------------------------------------------------------------
# 共享状态（活跃流集合 + 原子写 live_stream.json）
# ---------------------------------------------------------------------------

class StreamKeeper(object):
    """跟踪所有活跃转发流的实时计数，把「最新启动的活跃流」写入状态文件。

    active = 至少一个流在转发；计数/模型取最新启动流（主请求通常就是它）。
    流结束 / 无流时 active=false，并保留最近一次精确 usage 与最终计数
    （供状态条展示「精确 usage」视图）。
    写限流 STATE_THROTTLE_S，begin/finish 强制写。
    """

    def __init__(self, state_file):
        self.state_file = state_file
        self._lock = threading.Lock()
        self._streams = {}          # sid -> 计数 dict
        self._last_usage = None     # 最近结束流的精确 usage
        self._last_usage_ts = None
        self._last_counts = None    # 最近结束流的最终计数（无流时状态文件仍展示）
        self._last_write = 0.0

    def begin(self, sid, model, session_id):
        with self._lock:
            self._streams[sid] = {
                "started_at": time.time(),
                "last_event_at": time.time(),
                "model": model,
                "session_id": session_id,
                "output_chars": 0,
                "reasoning_chars": 0,
                "input_tokens": None,
                "usage": None,
            }
            self._write_locked(force=True)

    def update(self, sid, output_chars, reasoning_chars, input_tokens, usage):
        with self._lock:
            s = self._streams.get(sid)
            if s is None:
                return
            s["output_chars"] = output_chars
            s["reasoning_chars"] = reasoning_chars
            s["input_tokens"] = input_tokens
            s["last_event_at"] = time.time()
            if usage:
                s["usage"] = usage
            self._write_locked(force=False)

    def finish(self, sid, usage):
        with self._lock:
            s = self._streams.pop(sid, None)
            if s is not None:
                s["last_event_at"] = time.time()
                if usage:
                    self._last_usage = usage
                    self._last_usage_ts = time.time()
                # 保存最终计数：流移除后状态文件仍展示最近一次请求的精确结果
                self._last_counts = {
                    "output_chars": s["output_chars"],
                    "reasoning_chars": s["reasoning_chars"],
                    "input_tokens": s["input_tokens"],
                }
            self._write_locked(force=True)

    def _snapshot(self):
        """最新启动的活跃流；无流返回 None。"""
        if not self._streams:
            return None
        return max(self._streams.values(), key=lambda s: s["started_at"])

    def _write_locked(self, force=False):
        now = time.time()
        if not force and (now - self._last_write) < STATE_THROTTLE_S:
            return
        self._last_write = now
        snap = self._snapshot()
        if snap is None:
            lc = self._last_counts or {}
            obj = {
                "active": False,
                "sessionId": None,
                "model": None,
                "stream_input_tokens": lc.get("input_tokens"),
                "stream_output_chars": lc.get("output_chars", 0),
                "stream_reasoning_chars": lc.get("reasoning_chars", 0),
                "last_usage": self._last_usage,
                "started_at": self._last_usage_ts,
                "last_event_at": self._last_usage_ts,
                "ts": now,
            }
        else:
            obj = {
                "active": True,
                "sessionId": snap["session_id"],
                "model": snap["model"],
                "stream_input_tokens": snap["input_tokens"],
                "stream_output_chars": snap["output_chars"],
                "stream_reasoning_chars": snap["reasoning_chars"],
                "last_usage": snap["usage"] or self._last_usage,
                "started_at": snap["started_at"],
                "last_event_at": snap["last_event_at"],
                "ts": now,
            }
        _atomic_write_json(self.state_file, obj)


# ---------------------------------------------------------------------------
# 转发 handler（socket 层透明转发：逐 chunk 边收边转，chunked 帧原样透传）
# ---------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "zcode-live-proxy/" + VERSION

    # 由 make_handler 注入（类属性，Server 内共享）
    keeper = None
    upstream_host = "127.0.0.1"
    upstream_port = 8080
    log_dir = None

    # ---------- 各种 method 统一走 _forward（透明转发） ----------
    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_PUT(self):
        self._forward()

    def do_PATCH(self):
        self._forward()

    def do_DELETE(self):
        self._forward()

    def do_HEAD(self):
        self._forward()

    def do_OPTIONS(self):
        self._forward()

    def log_message(self, fmt, *args):
        _log(self.log_dir, "http: " + (fmt % args))

    # ---------- 请求体 ----------
    def _read_body(self):
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                n = int(cl)
            except Exception:
                n = 0
            if n > 0:
                try:
                    return self.rfile.read(n)
                except Exception:
                    return None
        return None

    def _extract_meta(self, body):
        """从请求体 / 头提取 model 与 sessionId（尽力而为，取不到为 None）。"""
        model, sess = None, None
        if body:
            try:
                obj = json.loads(body.decode("utf-8", "replace"))
                if isinstance(obj, dict):
                    model = obj.get("model") or None
                    for k in ("session_id", "sessionId"):
                        v = obj.get(k)
                        if v:
                            sess = v
                            break
            except Exception:
                pass
        if not sess:
            for hk in ("x-session-id", "x-claude-session-id", "x-zcode-session-id"):
                v = self.headers.get(hk)
                if v:
                    sess = v
                    break
        return model, sess

    # ---------- 转发 ----------
    def _forward(self):
        sid = "%d-%x" % (os.getpid(), id(self))
        try:
            body = self._read_body()
            model, sess = self._extract_meta(body)
            try:
                up_sock = socket.create_connection(
                    (self.upstream_host, self.upstream_port),
                    timeout=STREAM_READ_TIMEOUT)
            except Exception as e:
                _log(self.log_dir, "upstream connect failed: %r" % (e,))
                self._send_bad_gateway()
                return
            try:
                # 0.5.0：收到请求即写 active=true（不等上游响应头）——首 token
                # 前的等待期状态条也能显示「生成中」；非 SSE 请求也会短暂 active
                # 后由下方 finally 的 finish 置回 false。
                if self.keeper is not None:
                    self.keeper.begin(sid, model, sess)
                self._send_upstream(up_sock, body)
                self._forward_response(sid, up_sock, model, sess)
            finally:
                try:
                    up_sock.close()
                except Exception:
                    pass
                # 0.5.0：请求结束清理活跃流（SSE 已在 _forward_response 里带
                # usage finish；这里对非 SSE/异常路径兜底，重复 finish 是 no-op）。
                if self.keeper is not None:
                    try:
                        self.keeper.finish(sid, None)
                    except Exception:
                        pass
        except Exception as e:
            _log(self.log_dir, "handler error: %r" % (e,))
            try:
                self.close_connection = True
            except Exception:
                pass

    def _send_bad_gateway(self):
        """上游不可达：返回 502（ZCode 侧照常可重试，语义与直连 8080 一致）。"""
        msg = ("bad gateway: upstream %s:%d unreachable"
               % (self.upstream_host, self.upstream_port)).encode("utf-8")
        try:
            self.send_response_only(502, "Bad Gateway")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(msg)
            self.wfile.flush()
        except Exception:
            pass

    def _send_upstream(self, sock, body):
        """把客户端请求原样转发到上游（请求行 + Host 改 8080 + 头 + body）。"""
        lines = ["%s %s HTTP/1.1" % (self.command, self.path)]
        lines.append("Host: %s:%d" % (self.upstream_host, self.upstream_port))
        for k, v in self.headers.items():
            if k.lower() in _HOP_HEADERS:
                continue
            lines.append("%s: %s" % (k, v))
        if body is not None:
            lines.append("Content-Length: %d" % len(body))
        lines.append("Connection: close")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        if body:
            sock.sendall(body)

    def _forward_response(self, sid, sock, model, sess):
        """读上游响应头 + 逐 chunk 边收边转发响应体；SSE 响应顺带计数。"""
        f = sock.makefile("rb")
        try:
            status_line = f.readline()
            if not status_line:
                raise ConnectionError("empty status line from upstream")
            parts = status_line.decode("latin-1", "replace").split(" ", 2)
            try:
                status = int(parts[1])
            except Exception:
                status = 502
            reason = parts[2].strip() if len(parts) > 2 else ""

            headers = {}
            raw_pairs = []
            while True:
                line = f.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                s = line.rstrip(b"\r\n").decode("latin-1", "replace")
                k, _, v = s.partition(":")
                raw_pairs.append((k.strip(), v.strip()))
                headers[k.strip().lower()] = v.strip()

            te = headers.get("transfer-encoding", "").lower()
            cl = headers.get("content-length")
            ctype = headers.get("content-type", "").lower()
            cenc = headers.get("content-encoding", "identity").lower()
            upstream_chunked = te == "chunked"
            is_sse = ctype.startswith("text/event-stream") \
                and cenc in ("", "identity")

            try:
                self.send_response_only(status, reason)
            except Exception:
                return
            for k, v in raw_pairs:
                if k.lower() in ("transfer-encoding", "content-length",
                                 "connection", "keep-alive",
                                 "proxy-connection", "upgrade", "expect"):
                    continue
                try:
                    self.send_header(k, v)
                except Exception:
                    pass
            if upstream_chunked:
                try:
                    self.send_header("Transfer-Encoding", "chunked")
                except Exception:
                    pass
            elif cl:
                try:
                    self.send_header("Content-Length", cl)
                except Exception:
                    pass
            try:
                self.send_header("Connection", "close")
            except Exception:
                pass
            try:
                self.end_headers()
            except Exception:
                return

            keeper = self.keeper
            counter = None
            if is_sse and keeper is not None:
                # 0.5.0：begin 已在 _forward 上游连接后调用（收到请求即 active）；
                # 这里只挂事件回调更新计数。
                counter = SSECounter()
                counter.on_event = lambda: keeper.update(
                    sid, counter.output_chars, counter.reasoning_chars,
                    counter.input_tokens, counter.normalized_usage)

            try:
                self._pump_body(f, upstream_chunked, cl, counter)
                if counter is not None and keeper is not None:
                    keeper.finish(sid, counter.normalized_usage)
            except Exception as e:
                _log(self.log_dir, "stream error: %r" % (e,))
                if counter is not None and keeper is not None:
                    try:
                        keeper.finish(sid, counter.normalized_usage)
                    except Exception:
                        pass
                try:
                    self.close_connection = True
                except Exception:
                    pass
        finally:
            try:
                f.close()
            except Exception:
                pass

    def _pump_body(self, f, upstream_chunked, cl, counter):
        """逐 chunk 边读边转发；chunked 帧原样透传（不改响应字节）。
        SSE 计数：done 后停止喂解析器，但转发继续到流自然结束
        （保证客户端收到完整的 chunked 终止块 / EOF）。"""
        if upstream_chunked:
            while True:
                size_line = f.readline()
                if not size_line:
                    break
                size_line = size_line.strip()
                try:
                    size = int(size_line.split(b";")[0], 16)
                except Exception:
                    break
                self.wfile.write(size_line + b"\r\n")
                if size == 0:
                    while True:
                        t = f.readline()
                        if t in (b"\r\n", b"\n", b""):
                            break
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    break
                data = f.read(size)
                self.wfile.write(data)
                self.wfile.write(f.read(2))  # chunk 尾部 CRLF
                self.wfile.flush()
                if counter is not None and not counter.done:
                    counter.feed(data)
            return
        if cl:
            remaining = int(cl)
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
                if counter is not None and not counter.done:
                    counter.feed(chunk)
            return
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
            if counter is not None and not counter.done:
                counter.feed(chunk)


# ---------------------------------------------------------------------------
# --once 自测：预置 SSE 样例
# ---------------------------------------------------------------------------

ONCE_SAMPLE_LINES = [
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1700000000,'
    '"model":"test-model","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1700000000,'
    '"model":"test-model","choices":[{"index":0,"delta":{"reasoning_content":"思考过程一二三"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1700000000,'
    '"model":"test-model","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1700000000,'
    '"model":"test-model","choices":[{"index":0,"delta":{"content":"，世界"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1700000000,'
    '"model":"test-model","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":6,'
    '"total_tokens":17,"prompt_tokens_details":{"cached_tokens":3}}}',
    'data: [DONE]',
]
ONCE_SAMPLE = ("\n\n".join(ONCE_SAMPLE_LINES) + "\n\n").encode("utf-8")


def run_once_sample():
    """解析预置 SSE 样例并输出计数（自测用，不监听端口）。"""
    c = SSECounter()
    c.feed(ONCE_SAMPLE)
    return {
        "ok": True,
        "mode": "once",
        "version": VERSION,
        "events": c.events,
        "output_chars": c.output_chars,
        "reasoning_chars": c.reasoning_chars,
        "approx_output_tokens": c.output_chars,
        "input_tokens": c.input_tokens,
        "usage": c.normalized_usage,
        "done": c.done,
    }


# ---------------------------------------------------------------------------
# --alive-check：版本守卫（三重正证据门，缺一不杀）
# ---------------------------------------------------------------------------

# 退出码约定（ensure-proxy.cmd 按此分流）：
ALIVE_RC_OK = 0       # 同版本实例存活且健康，无需动作
ALIVE_RC_NONE = 1     # 无实例（端口空闲，可直接启动）
ALIVE_RC_KILLED = 2   # 已确认的旧版实例被清除，应启动新实例
ALIVE_RC_UNKNOWN = 3  # 端口被身份未知的进程占用，不能动作

_ALIVE_PYTHON_BASENAMES = frozenset(("python.exe", "pythonw.exe"))
_ALIVE_KILL_WAIT_MS = 3000   # TerminateProcess 后等进程退出的上限
_ALIVE_PORT_FREE_POLL_S = 0.5
_ALIVE_PORT_FREE_POLLS = 6   # 杀旧后最多再等 ~3s 确认端口释放


def _say(msg):
    sys.stdout.write(msg + "\n")


def _win_listening_pids_on_port(port):
    """GetExtendedTcpTable 枚举 IPv4 TCP LISTEN 行，返回监听 port 的 pid 集合。

    返回 None 表示枚举失败（按「身份未知」路径处理，绝不据此杀进程）。
    行结构 MIB_TCPROW_OWNER_PID 前几个字段固定：dwState / dwLocalAddr /
    dwLocalPort / dwRemoteAddr / dwRemotePort / dwOwningPid；行尾可能随
    SDK 版本追加 liCreateTimestamp 等，故行距按实际缓冲大小推算。
    """
    if os.name != "nt":
        return None
    try:
        iphlpapi = ctypes.windll.iphlpapi
        iphlpapi.GetExtendedTcpTable.restype = ctypes.c_ulong
        iphlpapi.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        AF_INET = 2
        TCP_TABLE_OWNER_PID_LISTENER = 3      # 仅 LISTEN 行、带 owning pid
        ERROR_INSUFFICIENT_BUFFER = 122
        size = ctypes.c_ulong(0)
        ret = iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False, AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret != ERROR_INSUFFICIENT_BUFFER or size.value == 0:
            return None
        buf = ctypes.create_string_buffer(int(size.value))
        ret = iphlpapi.GetExtendedTcpTable(
            buf, ctypes.byref(size), False, AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret != 0:
            return None
        raw = buf.raw[:int(size.value)]
        n = struct.unpack_from("<I", raw, 0)[0]
        if n == 0 or size.value <= 4:
            return set()
        rowsize = (size.value - 4) // n
        if rowsize < 24:                      # 固定前缀至少到 dwOwningPid
            return None
        pids = set()
        for i in range(n):
            f = struct.unpack_from("<6I", raw, 4 + i * rowsize)
            state, _laddr, lport_net, _raddr, _rport, owner = f
            if state != 2:                    # MIB_TCP_STATE_LISTEN
                continue
            lport = ((lport_net & 0xFF) << 8) | ((lport_net >> 8) & 0xFF)
            if lport == port:
                pids.add(owner)
        return pids
    except Exception:
        return None


def _port_busy_probe(port):
    """连接探活兜底（tcp 表枚举不可用时判定端口是否被占）。"""
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _win_process_image_basename(pid):
    """OpenProcess + QueryFullProcessImageNameW 取进程映像 basename；失败 None。"""
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong)]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(int(size.value))
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return None
            return os.path.basename(buf.value)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def _win_terminate_process(pid, exit_code=1):
    """TerminateProcess 结束进程并等待其退出；成功 True。

    仅在三重正证据门全过后调用。
    """
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.TerminateProcess.restype = ctypes.c_int
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        PROCESS_TERMINATE = 0x0001
        h = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        try:
            ok = bool(k32.TerminateProcess(h, exit_code))
            if ok:
                k32.WaitForSingleObject(h, _ALIVE_KILL_WAIT_MS)
            return ok
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _read_pid_file(data_dir):
    """读 live-proxy.pid；缺失/损坏/非法返回 None。"""
    try:
        with open(os.path.join(data_dir, PID_FILE_NAME), "r",
                  encoding="utf-8") as f:
            txt = f.read().strip()
        pid = int(txt)
        return pid if pid > 0 else None
    except Exception:
        return None


def _latest_log_version_for_pid(log_dir, pid):
    """live-proxy.log 中该 pid 最近一条 VERSION=xxx；找不到返回 None。"""
    try:
        with open(os.path.join(log_dir, LOG_FILE_NAME), "r",
                  encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return None
    pat = re.compile(r"VERSION=(\S+)\s+pid=%d\b" % int(pid))
    found = None
    for m in pat.finditer(text):
        found = m.group(1)
    return found


def _port_free(listen_port):
    lst = _win_listening_pids_on_port(listen_port)
    if lst is None:
        return not _port_busy_probe(listen_port)
    return not lst


def _run_alive_check(listen_port, data_dir):
    """N5 三重正证据门：

    门 a：pid 文件存在 -> OpenProcess 校验映像 basename 是 python/pythonw；
    门 b：GetExtendedTcpTable 确认该 pid 正持有监听端口；
    门 c：live-proxy.log 中该 pid 最近一条 VERSION 与磁盘 __version__ 一致。

    任一环节查不到/失败 = 身份未知，绝不杀。三条全过且日志版本更旧时才
    TerminateProcess 清除旧版。
    """
    log_dir = data_dir
    _say("[alive-check] port=%d data-dir=%s expect-version=%s"
         % (listen_port, data_dir, __version__))

    listeners = _win_listening_pids_on_port(listen_port)
    if listeners is None:
        # 表枚举失败：退化用连接探活只判「是否有东西在听」，归属未知。
        busy = _port_busy_probe(listen_port)
        _say("[alive-check] tcp-table unavailable; fallback probe busy=%s"
             % busy)
        if not busy:
            return ALIVE_RC_NONE
        return ALIVE_RC_UNKNOWN
    busy = bool(listeners)

    if not busy:
        _say("[alive-check] nothing listens on %d -> no instance" % listen_port)
        return ALIVE_RC_NONE

    # ---- 门 a：pid 文件 + 映像校验 ----
    pid = _read_pid_file(data_dir)
    if pid is None:
        _say("[alive-check] port busy but pid file missing/unreadable "
             "-> unknown holder")
        return ALIVE_RC_UNKNOWN
    base = _win_process_image_basename(pid)
    if base is None:
        _say("[alive-check] cannot inspect image of pid %d (gone or denied) "
             "-> unknown holder" % pid)
        return ALIVE_RC_UNKNOWN
    if base.lower() not in _ALIVE_PYTHON_BASENAMES:
        _say("[alive-check] pid %d image %r is not python/pythonw "
             "-> unknown holder" % (pid, base))
        return ALIVE_RC_UNKNOWN

    # ---- 门 b：端口归属 ----
    if pid not in listeners:
        _say("[alive-check] pid %d does not own listen port %d "
             "(holders=%s) -> unknown holder" % (pid, listen_port, listeners))
        return ALIVE_RC_UNKNOWN

    # ---- 门 c：日志版本比对 ----
    ver = _latest_log_version_for_pid(log_dir, pid)
    if ver is None:
        _say("[alive-check] no VERSION record in log for pid %d "
             "-> unknown identity" % pid)
        return ALIVE_RC_UNKNOWN
    if ver == __version__:
        _say("[alive-check] same-version proxy alive (pid=%d v%s)" % (pid, ver))
        return ALIVE_RC_OK

    # 三门全过 + 版本不同 -> 杀旧放行
    _say("[alive-check] outdated proxy confirmed "
         "(pid=%d log-v%s != disk-v%s) -> terminating" % (pid, ver, __version__))
    if not _win_terminate_process(pid):
        _say("[alive-check] TerminateProcess failed on pid %d -> leave as is"
             % pid)
        return ALIVE_RC_UNKNOWN
    for _ in range(_ALIVE_PORT_FREE_POLLS):
        if _port_free(listen_port):
            break
        time.sleep(_ALIVE_PORT_FREE_POLL_S)
    _say("[alive-check] old proxy cleared (pid=%d) -> should start" % pid)
    return ALIVE_RC_KILLED


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="ZCode local SSE reverse proxy (18080 -> 8080) with live token counting",
    )
    ap.add_argument("--listen-host", default=LISTEN_HOST_DEFAULT,
                    help="bind host (default 127.0.0.1)")
    ap.add_argument("--listen-port", type=int, default=LISTEN_PORT_DEFAULT,
                    help="listen port (default 18080)")
    ap.add_argument("--upstream", default=UPSTREAM_DEFAULT,
                    help="upstream base URL (default http://127.0.0.1:8080)")
    ap.add_argument("--data-dir", default=None,
                    help="data dir for logs (default plugin data dir)")
    ap.add_argument("--state-file", default=None,
                    help="live state json path (default <data-dir>/live_stream.json)")
    ap.add_argument("--log-file", default=None,
                    help="log file path (default <data-dir>/live-proxy.log)")
    ap.add_argument("--stream-timeout", type=float, default=None,
                    help="upstream read timeout in seconds (default %g)"
                         % STREAM_READ_TIMEOUT)
    ap.add_argument("--once", action="store_true",
                    help="self-test: parse a preset SSE sample and print counts, no listening")
    ap.add_argument("--alive-check", action="store_true",
                    help="N5 version guard: verify the running proxy's identity "
                         "(pid file image / port ownership / logged VERSION); "
                         "exit 0=alive same version, 1=no instance, "
                         "2=outdated cleared (should start), "
                         "3=port held by unknown process")
    return ap.parse_args(argv)


def _parse_upstream(url):
    if url.startswith("http://"):
        rest = url[len("http://"):]
    elif url.startswith("https://"):
        sys.stderr.write("https upstream not supported (plain HTTP only)\n")
        raise SystemExit(2)
    else:
        rest = url
    if "/" in rest:
        rest = rest.split("/", 1)[0]
    host, _, port = rest.partition(":")
    port = int(port) if port else 8080
    return host, port


def make_handler(keeper, upstream_host, upstream_port, log_dir):
    class _H(ProxyHandler):
        pass
    _H.keeper = keeper
    _H.upstream_host = upstream_host
    _H.upstream_port = upstream_port
    _H.log_dir = log_dir
    return _H


class ProxyHTTPServer(ThreadingHTTPServer):
    """N4：ThreadingHTTPServer 基类 allow_reuse_address=1（SO_REUSEADDR），
    Windows 下允许第二个进程 bind 同端口也成功，双实例并存、连接随机分流。
    关闭之：第二个实例 bind 必失败，走 main() 既有的「bind 失败返回 1」路径。"""

    allow_reuse_address = False


def main(argv=None):
    args = parse_args(argv)
    data_dir = args.data_dir or DATA_DIR_DEFAULT
    state_file = args.state_file or os.path.join(data_dir, STATE_FILE_NAME)

    if args.once:
        res = run_once_sample()
        sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
        return 0

    if args.alive_check:
        # N5 版本守卫模式。意外异常兜底返回 1（= 尝试启动）：端口真被占时
        # 新实例会被 N4 的 bind 失败路径安全拦下，不会造成双实例。
        try:
            return _run_alive_check(args.listen_port, data_dir)
        except Exception:
            import traceback
            traceback.print_exc(file=sys.stderr)
            return ALIVE_RC_NONE

    # 0.5.0：--stream-timeout 覆盖全局超时（默认 180s）
    timeout = args.stream_timeout
    if timeout is not None and timeout > 0:
        global STREAM_READ_TIMEOUT
        STREAM_READ_TIMEOUT = timeout

    upstream_host, upstream_port = _parse_upstream(args.upstream)
    os.makedirs(data_dir, exist_ok=True)
    log_dir = data_dir
    _rotate_log_if_needed(log_dir)  # N6：超 5MB 先轮转为 .old 再启动写新日志

    keeper = StreamKeeper(state_file)
    handler_cls = make_handler(keeper, upstream_host, upstream_port, log_dir)

    try:
        server = ProxyHTTPServer((args.listen_host, args.listen_port), handler_cls)
    except OSError as e:
        sys.stderr.write("cannot bind %s:%d: %r\n" % (args.listen_host, args.listen_port, e))
        return 1
    server.daemon_threads = True

    _log(log_dir, "boot VERSION=%s pid=%d listen=%s:%d upstream=%s:%d "
         "stream_timeout=%.0fs state=%s"
         % (VERSION, os.getpid(), args.listen_host, args.listen_port,
            upstream_host, upstream_port, STREAM_READ_TIMEOUT, state_file))
    _write_pid_file(data_dir)  # N5：boot 日志落定后声明 pid（供 alive-check 核查）
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
