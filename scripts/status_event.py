#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
status_event.py — 把 ZCode 钩子时序事件原子写入数据目录 status-state.json。

由 ZCode hook 调用，记录「每个会话各自的活动阶段」：
  - UserPromptSubmit  -> generating（用户发消息，本轮开始）
  - PreToolUse / PermissionRequest -> tool（模型在跑工具 / 等授权）
  - PostToolUse / PostToolUseFailure -> generating（工具回到模型，本轮继续）
  - Stop              -> idle（一轮结束）

写出的文件为 v2 分片结构（0.9.0）：
    {"version": 2,
     "sessions": {"<sid>": {"event","ts","hook","turn_started_at","session_id"}, ...},
     "order": ["<sid>", ...]}          # 尾部最新，超出上限从头裁剪
读端（docked_statusbar.py）按当前会话直接取自己那条记录。

为什么分片（v1 是全局单条整体覆盖）：ZCode 只有一份 status-state.json，
多会话并发时后写者覆盖前写者，A 会话的徽标会被 B 会话的事件污染——
读端加「sid 不符即视为空闲」的校验只是掩盖，副作用是 B 在干活时 A 恒显示
空闲。分片后每个会话有自己的槽位，互不覆盖，读端拿到的一定是自己那份。

`hook` 与 `turn_started_at` 的用途：
  - hook：区分「同一条 generating 是本轮开始还是工具回落」。DB 轮次行只在轮次
    结束时落库（实测 completed_at 为 NULL 的行数为 0），因此「turn 行的收尾时刻
    >= 最后一条钩子事件」是本轮已结束的权威证据；但若最后一条事件是
    UserPromptSubmit（新一轮刚开始），该结论必须让位给「正在生成」。没有 hook
    字段就无法区分这两者，只能靠时间容差猜，会把新一轮压成空闲。
  - turn_started_at：本轮起点（只在 UserPromptSubmit 时置位，工具事件不覆盖）。
    读端据此判定 DB 里的 turn 行属不属于本轮（本轮统计不再冒用上一轮数字）。

失败容错（hook 绝不失败，与 mark_session.py / inject_context.py / record_usage.py
同约定）：任何异常均吞掉，stdout 恒输出合法空 JSON "{}" 且 exit 0，绝不使
钩子失败或阻塞会话。

调用：python status_event.py <event> [<session_id>] [<hook_name>] [--data-dir <path>]
  - event 必填，取值 generating / tool / idle（其他取值拒绝写入但仍 exit 0）。
  - 数据目录优先级：--data-dir > 默认插件数据目录（固定，钩子与 GUI 同目录）。
  - session_id 获取优先级（stdin 优先，官方钩子输入机制）：
      1) stdin 传入的 hook 输入 JSON 的 "session_id" 公共字段；
      2) argv[2]（hooks.json 里 "${CLAUDE_SESSION_ID}" 模板展开的兜底参数）；
      3) 都为空 -> 记为 null（事件本身仍记录）。
  - hook_name 获取优先级：stdin 的 "hook_event_name" -> argv[3] -> None。
