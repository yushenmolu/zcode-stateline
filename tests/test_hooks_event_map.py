# -*- coding: utf-8 -*-
"""hooks/hooks.json 静态契约：7 个平台事件的状态映射一个都不能少。

背景：状态徽标完全由钩子时序（status-event.cmd 写 status-state.json）
驱动。若某平台事件在 hooks.json 中漏注册（如 PostToolUseFailure 缺失），
对应状态切换会静默失效，读端只能在超窗后回落 idle——静态契约测试在
安装/发布前就把映射缺口拦下来。
"""
import json
import os
import unittest

_HOOKS_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hooks", "hooks.json")

# 7 个平台事件（少一个即失败）
ALL_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
)


def _load_doc():
    with open(_HOOKS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _event_commands(doc, event):
    """取某事件下全部 command 命令行（拼接所有 matcher 组的所有 hook）。"""
    commands = []
    for group in doc.get("hooks", {}).get(event, []):
        for hook in group.get("hooks", []):
            if hook.get("type") == "command":
                commands.append(hook.get("command", ""))
    return commands


def _status_event_commands(doc, event):
    """取某事件下所有走 status-event.cmd 的命令行。"""
    return [c for c in _event_commands(doc, event)
            if "status-event.cmd" in c]


class TestHooksEventMap(unittest.TestCase):
    """hooks.json 事件注册与状态映射的静态契约。"""

    def test_all_seven_events_registered(self):
        """7 个平台事件全部注册，且每个至少挂一条 command hook。"""
        doc = _load_doc()
        for event in ALL_HOOK_EVENTS:
            self.assertIn(event, doc.get("hooks", {}),
                          "hooks.json 缺少事件注册: %s" % event)
            self.assertTrue(_event_commands(doc, event),
                            "事件 %s 注册了但没有任何 command hook" % event)

    def test_session_start_does_not_write_status(self):
        """SessionStart 只写会话标记（mark-session 等），不写状态事件。"""
        doc = _load_doc()
        self.assertEqual(_status_event_commands(doc, "SessionStart"), [])

    def test_pretooluse_and_permissionrequest_map_to_tool(self):
        """PreToolUse / PermissionRequest -> status-event.cmd tool。"""
        doc = _load_doc()
        for event in ("PreToolUse", "PermissionRequest"):
            cmds = _status_event_commands(doc, event)
            self.assertTrue(cmds, "事件 %s 缺少 status-event.cmd 动作" % event)
            for cmd in cmds:
                self.assertIn(" tool ", cmd,
                              "事件 %s 的动作应写 tool 状态: %r" % (event, cmd))

    def test_generating_events_map_to_generating(self):
        """UserPromptSubmit / PostToolUse / PostToolUseFailure -> generating。"""
        doc = _load_doc()
        for event in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure"):
            cmds = _status_event_commands(doc, event)
            self.assertTrue(cmds, "事件 %s 缺少 status-event.cmd 动作" % event)
            for cmd in cmds:
                self.assertIn(" generating ", cmd,
                              "事件 %s 的动作应写 generating 状态: %r"
                              % (event, cmd))

    def test_stop_maps_to_idle(self):
        """Stop -> status-event.cmd idle。"""
        doc = _load_doc()
        cmds = _status_event_commands(doc, "Stop")
        self.assertTrue(cmds, "Stop 缺少 status-event.cmd 动作")
        for cmd in cmds:
            self.assertIn(" idle ", cmd,
                          "Stop 的动作应写 idle 状态: %r" % cmd)

    def test_platform_hook_name_passed_as_third_arg(self):
        """0.9.0：每条 status-event.cmd 必须把平台钩子名作为第 3 参数传下去。

        读端要靠 `hook` 字段区分「本轮开始的 UserPromptSubmit」与「工具回落到
        PostToolUse」——两者的 event 值同为 generating。缺第 3 参数时 turn 收尾
        例外判定失效，新一轮刚开始就会被上一轮的收尾压成空闲。
        """
        doc = _load_doc()
        for event in ALL_HOOK_EVENTS:
            for cmd in _status_event_commands(doc, event):
                self.assertIn('"%s"' % event, cmd,
                              "事件 %s 的 status-event.cmd 未传平台钩子名: %r"
                              % (event, cmd))


if __name__ == "__main__":
    unittest.main()
