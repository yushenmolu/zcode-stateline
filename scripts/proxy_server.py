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
    异常 / 超时（60s 无数据）置 active=false 并保留精确 usage。

容错原则：不缓存、不缓冲（边收边转发）；上游断连/转发异常只记日志、关闭
该连接，绝不影响 ZCode 对上游的重试（请求失败由客户端自行重试，语义与
直连 8080 一致）。

纯标准库：http.server / socket / json / threading，无第三方依赖。

CLI：
  python proxy_server.py [--listen-port 18080] [--upstream http://127.0.0.1:8080]
                         [--data-dir <dir>] [--state-file <path>] [--log-file <path>]
  python proxy_server.py --once   # 自测：解析预置 SSE 样例并输出计数，不监听
"""
import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.3.0"

LISTEN_HOST_DEFAULT = "127.0.0.1"
LISTEN_PORT_DEFAULT = 18080
UPSTREAM_DEFAULT = "http://127.0.0.1:8080"
DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
STATE_FILE_NAME = "live_stream.json"
LOG_FILE_NAME = "live-proxy.log"
STREAM_READ_TIMEOUT = 60.0   # 上游读超时秒：60s 无数据 -> 置 active=false
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
                self._send_upstream(up_sock, body)
                self._forward_response(sid, up_sock, model, sess)
            finally:
                try:
                    up_sock.close()
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
                counter = SSECounter()
                keeper.begin(sid, model, sess)
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
    ap.add_argument("--once", action="store_true",
                    help="self-test: parse a preset SSE sample and print counts, no listening")
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


def main(argv=None):
    args = parse_args(argv)
    data_dir = args.data_dir or DATA_DIR_DEFAULT
    state_file = args.state_file or os.path.join(data_dir, STATE_FILE_NAME)

    if args.once:
        res = run_once_sample()
        sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
        return 0

    upstream_host, upstream_port = _parse_upstream(args.upstream)
    os.makedirs(data_dir, exist_ok=True)
    log_dir = data_dir

    keeper = StreamKeeper(state_file)
    handler_cls = make_handler(keeper, upstream_host, upstream_port, log_dir)

    try:
        server = ThreadingHTTPServer((args.listen_host, args.listen_port), handler_cls)
    except OSError as e:
        sys.stderr.write("cannot bind %s:%d: %r\n" % (args.listen_host, args.listen_port, e))
        return 1
    server.daemon_threads = True

    _log(log_dir, "boot VERSION=%s pid=%d listen=%s:%d upstream=%s:%d state=%s"
         % (VERSION, os.getpid(), args.listen_host, args.listen_port,
            upstream_host, upstream_port, state_file))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