"""
import argparse
import json
import os
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
STATUS_STATE_NAME = "status-state.json"
LOCK_NAME = "status-state.lock"

VALID_EVENTS = ("generating", "tool", "idle")

STATE_VERSION = 2
MAX_SESSION_SLOTS = 16          # 分片上限：超出按 order 从头裁剪（防文件无界增长）
LOCK_TIMEOUT_S = 1.0            # 跨进程读-改-写的等锁上限
LOCK_POLL_S = 0.01
STALE_LOCK_S = 2.0              # 超过此龄的锁视为持锁进程已死，直接抢占


def read_session_id_from_stdin():
    """尝试从 stdin 的 hook 输入 JSON 里取 session_id（官方钩子输入机制）。

    容错：stdin 不可读 / 是交互终端 / 为空 / 非 JSON / 无 session_id 字段 /
    读取抛任何异常 -> 一律返回 None（调用方回退 argv），绝不抛错、不阻塞。
    """
    sid, _hook = read_hook_input_from_stdin()
    return sid


def read_hook_input_from_stdin():
    """从 stdin 的 hook 输入 JSON 取 (session_id, hook_event_name)。

    两者各自缺失时为 None；任何异常一律降级为 (None, None)，绝不抛错。
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None, None
        raw = sys.stdin.read()
    except Exception:
        return None, None
    if not raw:
        return None, None
    try:
        data = json.loads(raw.lstrip("\ufeff"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    sid = data.get("session_id")
    sid = sid.strip() if (isinstance(sid, str) and sid.strip()) else None
    hook = data.get("hook_event_name")
    hook = hook.strip() if (isinstance(hook, str) and hook.strip()) else None
    return sid, hook


def is_subagent_session(session_id):
    """session_id 是否属于 subagent 会话（ZCode 给子代理开的独立会话）。

    子代理会被 ZCode 以独立 session_id 运行（形如 ``sess_subagent_*``），其
    UserPromptSubmit / PostToolUse / Stop 同样会触发本钩子；主会话状态条不该
    被子代理的活动阶段代表，故这里与 mark_session.py / docked_statusbar.py
    同款判定：命中即跳过写入。
    """
    if not session_id or not isinstance(session_id, str):
        return False
    s = session_id.strip().lower()
    return s.startswith("sess_subagent_") or "subagent" in s


def _state_path(data_dir):
    return os.path.join(data_dir, STATUS_STATE_NAME)


def _load_state(path):
    """读现有状态文件 -> dict 分片表 {sid: record}。

    v1（全局单条 {"event","ts","session_id"}）就地升级：把那条记录放回它自己的
    sid 槽位，保证升级过程中老记录不丢（读到 v1 时新写入不会抹掉别的会话——
    v1 本来就只有一条）。坏 JSON / 结构不符 -> 视为空表重新开始。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    sessions = obj.get("sessions")
    if isinstance(sessions, dict):
        return dict((k, v) for k, v in sessions.items()
                    if isinstance(v, dict) and v.get("event"))
    if obj.get("event"):                      # v1 兼容升级
        sid = obj.get("session_id") or ""
        return {sid: {"event": obj.get("event"), "ts": obj.get("ts"),
                      "hook": obj.get("hook"),
                      "turn_started_at": obj.get("turn_started_at"),
                      "session_id": obj.get("session_id")}}
    return {}


def _prune(slots, order):
    """按 order（尾部最新）裁剪到 MAX_SESSION_SLOTS，返回 (slots, order)。"""
    order = [s for s in order if s in slots]
    for s in slots:
        if s not in order:
            order.append(s)
    drop = len(order) - MAX_SESSION_SLOTS
    if drop > 0:
        for s in order[:drop]:
            slots.pop(s, None)
        order = order[drop:]
    return slots, order


def _atomic_dump(payload, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _acquire_lock(data_dir):
    """跨进程排他小锁（O_CREAT|O_EXCL 自旋）。返回锁路径或 None（超时/失败）。

    钩子是内联阻塞执行的，但**不同会话/不同窗口**的钩子进程仍可能并发；
    分片写是「读-改-写」，无锁会丢掉另一会话刚写的槽位——那正是分片要解决的
    问题。拿不到锁时退化为「只写自己那条 + 不合并」（宁可少一条记录，绝不
    整体覆盖别人的记录）。
    """
    lock = os.path.join(data_dir, LOCK_NAME)
    deadline = time.time() + LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:                            # 持锁进程已死 -> 抢占陈旧锁
                if time.time() - os.path.getmtime(lock) > STALE_LOCK_S:
                    os.remove(lock)
                    continue
            except Exception:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(LOCK_POLL_S)
            continue
        except Exception:
            return None
        try:
            os.write(fd, str(int(time.time() * 1000)).encode("ascii"))
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        return lock


def _release_lock(lock):
    if not lock:
        return
    try:
        os.remove(lock)
    except Exception:
        pass


def write_status_event(event, session_id, data_dir, hook=None):
    """校验 event 并按会话分片原子写入 status-state.json。返回 (ok, error)。

    子代理会话（sess_subagent_*）不写事件，主会话状态条不被子代理活动代表。
    hook == 'UserPromptSubmit' 时同时置位 turn_started_at（本轮起点）；其余
    事件沿用该会话已有的 turn_started_at，不覆盖。
    """
    if not event or not isinstance(event, str):
        return False, "empty event"
    event = event.strip().lower()
    if event not in VALID_EVENTS:
        return False, "unknown event: %s" % event
    if is_subagent_session(session_id):
        return False, "subagent session ignored"
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = _state_path(data_dir)
        now = int(time.time() * 1000)
        sid = session_id if (isinstance(session_id, str) and session_id) else ""
        is_turn_start = (hook == "UserPromptSubmit")

        lock = _acquire_lock(data_dir)
        try:
            slots = _load_state(path)
            prev = slots.get(sid) or {}
            turn_started_at = now if is_turn_start else (
                prev.get("turn_started_at") or (now if event == "generating" else None))
            record = {
                "event": event,
                "ts": now,
                "hook": hook,
                "turn_started_at": turn_started_at,
                "session_id": session_id if sid else None,
            }
            if lock is None:
                # 等锁超时：只带自己这条落盘，不合并他槽（丢别人的记录比
                # 丢自己的更糟——那会退回 v1 的互踩症状）
                slots = {sid: record}
                order = [sid]
            else:
                slots[sid] = record
                order = [s for s in slots if s != sid] + [sid]
            slots, order = _prune(slots, order)
            _atomic_dump({"version": STATE_VERSION, "sessions": slots,
                          "order": order}, path)
        finally:
            _release_lock(lock)
        return True, None
    except Exception as e:
        return False, str(e)


def main(argv=None):
    ap = argparse.ArgumentParser(description="record ZCode hook status event")
    ap.add_argument("event", help="generating | tool | idle")
    ap.add_argument("session_id", nargs="?", default=None,
                    help="current session id injected by ZCode (fallback)")
    ap.add_argument("hook_name", nargs="?", default=None,
                    help="platform hook name (fallback; stdin wins)")
    ap.add_argument("--data-dir", default=None,
                    help="override data dir (default plugin data dir)")
    args = ap.parse_args(argv)

    data_dir = args.data_dir or DATA_DIR_DEFAULT
    # 注：ZCODE_PLUGIN_DATA 不参与解析——钩子环境下该变量指向另一套空目录
    # （data/zcode-token-stats@local/），曾导致与钩子读写分叉。

    # 优先级：stdin hook JSON（官方输入机制，字段更全）-> argv -> None
    stdin_sid, stdin_hook = read_hook_input_from_stdin()
    session_id = stdin_sid or args.session_id
    hook_name = stdin_hook or args.hook_name
    _ok, _err = write_status_event(args.event, session_id, data_dir,
                                   hook=hook_name)

    # 无论成败，stdout 恒为合法空 JSON {}（ZCode hook 运行器校验 stdout 的 JSON）
    try:
        sys.stdout.write("{}\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
