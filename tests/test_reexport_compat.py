# -*- coding: utf-8 -*-
"""re-export 兼容性回归：docked_statusbar 抽离模块后，旧名 dsb.<name> 全部仍可访问。

背景：statusbar_layout / statusbar_db / statusbar_status 相继从
docked_statusbar 抽离，主文件靠 `from <mod> import *` 保留旧名。
`import *` 默认不带下划线私有名，所以各模块必须在 `__all__` 里显式列全；
本文件把「对外契约名清单」固化成测试，任一名字从 re-export 链上掉落即红。

结构：每个抽离模块一个 NAMES_* 列表 + 通用断言函数；后续 Stage 继续抽离时
直接在对应列表追加（或新增 NAMES_* 列表 + 一个 test 方法）即可。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


# Round2 Step 1：statusbar_layout（几何/布局纯函数与小常量）
NAMES_LAYOUT = [
    "WINDOW_H", "MARGIN", "TIP_EDGE_GAP", "HANDLE_H", "HANDLE_W_DEFAULT",
    "HANDLE_FALLBACK_MARGIN",
    "tooltip_placement", "default_handle_xy", "valid_and_clamped_handle_xy",
    "hidden_skip_refresh", "collapsed_poll_decision", "expanded_poll_decision",
    "hover_expand_should_schedule", "drag_target_xy", "drag_grab_offset",
]

# Round2 Step 2：statusbar_db（db.sqlite 只读查询层）
NAMES_DB = [
    "BACKGROUND_QUERY_SOURCES", "INTERACTIVE_SOURCE_SQL", "ACTIVITY_GRACE_MS",
    "TOOL_LIVE_MAX_MS", "DB_ACTIVE_WINDOW_MS",
    "COLD_READ_RATIO", "COLD_READ_COUNT_SQL",
    "_num", "time_ms", "_is_subagent_sid",
    "_db_connect",
    "db_recent_session_activity", "db_latest_session_id", "db_latest_model_id",
    "db_session_title", "db_aggregate_session", "db_session_speed",
    "recent_turn_stats", "turn_window_left_edge", "live_turn_stats",
    "db_turn_request_profile", "db_session_model_activity_ts",
    "db_tool_activity", "db_recent_tool_activity", "tool_live_ms",
    "db_latest_speed", "db_today_stats", "db_latest_model_input",
    "today_start_ms",
]

# Round2 Step 3：statusbar_status（状态机与状态徽标段）
NAMES_STATUS = [
    # 状态常量（含时间窗）
    "STATUS_STATE_NAME", "STATUS_IDLE_AFTER_MS", "STATUS_IDLE_AFTER_MS_DEFAULT",
    "GENERATING_MAX_LIFETIME_MS", "BUSY_FAMILY", "STATUS_DWELL_MS",
    "TURN_END_TIE_MS", "OUTCOME_HOLD_MS", "SPEED_HOLD_MS",
    # 徽标文案 / 颜色 / tooltip 常量
    "STATUS_TEXT", "STATUS_COLORS", "STATUS_TIPS", "BADGE_TOOL_NAME_MAX",
    # 状态文件读取与分片视图（下划线私有名也必须 re-export）
    "_read_status_state", "_status_slots",
    "status_state_for", "status_state_latest", "status_turn_started_at",
    # 状态判定 / 去抖 / 速度
    "status_detector", "status_debounce", "speed_hold", "speed_display",
    # 徽标文案 / tooltip 与统一解析
    "format_elapsed_ms", "status_badge_text", "status_badge_tip",
    "resolve_turn_status",
]

# Round2 Step 4：statusbar_session（会话判定层）
NAMES_SESSION = [
    # 判定常量
    "MARK_FILE_NAME", "LOG_DIR_DEFAULT",
    "MARK_FRESH_MS", "STATUS_SIGNAL_FRESH_MS", "RESUME_FRESH_MS",
    # mark / 日志解析 / resume tail（下划线私有名也必须 re-export）
    "read_mark_raw", "_log_line_ts_ms", "tail_session_resume",
    # jsonl 兜底判定与信号竞争 + 粘滞
    "current_session", "resolve_session_sticky",
]

# Round2 Step 4：statusbar_config（配置层）
NAMES_CONFIG = [
    # 常量
    "DEFAULT_CONFIG", "CONFIG_FILE_NAME", "DATA_DIR_DEFAULT", "SHOW_MENU_ITEMS",
    "DEFAULT_CONTEXT_WINDOW",
    # 错误日志（配置层与主文件共用，下划线私有名也必须 re-export）
    "_log_err",
    # 配置读写
    "load_config", "_apply_raw_config", "hot_reload_config",
    "save_config_keys", "save_config_show_keys",
]

# Round2 Step 5：statusbar_gui（GUI 应用层：原 run_gui 闭包提为 StatusBarApp）
NAMES_GUI = [
    "StatusBarApp",
    "_run_gui_impl",
]

# statusbar_gui 只能经 deps 桥接访问的主文件助手名（R3-001 回归）：
# 抽类时这些名字曾被裸用（漏 deps. 前缀），运行即 NameError——但单测全绿，
# 因为 re-export 测试不覆盖 gui 模块内部裸名。此处固化为 AST 级契约：
# statusbar_gui.py 源码中禁止出现这些名字的裸 Load（必须 deps.<name>）。
GUI_DEPS_ONLY_NAMES = [
    "clamp_to_work_area", "work_area_of_rect",
    "_cleanup_pid", "_hit_rate", "_truncate", "format_tokens",
    "round_rect", "plan_statusbar_layout_3zone",
]


def _assert_all_present(testcase, names, module_label):
    """逐个断言 dsb.<name> 存在；缺失时一次性列出全部缺名（不第一个就停）。"""
    missing = [n for n in names if not hasattr(dsb, n)]
    testcase.assertEqual(
        [], missing,
        "%s 有 %d 个旧名未从 docked_statusbar re-export：%s"
        % (module_label, len(missing), missing))


class TestReexportCompat(unittest.TestCase):
    """抽离模块的旧名 re-export 契约（逐模块一个用例，便于定位是哪一层掉了）。"""

    def test_layout_names_reexported(self):
        _assert_all_present(self, NAMES_LAYOUT, "statusbar_layout")

    def test_db_names_reexported(self):
        _assert_all_present(self, NAMES_DB, "statusbar_db")

    def test_status_names_reexported(self):
        _assert_all_present(self, NAMES_STATUS, "statusbar_status")

    def test_session_names_reexported(self):
        _assert_all_present(self, NAMES_SESSION, "statusbar_session")

    def test_config_names_reexported(self):
        _assert_all_present(self, NAMES_CONFIG, "statusbar_config")

    def test_gui_names_reexported(self):
        _assert_all_present(self, NAMES_GUI, "statusbar_gui")

    def test_private_underscore_names_reexported(self):
        """下划线私有名是 import * 的盲区，单列一组盯住（P1-001 教训）。"""
        private = [n for n in (NAMES_LAYOUT + NAMES_DB + NAMES_STATUS
                               + NAMES_SESSION + NAMES_CONFIG + NAMES_GUI)
                   if n.startswith("_")]
        # 至少要有我们已知的私有名；清单里若一个都没有，说明清单本身漏了
        self.assertTrue(private, "清单里应至少包含一个下划线私有名")
        _assert_all_present(self, private, "private names")

    def test_module_all_lists_match_expectation(self):
        """各抽离模块的 __all__ 无多余、无缺失（与对应 NAMES_* 双向对齐）。"""
        import statusbar_status
        import statusbar_session
        import statusbar_config
        import statusbar_gui
        for mod, expected, label in (
                (statusbar_status, NAMES_STATUS, "statusbar_status"),
                (statusbar_session, NAMES_SESSION, "statusbar_session"),
                (statusbar_config, NAMES_CONFIG, "statusbar_config"),
                (statusbar_gui, NAMES_GUI, "statusbar_gui")):
            declared = set(mod.__all__)
            want = set(expected)
            self.assertEqual(declared - want, set(),
                             "%s.__all__ 里多出了清单外的名字" % label)
            self.assertEqual(want - declared, set(),
                             "%s.__all__ 漏掉了清单里的名字" % label)

    def test_gui_uses_deps_prefix_for_mainfile_helpers(self):
        """statusbar_gui 对主文件助手名必须 deps. 前缀（R3-001：裸名运行即
        NameError，bar 永远 hidden at (0,0)，但单测全绿——AST 级补盲）。"""
        import ast
        gui_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "scripts", "statusbar_gui.py")
        with open(gui_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bare = sorted({n.id for n in ast.walk(tree)
                       if isinstance(n, ast.Name)
                       and isinstance(n.ctx, ast.Load)
                       and n.id in GUI_DEPS_ONLY_NAMES})
        self.assertEqual([], bare,
                         "statusbar_gui 裸用了主文件助手名（应 deps. 前缀）：%s"
                         % bare)
        # 反向：deps.<name> 访问的名字必须真在 docked_statusbar 上存在
        accessed = sorted({n.attr for n in ast.walk(tree)
                           if isinstance(n, ast.Attribute)
                           and isinstance(n.value, ast.Name)
                           and n.value.id == "deps"})
        missing = [a for a in accessed if not hasattr(dsb, a)]
        self.assertEqual([], missing,
                         "statusbar_gui 经 deps 访问了主文件不存在的名字：%s"
                         % missing)


if __name__ == "__main__":
    unittest.main()
