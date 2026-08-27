#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
status_event.py — 把 ZCode 钩子时序事件原子写入数据目录 status-state.json。

由 ZCode hook（UserPromptSubmit / PostToolUse / Stop）调用，记录当前「会话
活动阶段」：
  - UserPromptSubmit -> generating（用户发消息，模型开始生成）
  - PostToolUse      -> tool（模型调用工具）
  - Stop             -> idle（一轮结束，回到空闲）

状态条（docked_statusbar.py 0.6.0）以此文件推断状态徽标：最近事件 =
generating 且距今 < STATUS_IDLE_AFTER_MS（60s）->「生成中」；= tool 且
< 60s ->「工具中」；否则「空闲」。不依赖 live_stream / 代理。

写出的文件内容：{"event":"generating|tool|idle","ts":<epoch_ms>,"session_id":<sid|null>}，
UTF-8。写入方式：先写临时文件再 os.replace 原子替换，避免并发读到半截内容。

失败容错（hook 绝不失败，与 mark_session.py / inject_context.py / record_usage.py
同约定）：任何异常均吞掉，stdout 恒输出合法空 JSON "{}" 且 exit 0，绝不使
钩子失败或阻塞会话。

调用：python status_event.py <event> [<session_id>] [--data-dir <path>]
  - event 必填，取值 generating / tool / idle（其他取值仅记录不校验）。
  - 数据目录优先级：--data-dir > 默认插件数据目录（固定，钩子与 GUI 同目录）。
  - session_id 获取优先级（stdin 优先，官方钩子输入机制）：
      1) stdin 传入的 hook 输入 JSON 的 "session_id" 公共字段；
      2) argv[2]（hooks.json 里 "${CLAUDE_SESSION_ID}" 模板展开的兜底参数）；
      3) 都为空 -> 记为 null（事件本身仍记录）。
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

VALID_EVENTS = ("generating", "tool", "idle")


def read_session_id_from_stdin():
    """尝试从 stdin 的 hook 输入 JSON 里取 session_id（官方钩子输入机制）。

    容错：stdin 不可读 / 是交互终端 / 为空 / 非 JSON / 无 session_id 字段 /
    读取抛任何异常 -> 一律返回 None（调用方回退 argv），绝不抛错、不阻塞。
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.lstrip("\ufeff"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return None


def is_subagent_session(session_id):
    """session_id 是否属于 subagent 会话（ZCode 给子代理开的独立会话）。

    子代理会被 ZCode 以独立 session_id 运行（形如 ``sess_subagent_*``），其
    UserPromptSubmit / PostToolUse / Stop 同样会触发本钩子；而 status-state.json
    是全局单份事件文件，照单全收会把主会话的活动阶段覆盖成子代理的。这里与
    mark_session.py / docked_statusbar.py 同款判定：命中即跳过写入。
    """
    if not session_id or not isinstance(session_id, str):
        return False
    s = session_id.strip().lower()
    return s.startswith("sess_subagent_") or "subagent" in s


def write_status_event(event, session_id, data_dir):
    """校验 event 并原子写入 status-state.json。返回 (ok, error)。

    子代理会话（sess_subagent_*）不会写入事件，防止主会话状态被覆盖。
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
        payload = {
            "event": event,
            "ts": int(time.time() * 1000),
            "session_id": session_id if (isinstance(session_id, str) and session_id) else None,
        }
        tmp = os.path.join(data_dir, STATUS_STATE_NAME + ".tmp")
        target = os.path.join(data_dir, STATUS_STATE_NAME)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, target)
        return True, None
    except Exception as e:
        return False, str(e)


def main(argv=None):
    ap = argparse.ArgumentParser(description="record ZCode hook status event")
    ap.add_argument("event", help="generating | tool | idle")
    ap.add_argument("session_id", nargs="?", default=None,
                    help="current session id injected by ZCode (fallback)")
    ap.add_argument("--data-dir", default=None,
                    help="override data dir (default plugin data dir)")
    args = ap.parse_args(argv)

    data_dir = args.data_dir or DATA_DIR_DEFAULT
    # 注：ZCODE_PLUGIN_DATA 不参与解析——钩子环境下该变量指向另一套空目录
    # （data/zcode-token-stats@local/），曾导致与钩子读写分叉。

    # session_id 优先级：stdin hook JSON 的 session_id -> argv[2] -> None
    session_id = read_session_id_from_stdin() or args.session_id
    _ok, _err = write_status_event(args.event, session_id, data_dir)

    # 无论成败，stdout 恒为合法空 JSON {}（ZCode hook 运行器校验 stdout 的 JSON）
    try:
        sys.stdout.write("{}\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
