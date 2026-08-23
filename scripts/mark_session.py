#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mark_session.py — 把「当前真实会话 ID」原子写入数据目录 current-session.json。

由 ZCode hook（SessionStart / UserPromptSubmit）调用，ZCode 会把 hook 模板变量
${ZCODE_SESSION_ID} 展开为当前会话的真实 ID 并作为第一个参数传入。状态条
（docked_statusbar.py）以此标记判断「当前是哪个对话」，从而显示该对话自己的
累计统计，避免 jsonl 跨会话串数据。

写出的文件内容：{"session_id":"<id>","updated_at":<epoch_ms>}，UTF-8。
写入方式：先写临时文件再 os.replace 原子替换，避免并发读到半截内容。

失败容错（hook 绝不失败，与 inject_context.py / record_usage.py 同约定）：
  - 任何异常均吞掉，stdout 恒输出合法空 JSON "{}" 且 exit 0，
    绝不使 SessionStart / UserPromptSubmit 钩子失败或阻塞会话。

调用：python mark_session.py [<session_id>] [--data-dir <path>]
  - 数据目录优先级：--data-dir > 默认插件数据目录（固定，钩子与 GUI 同目录）。
    ZCODE_PLUGIN_DATA 不再参与解析（该变量在钩子环境指向另一套空目录）。
  - session_id 获取优先级（stdin 优先，官方钩子输入机制）：
      1) stdin 传入的 hook 输入 JSON 的 "session_id" 公共字段；
      2) argv[1]（hooks.json 里 "${ZCODE_SESSION_ID}" 模板展开的兜底参数）；
      3) 都为空 → 仅输出 {} 退出（不写文件），保证手跑安全。
    stdin 不可读 / 非 JSON / 无 session_id 字段时静默回退 argv，绝不抛错。
"""
import argparse
import json
import os
import sys
import time

# 插件根目录：本脚本实际所在目录的上一级（scripts/ -> 0.1.0/）
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
MARK_FILE_NAME = "current-session.json"


def is_subagent_session(session_id):
    """session_id 是否属于 subagent 会话（ZCode 给子代理开的独立会话）。

    UserPromptSubmit / SessionStart 也会在子代理会话里触发，若照单全收会把
    current-session.json 标记覆盖成子代理会话，导致状态条显示成别的对话。
    这里统一过滤：形如 ``sess_subagent_*`` 或含 "subagent" 的一律不写标记。
    """
    if not session_id or not isinstance(session_id, str):
        return False
    s = session_id.strip().lower()
    return s.startswith("sess_subagent_") or "subagent" in s


def read_session_id_from_stdin():
    """尝试从 stdin 的 hook 输入 JSON 里取 session_id（官方钩子输入机制）。

    ZCode 钩子运行器把 hook 输入 JSON（公共字段含 session_id）经 stdin 传入。
    容错：stdin 不可读 / 是交互终端 / 为空 / 非 JSON / 无 session_id 字段 /
    读取抛任何异常 → 一律返回 None（调用方回退 argv），绝不抛错、不阻塞。
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


def mark_session(session_id, data_dir):
    """校验 session_id 并原子写入 current-session.json。返回 (ok, error)。

    子代理会话（sess_subagent_*）不会写入标记，防止主会话标记被覆盖。
    """
    if not session_id or not isinstance(session_id, str):
        return False, "empty session id"
    if is_subagent_session(session_id):
        return False, "subagent session ignored"
    try:
        os.makedirs(data_dir, exist_ok=True)
        payload = {
            "session_id": session_id,
            "updated_at": int(time.time() * 1000),
        }
        tmp = os.path.join(data_dir, MARK_FILE_NAME + ".tmp")
        target = os.path.join(data_dir, MARK_FILE_NAME)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, target)
        return True, None
    except Exception as e:
        return False, str(e)


def main(argv=None):
    ap = argparse.ArgumentParser(description="mark current ZCode session id")
    ap.add_argument("session_id", nargs="?", default=None,
                    help="current real session id injected by ZCode")
    ap.add_argument("--data-dir", default=None,
                    help="override data dir (default plugin data dir)")
    args = ap.parse_args(argv)

    data_dir = args.data_dir or DATA_DIR_DEFAULT
    # 注：ZCODE_PLUGIN_DATA 不参与解析——钩子环境下该变量指向另一套空目录
    # （data/zcode-token-stats@local/），曾导致标记写错目录、状态条读不到。

    # session_id 优先级：stdin hook JSON 的 session_id → argv[1] → 空（跳过）
    session_id = read_session_id_from_stdin() or args.session_id
    _ok, _err = mark_session(session_id, data_dir)

    # 无论成败，stdout 恒为合法空 JSON {}（ZCode hook 运行器校验 stdout 的 JSON）
    try:
        sys.stdout.write("{}\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
