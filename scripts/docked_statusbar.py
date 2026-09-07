#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
docked_statusbar.py — ZCode token-stats「智能贴边底部状态条」（对话级三区：状态徽标+对话名 / 本轮统计 / 会话累计）。

作用：ZCode 没有官方 UI 槽位，本脚本用一个无边框置顶的 tkinter 小条
「智能贴边」到 ZCode 主窗口底部外沿（水平居中）。仅当 ZCode 在前台且
未最小化时显示；每 ~1 秒刷新为对话级状态显示：
  - 第一行：状态徽标（空闲=灰「空闲」/ 生成中=绿「⚡生成中」带实时 tok/s /
    工具中=蓝「🔧工具中」/ 出错=红「出错」）+ 对话名；
  - 第二行：本轮统计——当前会话最近一轮的 `⏱<秒> · in <k/M> · out <k/M> ·
    cache hit <%>`（每轮结束更新，非会话累计）；
  - 右侧小字：会话累计（`in <k/M> · out <k/M>` 小号次要）。

「当前会话」判定优先级（fail-closed：判定不充分显示占位，绝不猜；
data_dir/current-session.json 由 scripts/mark_session.py 在
SessionStart/UserPromptSubmit 时写入真实会话 ID）：
  0. 实时流 active 且带 sessionId（0.4.2 新增最高优先级：正在发请求的会话
     就是当前对话；proxy_server.py 写 live_stream.json）-> 用该 session_id；
  1. current-session.json 存在且 updated_at 距今 < 30 秒 -> 用其 session_id；
  2. 否则 db 兜底（限 60 秒窗口）：最近 60 秒内有模型调用的最新主会话
     （model_usage started_at DESC 第一条、非 subagent）；
  3. 否则 token-stats.jsonl ts 最大主会话记录——行尾标注「（最近会话累计）」；
  4. 都没有 -> 「（会话未识别，待首轮活动）」占位。
切换对话（未发消息）时无事件源，状态条在下一个流开始（或 mark/db 窗口
到期）前沿用旧会话——此为无信号可判的固有边界；发消息后由 0 立即跟随。

0.4.2 变更（fix-20260824）：
  - 会话跟随：live_stream active 且 sessionId 存在时以其为当前会话（最高
    优先级），切换对话发消息后状态条立即跟随新会话；
  - 生成中防误报：实时流 active 但其 sessionId 与当前会话不一致（旧会话
    残留流）时不显示生成中；LIVE_STALE_MS 60s -> 10s，流停后 10 秒内消退；
  - tok/s 弱化：速度从徽标内（粗体主显示）降为徽标旁小号灰字，仅生成中
    显示，不占主显示；--once 输出结构不变。

0.5.0 变更（fix-20260826）：
  - 子代理流不再抑制：0.4.2 的会话匹配闸（live_sid != session_id ->
    不显示生成中）把子代理的实时流也压掉，用户盯子代理任务永远 idle。
    现改为**不按会话抑制生成中**——只按 live active + 新鲜性判定，会话
    跟随仍由 resolve_current_session 管，二者解耦（取舍：切换对话后旧流
    残留会在新鲜窗口 45s 内显示生成中，换来的代价是子代理流可实时显示）；
  - LIVE_STALE_MS 10s -> 45s：模型首 token 延迟实测 20-33s，10s 窗口在
    首 token 前即判过期掉回 idle；45s 以 last_event_at 距 now < 45s 判定
    新鲜，首 token 前也持续显示「生成中」；
  - speed 为 None（首 token 前 / 计时不足）时本轮统计尾部显示「生成中…」
    占位，不再静默无速度块；
  - 配置显式化：load 时若 statusbar-config.json 缺失
    show_live/show_status/show_recent_turn/show_cumulative/show_speed 等
    键，自动补齐默认值写回文件（不覆盖用户已有值）；

0.6.0 变更（fix-20260826）：
  - 状态徽标改用 ZCode 钩子时序推断（status-state.json，status_event.py
    原子写）：UserPromptSubmit -> generating（发消息即「⚡生成中」）；
    PostToolUse -> tool（「🔧工具中」）；Stop -> idle。**不再读 live_stream、
    不再依赖代理**（live_stream 即使存在也不读取）；
  - tok/s 改用数据库精确值：最近一条 completed 非 subagent model_usage 的
    output_tokens/(duration_ms/1000)（db_latest_speed），非流式字符≈token
    近似；生成中时显示该精确速度（或最近一次速度）；
  - STATUS_IDLE_AFTER_MS=60s：最近事件距 now 超 60s 一律回落「空闲」；
  - show_live 语义保留（控制实时徽标显示；默认 true）；
  - ensure-proxy 不再由 SessionStart 钩子默认拉起（代理降级为可选）。

数据源（本轮改进）：
  - **主数据源 = db.sqlite（model_usage 行级）**：模型调用完成即落库，比
    jsonl（要等 Stop 钩子 record_usage.py 聚合写盘）早一轮，延迟更低。
    按当前会话聚合 status='completed' 且非 subagent 的行（规避 subagent
    会话 id 与 query_source='subagent'，双保险），avgDuration = 行级平均耗时。
  - jsonl（token-stats.jsonl）仅作兜底：db 读不到 / db 聚合无 completed 行 /
    db 打不开时才回退，避免冷启动显示「—」。
  - db 全程只读：`sqlite3.connect("file:...?mode=ro", uri=True)` +
    `PRAGMA query_only=ON`，绝不写库。

设计要点：
  - 纯标准库：tkinter（GUI）+ ctypes（Win32 窗口跟踪/贴边）+ sqlite3（只读）。
  - 64 位句柄安全：所有 Win32 函数设 argtypes，hwnd 一律 c_void_p；
    SetWindowPos 的 HWND_TOPMOST 必须传 ctypes.c_void_p(-1)。
  - 绝不抢焦点：只用 ShowWindow(SW_SHOWNOACTIVATE) / MoveWindow /
    SetWindowPos(SWP_NOACTIVATE)，从不调用 SetForegroundWindow；
    任何异常只隐藏或保持现状，绝不弹错。
  - 前台判定：GetForegroundWindow() 的 pid 与 ZCode 主窗口 pid 一致，
    再用 QueryFullProcessImageNameW 比对 exe 名（双保险）。
    FindWindowW 用精确标题 "ZCode"（非子串），避免含 zcode 的窗口误命中。
  - 拖动：按住状态条任意区域（Canvas 全区域，含两行文字与各指标块；
    右上 close 小块除外）左键拖动，按下记录 (event.x_root - winfo_rootx,
    event.y_root - winfo_rooty)，移动时 geometry 跟随；释放后置
    manual_position=True，poll 的贴边分支若 manual_position 为真则**跳过
    MoveWindow 吸回**（前台显隐判定照常）。右键菜单「重新贴边」清除
    manual_position，恢复智能自动贴边。拖动中（dragging=True）poll 也不吸
    回，避免拖到一半被抢。
  - 悬停提示（tooltip）：鼠标悬停在第二行各指标块（或第一行模型/会话）上
    ~400ms 后显示一个无边框置顶小气泡，跟随鼠标；移开即隐藏。气泡复用
    单一全局 toplevel，只改文本，避免反复建窗口。指标块悬停同时高亮块
    背景（#1b1e24 -> #262b33）。
  - UI 主题（对话级三区布局，暗色，AA/WCAG 对比度）：
      窗口宽自适应内容（基准 620，见 plan_statusbar_layout_3zone），底色
      #14161a + 顶部 1px 分隔线 #2a2f38。
      第一行（~20px）：状态徽标（圆角色块 + 深色粗体字；空闲灰 / 生成中绿
      ⚡ / 工具中蓝 🔧 / 出错红）+ 速度小字（0.4.2 起 tok/s 为徽标旁小号灰字，
      仅生成中显示，不再占徽标主显示）+ 会话
      标题（灰 #9aa1aa），右端「×」close 小块（hover 红 #e5534b + 白字）。
      第二行（~30px）：本轮统计——当前会话最近一轮 `◷<秒> · in <k/M> ·
      out <k/M> · cache hit <%>`（数字 Consolas 等宽防跳字；每轮结束更新，
      非会话累计；无已完成轮次显示「（本轮统计待更新）」）。
      右侧小字（第三区）：会话累计 `in <k/M> · out <k/M>`（灰小号次要，
      jsonl 兜底判定时尾部附「（最近会话累计）」标注）。
      三区独立开关（show_status / show_recent_turn / show_cumulative），
      关掉的区不画、不留空位；文本画在单一 Canvas 上，按 tag 分组绑定
      悬停 tooltip 与拖动。
  - 显示项可配置：data_dir/statusbar-config.json（缺省自动生成默认配置）。
    show_status / show_recent_turn / show_cumulative 决定三区（状态徽标 /
    本轮统计 / 会话累计）是否渲染；右键菜单「显示项」子菜单可直接勾选切换
    （切换即重画并原子写回配置，无需手改 JSON）；手改文件也会在下一拍热
    加载生效。
    refresh_ms 覆盖 --interval-ms 默认（数据刷新间隔，默认 1000ms）。
    坏 JSON / 缺字段一律回退默认值并写一条日志到 docked-statusbar-err.log。
    --config <path> 覆盖配置文件路径。
  - 速度（tok/s）：生成中实时流速度（累计字符 ÷ 流已用时，字符≈token 近似）
    以徽标旁小号灰字显示（0.4.2，次要不抢主显示；仅生成中）；常规态最近一次
    completed 请求的 output_tokens/(duration_ms/1000) 仅供 --once 输出
    （speedTokPerSec / speedAvgTokPerSec，可 null；jsonl 无行级耗时故速度仅
    db 可算）。
  - 靠边收起（collapsed）：双击状态条任意区域 / 右键「收起到边缘」-> 收成
    ~72x18 底部小把手（◐ 缓存命中率绿字，贴屏幕底缘、ZCode 底部中心 x
    附近）；把手悬停 ~0.5s（Move 刷新计时）或单击 -> 展开回完整状态条
    （重新贴边）。collapsed 字段持久化（save_config_keys 原子写），重启
    恢复原状态；收起态显隐同样只随 ZCode 前台，把手可拖动且钳制工作区。
  - 防多开：数据目录 statusbar.pid 记录本进程 pid；已有存活实例直接退出；
    退出时若 pid 仍是自己的则删除。
  - CLI：--once 读一次打印统计 JSON 后退出（不建窗口、不进 GUI）。
    输出结构：{ok, line, source, sessionId, model, sessionLabel, line1,
    speedTokPerSec, speedAvgTokPerSec, status, recentTurn, version}；
    line 按 show_* 配置裁剪（与旧第二行口径一致）；status 为状态徽标判定
    （generating/error/tool/idle）；recentTurn 为当前会话最近一轮
    turn_usage 统计对象（无数据为 null）。

调用：
  pythonw.exe docked_statusbar.py [--data-dir DIR] [--db-path PATH]
                                  [--interval-ms MS] [--once] [--config PATH]

验收说明：本脚本完成脚本级自测（py_compile / --once / 配置开关渲染 /
拖动绑定与 tooltip 的绑定逻辑自查）。「真实 GUI 拖动不吸回 / tooltip 悬停 /
视觉主题」需 ZCode 重启后由用户实测确认——本脚本不制造该结论。
"""
import argparse
import ctypes
import ctypes.wintypes as wintypes
import datetime
import json
import os
import sqlite3
import sys
import time
import traceback


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUSBAR_VERSION = "0.7.0"  # 自证版本：肉眼可确认状态条运行的是本版代码
DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
DB_DEFAULT = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
LOG_DIR_DEFAULT = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "log")
JSONL_NAME = "token-stats.jsonl"
PID_NAME = "statusbar.pid"
MARK_FILE_NAME = "current-session.json"
CONFIG_FILE_NAME = "statusbar-config.json"
LIVE_STREAM_NAME = "live_stream.json"   # 实时流计数共享文件（proxy_server.py 原子写）
                                        # （0.6.0 起状态徽标不再读它，仅代理侧自用）
STATUS_STATE_NAME = "status-state.json" # 钩子时序状态文件（status_event.py 原子写）
STATUS_IDLE_AFTER_MS = 60 * 1000        # 钩子状态新鲜窗口：距最近事件超此值回落「空闲」
LIVE_STALE_MS = 45 * 1000               # 实时流新鲜窗口：距 last_event_at 超此值视为过期/卡住
                                        # （0.5.0 10s->45s：模型首 token 延迟实测 20-33s，
                                        #  10s 窗口在首 token 前即判过期掉回 idle；45s 以
                                        #  last_event_at 判定，首 token 前也持续显示生成中）
LIVE_USAGE_FRESH_MS = 30 * 1000         # 流结束后精确 usage 的展示窗口（随后回 db 轮询）
MARK_FRESH_MS = 30 * 1000  # current-session.json 标记的 freshness 窗口（30 秒）
DB_ACTIVE_WINDOW_MS = 60 * 1000  # db 兜底判定窗口：最近 60 秒内有模型调用才算活跃
DB_READ_INTERVAL = 5        # 每 N 次刷新才重读一次 db（model/title 不频繁变化）
STATS_PENDING = u"\uff08\u672c\u8f6e\u7ed3\u675f\u540e\u66f4\u65b0\uff09"  # （本轮结束后更新）
TURN_PENDING = u"\uff08\u672c\u8f6e\u7edf\u8ba1\u5f85\u66f4\u65b0\uff09"  # （本轮统计待更新）——0.4.0 第二行无已完成轮次时占位
SESSION_UNKNOWN = u"\uff08\u4f1a\u8bdd\u672a\u8bc6\u522b\uff0c\u5f85\u9996\u8f6e\u6d3b\u52a8\uff09"  # （会话未识别，待首轮活动）
SESSION_RECENT_NOTE = u"\uff08\u6700\u8fd1\u4f1a\u8bdd\u7d2f\u8ba1\uff09"  # （最近会话累计）——jsonl 兜底判定时的标注

WINDOW_W = 620        # 状态条基准宽度（初始 geometry；实际宽度按内容自适应，
                      # 见 plan_statusbar_layout —— 指标块永不因宽度丢块）
WINDOW_H = 56         # 状态条高度（1px 顶线 + 信息行 ~20px + 指标行 ~30px）
MARGIN = 6            # 贴边留白
REFRESH_MS_DEFAULT = 1000  # 数据刷新 / 贴边/前台轮询（用户要求默认 1000ms）

# ---- 暗色主题（AA 对比度）----
BG = "#14161a"          # 窗口背景（比纯黑有层次）
BG_SECOND = "#1b1e24"   # 次要背景（tooltip 底 / close 小块底）
FG = "#e6e8eb"          # 主文字（对 BG 对比度 ~13:1，过 AA）
FG_DIM = "#9aa1aa"      # 次要文字 / 标签（~5.8:1，过 AA）
ACCENT_BLUE = "#4f9cf7" # 强调色（模型名 / 第一行小圆点）
ACCENT_GREEN = "#3fb68b"# 命中率 / 省钱（绿）
ACCENT_PURPLE = "#b08cf7"  # reasoning 标识紫
ACCENT_ORANGE = "#e8b33f"  # 速度 tok/s 标识橙黄
SEP_COLOR = "#3a4048"   # 分隔线 / `·` 灰
EDGE_LINE = "#2a2f38"   # 顶部 1px 分隔线（提质感）
CLOSE_HOVER_BG = "#e5534b"  # close 悬停红
CLOSE_HOVER_FG = "#ffffff"
MENU_BG = "#1b1e24"
MENU_ACTIVE = "#2f3a4a"
FONT_DIM = ("Microsoft YaHei UI", 9)      # 第一行（会话·模型）小字
FONT_MAIN = ("Microsoft YaHei UI", 10)    # 指标块中文标签
FONT_NUM = ("Consolas", 10, "bold")       # 指标块数字（等宽防跳字）
FONT_CLOSE = ("Microsoft YaHei UI", 9, "bold")

# ---- 状态徽标（0.4.0 对话级状态显示：第一行 状态徽标 + 对话名）----
STATUS_TEXT = {
    "idle": u"\u7a7a\u95f2",                        # 空闲
    "generating": u"\u26a1\u751f\u6210\u4e2d",      # ⚡生成中
    "tool": u"\U0001f527\u5de5\u5177\u4e2d",        # 🔧工具中
    "error": u"\u51fa\u9519",                       # 出错
}
STATUS_COLORS = {
    "idle": "#7d8590",          # 灰
    "generating": ACCENT_GREEN,  # 绿
    "tool": ACCENT_BLUE,        # 蓝
    "error": "#e5534b",         # 红
}
STATUS_TIPS = {
    "idle": u"\u7a7a\u95f2\uff1a\u5f53\u524d\u65e0\u6a21\u578b\u6d3b\u52a8",
    "generating": (u"\u751f\u6210\u4e2d\uff1a\u5b9e\u65f6\u6d41\u6b63\u5728\u8f93\u51fa\uff0c"
                   u"\u901f\u5ea6\u4e3a\u5b57\u7b26\u2248token \u8fd1\u4f3c"),
                   # 生成中：实时流正在输出，速度为字符≈token 近似
    "tool": (u"\u5de5\u5177\u4e2d\uff1a\u6700\u8fd1\u4e00\u6b21\u6a21\u578b\u8c03\u7528\u542b"
             u"\u5de5\u5177\u8c03\u7528\uff0c\u6216\u5f53\u524d\u8f6e\u672a\u5b8c\u6210"),
             # 工具中：最近一次模型调用含工具调用，或当前轮未完成
    "error": u"\u51fa\u9519\uff1a\u6700\u8fd1\u4e00\u6b21\u6a21\u578b\u8c03\u7528\u4ee5\u9519\u8bef\u7ed3\u675f",
}
BADGE_FONT = ("Microsoft YaHei UI", 9, "bold")  # 徽标文字（粗体）
BADGE_FG = "#0e1114"          # 徽标文字色（深色，对状态色过 AA）
BADGE_PAD_X = 9               # 徽标内左右留白
BADGE_H = 18                  # 徽标高
BADGE_GAP = 8                 # 徽标与标题间距（起步值；收缩档位见下）
BADGE_GAP_STEPS = (8, 6, 4)   # 徽标-标题间距收缩档位（超宽时其次于标题截短）

# ---- 本轮统计 / 会话累计（0.4.0 第二行 + 右侧小字）----
ROW2_TURN_Y = 40              # 第二行（本轮统计 / 累计小字）文字垂直中心
TIP_TURN = (u"\u672c\u8f6e\u7edf\u8ba1\uff1a\u5f53\u524d\u4f1a\u8bdd\u6700\u8fd1\u4e00\u8f6e"
            u"\u6a21\u578b\u8c03\u7528\u7684\u8017\u65f6 / \u8f93\u5165 / \u8f93\u51fa / "
            u"\u7f13\u5b58\u547d\u4e2d\u7387\uff08\u6bcf\u8f6e\u7ed3\u675f\u66f4\u65b0\uff0c"
            u"\u975e\u4f1a\u8bdd\u7d2f\u8ba1\uff09")
            # 本轮统计：当前会话最近一轮模型调用的耗时 / 输入 / 输出 / 缓存命中率（每轮结束更新，非会话累计）
TIP_CUM = u"\u4f1a\u8bdd\u7d2f\u8ba1\uff1a\u5f53\u524d\u5bf9\u8bdd\u5168\u90e8\u8f6e\u6b21\u7684\u7d2f\u8ba1\u8f93\u5165 / \u8f93\u51fa"

# ---- 彩色指标块（Canvas 绘制）----
BLOCK_BG = "#1b1e24"     # 指标块底色
BLOCK_HOVER = "#262b33"  # 指标块悬停高亮
BLOCK_GAP = 8            # 块间水平间距
BLOCK_H = 27             # 块高
BLOCK_PAD = 9            # 块内左右留白
ROW1_CY = 13             # 第一行（模型/会话）文字垂直中心
ROW2_Y = 26              # 第二行（指标块）顶部 y
BAR_W, BAR_H = 60, 4     # 命中率微型进度条尺寸
BAR_SLOT = "#2a2f38"     # 进度条槽色
BAR_FILL = "#3fb68b"     # 进度条填充色（命中率 <=70%）
BAR_FILL_HI = "#5dd6a8"  # 进度条填充色（命中率 >70%，更亮绿）

# ---- 自适应宽度（窗口宽按内容实测伸缩；指标块永不因宽度丢块）----
LABEL_MAX_STEPS = (16, 12, 10, 8, 6, 4)  # 会话标题截断上限档位（超宽时优先收紧）
GAP_STEPS = (8, 6, 4)                    # 块间距收缩档位（其次；起步 8 = BLOCK_GAP）
CLOSE_RESERVE_W = 34                     # 右上 close 小块预留宽（24 块宽 + 10 右边距）
WIDTH_HYSTERESIS = 8                     # 宽度变化 <8px 不更新 geometry（防数字跳动闪烁）
ICON_FONT = ("Segoe UI Symbol", 10)  # 块左侧彩色标识字形（Windows 自带符号字体）
ICON_DUR = u"\u25f7"     # ◷ 耗时
ICON_IN = u"\u25b8"      # ▸ in
ICON_OUT = u"\u25c2"     # ◂ out
ICON_HIT = u"\u25d0"     # ◐ 缓存命中
ICON_CRD = u"\u21bb"     # ↻ cache read
ICON_RSN = u"\u2726"     # ✦ reasoning
ICON_SPD = u"\u26a1"     # ⚡ 速度 tok/s
ICON_LIVE = u"\u25cf"    # ● 实时生成中

# ---- 靠边收起（collapsed 把手）----
HANDLE_H = 18            # 收起把手高度（小条 ~72x18）
HANDLE_W_DEFAULT = 72    # 收起把手宽度估算（内容自适应渲染；记忆位置/越界回退用）
HANDLE_HOVER_MS = 500    # 把手悬停多久自动展开（毫秒）
# ---- 收起冷却/展开守卫 ----
COLLAPSE_HOVER_GRACE_MS = 1500   # 收起冷却窗：收起后这段时间内把手不响应悬停展开
DOUBLE_CLICK_TAIL_MS = 500      # 双击尾巴遮蔽窗：收起后此刻内残余 button 释放
                                # 不喂手势机（双击收起的 Release#2 不再 arm_click 展开）
                                 # （防双击收起后把手恰在指针下 -> 500ms 悬停自展开 ->「收起又自己恢复」）
HANDLE_FALLBACK_MARGIN = 8  # 越界回退右下角时距工作区右/下缘的留白
HANDLE_TIP = (u"\u5df2\u6536\u8d77\u2014\u2014"
              u"\u60ac\u505c\u6216\u5355\u51fb\u5c55\u5f00\u5b8c\u6574\u7edf\u8ba1")
              # 已收起——悬停或单击展开完整统计

# 默认显示项配置（写 statusbar-config.json 用）
DEFAULT_CONFIG = {
    "show_model": True,
    "show_session": True,
    "show_avg_duration": True,
    "show_input": True,
    "show_output": True,
    "show_cache_read": True,
    "show_cache_hit": True,
    "show_speed": True,
    "show_reasoning": False,
    "show_live": True,
    # ---- 0.4.0 对话级三区（状态徽标 / 本轮统计 / 会话累计）----
    "show_status": True,
    "show_recent_turn": True,
    "show_cumulative": True,
    "collapsed": False,
    "handle_x": None,
    "handle_y": None,
    "refresh_ms": 1000,
    "theme": "dark",
}

HWND_TOPMOST = ctypes.c_void_p(-1)
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000  # 点击不激活（不抢前台焦点）
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
MONITOR_DEFAULTTONEAREST = 2
MONITOR_DEFAULTTOPRIMARY = 1


# ---------------------------------------------------------------------------
# 配置读取（容错：坏 JSON / 缺字段 -> 默认值 + err 日志，不崩）
# ---------------------------------------------------------------------------

def _log_err(data_dir, msg):
    """写一条调试/错误日志到数据目录 docked-statusbar-err.log；失败忽略。"""
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "docked-statusbar-err.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _throttled_collapsed_err(state, data_dir, name):
    """收起把手判定失败诊断日志（0.2.2）：同因连续失败只记一行，不刷屏；
    判定恢复后由收起态成功路径清 state["last_collapse_err"]=None。"""
    if state.get("last_collapse_err") == name:
        return False
    state["last_collapse_err"] = name
    _log_err(data_dir,
             "collapsed-handle: %s failed, fallback=default-dock" % name)
    return True


def load_config(config_path):
    """
    读取状态条显示配置。返回 (config_dict, source_path)。
    容错规则：
      - 文件不存在 -> 自动生成默认配置文件（DEFAULT_CONFIG）并返回默认值；
      - 坏 JSON / 值类型不对 / 未知键 -> 回退默认值并写一条日志（不崩）；
      - refresh_ms 截断极性、clamp 到 [250, 60000]。
    """
    cfg = dict(DEFAULT_CONFIG)
    if not config_path or not os.path.exists(config_path):
        # 缺省：自动生成默认配置文件，供用户在数据目录手改
        if config_path:
            try:
                os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
                if not os.path.exists(config_path):
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return cfg, config_path

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        _log_err(os.path.dirname(config_path) or DATA_DIR_DEFAULT,
                 "statusbar-config.json parse error, using defaults: %r" % (e,))
        return cfg, config_path
    if not isinstance(raw, dict):
        _log_err(os.path.dirname(config_path) or DATA_DIR_DEFAULT,
                 "statusbar-config.json not an object, using defaults")
        return cfg, config_path

    _apply_raw_config(cfg, raw)
    # 0.5.0 配置显式化：补齐缺失键（show_live / show_status /
    # show_recent_turn / show_cumulative / show_speed 等）写回文件，
    # 只补默认值，不覆盖用户已有值。
    missing_keys = [k for k in DEFAULT_CONFIG if k not in raw]
    if missing_keys:
        for _k in missing_keys:
            raw[_k] = DEFAULT_CONFIG[_k]
        try:
            os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
            _tmp = config_path + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            os.replace(_tmp, config_path)
        except Exception:
            pass
    return cfg, config_path


def _apply_raw_config(cfg, raw, skip_collapsed=False):
    """把已解析的 raw dict 按 schema 原地合并进 cfg（load_config 与热加载共用）。

    skip_collapsed=True 时跳过 collapsed 键（默认合并——load_config 启动恢复用）：
    热加载只同步其它键，collapsed 的写权只留给 collapse_bar/expand_bar（用户主动
    双击/点击展开时原子写回），避免热加载重读 mtime 边缘把 cfg["collapsed"] 翻
    回 false，导致下次写回盘上 collapsed 被误清（热加载竞态自恢复源之一）。
    """
    for key in ("show_model", "show_session", "show_avg_duration", "show_input",
                "show_output", "show_cache_read", "show_cache_hit", "show_speed",
                "show_reasoning", "show_live",
                "show_status", "show_recent_turn", "show_cumulative",
                "collapsed"):
        if skip_collapsed and key == "collapsed":
            continue
        if key in raw:
            v = raw[key]
            if isinstance(v, bool):
                cfg[key] = v
            elif isinstance(v, int) and v in (0, 1):
                cfg[key] = bool(v)
    if "refresh_ms" in raw:
        try:
            rms = int(raw["refresh_ms"])
            if rms > 0:
                cfg["refresh_ms"] = min(max(rms, 250), 60000)
        except Exception:
            pass
    for _hk in ("handle_x", "handle_y"):
        if _hk in raw:
            _hv = raw[_hk]
            if isinstance(_hv, bool):
                continue  # True/False 不是有效坐标
            try:
                _iv = int(_hv)
            except Exception:
                _iv = None
            if _iv is not None:
                cfg[_hk] = _iv
            else:
                cfg[_hk] = None
    if "theme" in raw and isinstance(raw.get("theme"), str) and raw["theme"]:
        cfg["theme"] = raw["theme"]


def hot_reload_config(cfg, config_path, data_dir, prev_mtime):
    """配置热加载：mtime 变化则重读并合并进 cfg（原地改，调用方持有的引用不换）。

    返回最新 mtime（供调用方下次比对）：
      - 文件不存在 / mtime 未变 -> 原样返回 prev_mtime；
      - 坏 JSON / 非 object -> 写一行 err 日志、**保留当前配置**，
        但返回新 mtime（避免每帧重读坏文件刷日志；用户再保存才重试）；
      - 正常 -> 合并生效（refresh_ms 变化由调用方从 cfg 读取应用）。
    """
    if not config_path:
        return prev_mtime
    try:
        mtime = os.path.getmtime(config_path)
    except Exception:
        return prev_mtime
    if mtime == prev_mtime:
        return prev_mtime
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config root is not an object")
    except Exception as e:
        _log_err(data_dir, "config hot-reload failed, keep current config: %r" % (e,))
        return mtime
    _apply_raw_config(cfg, raw, skip_collapsed=True)
    return mtime


# 右键「显示项」子菜单的开关项：(中文标签, 配置键)，顺序即菜单顺序。
# 0.4.0 改版为对话级三区：状态徽标 / 本轮统计 / 会话累计（旧指标块时代的
# show_* 键仍保留在配置中兼容旧配置文件，但不再对应可见区域）。
SHOW_MENU_ITEMS = (
    (u"\u72b6\u6001\u5fbd\u6807", "show_status"),         # 状态徽标
    (u"\u672c\u8f6e\u7edf\u8ba1", "show_recent_turn"),    # 本轮统计
    (u"\u4f1a\u8bdd\u7d2f\u8ba1", "show_cumulative"),    # 会话累计
)


def save_config_keys(config_path, cfg, data_dir, keys):
    """把 cfg 中指定 keys 原子写回 statusbar-config.json（右键菜单 / 收起展开用）。

    - 文件里其它字段（含未知键、refresh_ms、theme、collapsed）原样保留；
    - 原子写：先写 .tmp 再 os.replace；
    - 任何失败只记 err 日志、返回 False，绝不抛出（菜单点击不能崩小条）。
    """
    try:
        raw = {}
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if isinstance(obj, dict):
                    raw = obj
            except Exception:
                raw = {}
        for key in keys:
            v = cfg.get(key)
            if isinstance(v, bool):
                raw[key] = v
            else:
                raw[key] = v
        os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path)
        return True
    except Exception as e:
        try:
            _log_err(data_dir, "save statusbar-config.json failed: %r" % (e,))
        except Exception:
            pass
        return False


def save_config_show_keys(config_path, cfg, data_dir):
    """兼容包装：写回全部显示项开关（show_*）。"""
    return save_config_keys(config_path, cfg, data_dir,
                            [k for _label, k in SHOW_MENU_ITEMS])


def default_handle_xy(work_area, handle_w=HANDLE_W_DEFAULT, handle_h=HANDLE_H):
    """默认把手位置 = 工作区底部水平居中（None 记忆 / 收起恢复的默认值）。
    work_area 为 (left, top, right, bottom)；取不到返回 None。"""
    if not work_area or len(work_area) != 4:
        return None
    wl, wt, wr, wb = work_area
    if wr <= wl or wb <= wt:
        return None
    x = wl + (wr - wl - handle_w) // 2
    x = max(x, wl + MARGIN)
    y = wb - handle_h - MARGIN
    return x, y


def valid_and_clamped_handle_xy(xy, work_area,
                                handle_w=HANDLE_W_DEFAULT, handle_h=HANDLE_H,
                                margin=HANDLE_FALLBACK_MARGIN):
    """把记忆的把手预设坐标 (x, y) 处理成可停靠坐标：

    - 无记忆（None / 非二元组 / 含 None 成员）-> 默认底部居中（default_handle_xy）；
    - 坐标夹在 work_area 内可完整放下 -> 原样返回（已在区内的手工拖动值）；
    - 越出工作区 / 工作区过小放不下 -> 回退默认**右下角**
      (工作区宽-把手宽-margin, 工作区高-把手高-margin)。
    work_area 取不到返回 None。"""
    if not work_area or len(work_area) != 4:
        return None
    wl, wt, wr, wb = work_area
    if wr <= wl or wb <= wt:
        return None
    # (None, None) 也是「无记忆」：cfg 的 handle_x/handle_y 在用户从未拖过
    # 把手时为 None（0.2.2 修复：旧守卫 `not xy or len(xy) != 2` 放过了
    # (None, None)，int(None) 抛 TypeError，poll 外层 except 把收起把手
    # SW_HIDE 掉——「双击收起后把手消失」的确定性根因之一）。
    if (not xy or len(xy) != 2
            or xy[0] is None or xy[1] is None):
        return default_handle_xy(work_area, handle_w, handle_h)
    fx = int(xy[0])
    fy = int(xy[1])
    if (fx < wl or fy < wt or fx + handle_w > wr or fy + handle_h > wb
            or wr - wl < handle_w + margin * 2
            or wb - wt < handle_h + margin * 2):
        dx = wr - handle_w - margin
        dy = wb - handle_h - margin
        return dx, dy
    return fx, fy


def collapsed_poll_decision(state, cfg, bar_w, alive, zcode_hwnd,
                            is_iconic, current_xy, work_area,
                            valid_and_clamped, default_dock, on_err):
    """收起态 poll 判定（0.2.2 抽为模块级纯函数，可独立重放测试；副作用
    move/show/hide 仍由调用方 run_gui.poll 执行，本函数只做裁决）。

    依赖全部显式注入（不隐式依赖闭包/全局），判定语义（0.2.2 根因修复）：
      - ZCode 进程退出 / 真窗口最小化 -> 藏（语义保留）；
      - 坐标/工作区判定**瞬时失败**（current_window_xy 或
        valid_and_clamped_handle_xy 返回 None）-> 不再隐藏，回退默认停靠
        （工作区底部居中，default_handle_xy）并保持可见。

    参数：
      - state:           run_gui state（读 manual_handle）
      - cfg:             配置（读 handle_x/handle_y 记忆把手坐标）
      - bar_w:           把手宽度
      - alive:           ZCode 进程是否存活（False -> 藏）
      - zcode_hwnd:      ZCode 窗口句柄（0/None 视为取不到，跳过最小化判定）
      - is_iconic(h):    窗口是否最小化（命中 -> 藏）
      - current_xy():    小条当前屏幕坐标；失败返回 None
      - work_area(zrect): 矩形所在显示器工作区；失败返回 None
      - valid_and_clamped(xy, wa): 记忆坐标 -> 可停靠坐标；wa 不可得返回 None
      - default_dock(bar_w): 回退默认停靠坐标；失败返回 None
      - on_err(name):    判定失败诊断回调（仅失败时调用；调用方负责落
                          docked-statusbar-err.log 与同因去重）

    返回 (visible, hxy)：
      - (False, None)    -> 调用方 SW_HIDE（ZCode 退出/最小化；极端兜底失败）
      - (True, None)     -> 调用方 SW_SHOW（manual_handle：不动位置）
      - (True, (x, y))   -> 调用方 move_window 到 (x,y) 后 SW_SHOW
    """
    if not alive:
        return False, None
    if zcode_hwnd and is_iconic(zcode_hwnd):
        return False, None
    if state.get("manual_handle"):
        return True, None

    def _safe(fn):
        """坐标/工作区依赖调用防抛：任一判定抛异常视同失败（走回退默认停靠），
        绝不让异常冒泡到 poll 外层 except 导致 SW_HIDE（0.2.2 根因）。"""
        try:
            return fn()
        except Exception:
            return None

    xy = _safe(current_xy)
    if xy is None:
        on_err("current_window_xy")
        hxy = _safe(lambda: default_dock(bar_w))
        if hxy is None:
            return False, None
        return True, hxy
    wa = _safe(lambda: work_area((xy[0], xy[1],
                                  xy[0] + bar_w, xy[1] + HANDLE_H)))
    hxy = _safe(lambda: valid_and_clamped(
        (cfg.get("handle_x"), cfg.get("handle_y")), wa))
    if hxy is None:
        on_err("valid_and_clamped_handle_xy")
        hxy = _safe(lambda: default_dock(bar_w))
        if hxy is None:
            return False, None
        return True, hxy
    return True, hxy


# ---------------------------------------------------------------------------
# 纯数据 / 统计口径（与 inject_context.py 一致，自包含、可独立测试）
# ---------------------------------------------------------------------------

def _num(v):
    """安全转数字；失败返回 0。"""
    if v is None:
        return 0
    try:
        return float(v)
    except Exception:
        try:
            return int(v)
        except Exception:
            return 0


def time_ms():
    """当前 epoch 毫秒（通用时钟，只用于 freshness 判断与展示）。"""
    return int(time.time() * 1000)


def _is_subagent_sid(sid):
    """session id 是否属于 subagent 会话（ZCode 给子代理开的独立会话）。

    子代理（subagent）会被 ZCode 以独立 session_id 运行（形如
    ``sess_subagent_*``），它们的 Stop / UserPromptSubmit 也会触发本插件钩子。
    状态条 / 注入行应当展示**用户正在主会话**的统计，所以凡是 subagent
    会话一律排除（既不做当前会话，也不参与聚合）。
    """
    if not sid or not isinstance(sid, str):
        return False
    s = sid.strip().lower()
    return s.startswith("sess_subagent_") or "subagent" in s


def read_jsonl(path):
    """读取 jsonl 全部行；坏行/空行/非 JSON 跳过。返回 (rows, error)。"""
    rows = []
    error = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except Exception as e:
        error = str(e)
    return rows, error


# token-stats.jsonl 解析缓存（N6 性能）：文件只增不减，refresh_stats 每秒
# 全量逐行 json.loads 会越用越卡；以 (st_mtime_ns, st_size) 指纹判断文件
# 是否变化，未变化直接复用上次解析结果。仅 GUI 刷新循环使用；--once 与
# 其他读取方仍走原 read_jsonl，解析逻辑本身不变。
_jsonl_cache = {"key": None, "rows": [], "err": None}


def read_jsonl_cached(path):
    """带 (mtime_ns, size) 指纹缓存的 read_jsonl。返回 (rows, error)。

    指纹与上次一致 -> 直接返回缓存（rows 为同一 list 对象，调用方只读）；
    首次 / 文件变化 / stat 失败（如文件暂不可见）-> 全量重读并更新缓存，
    与原行为一致，绝不抛。
    """
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None   # stat 失败不缓存指纹：本次全量读兜底，下一拍再校验
    if key is not None and key == _jsonl_cache["key"]:
        return _jsonl_cache["rows"], _jsonl_cache["err"]
    rows, err = read_jsonl(path)
    _jsonl_cache["key"] = key
    _jsonl_cache["rows"] = rows
    _jsonl_cache["err"] = err
    return rows, err


def current_session(rows):
    """取 ts 最大记录的**主会话** id（subagent 会话一律跳过；sessionId 为空退 slug）。"""
    candidates = [r for r in rows if not _is_subagent_sid(r.get("sessionId"))]
    if not candidates:
        return None
    latest = max(candidates, key=lambda r: _num(r.get("ts")))
    sid = latest.get("sessionId")
    if not sid:
        sid = latest.get("slug")
    return sid or None


def aggregate_session(rows, session_id):
    """对当前会话全部行累计求和（subagent 会话 id 不参与聚合）。返回 (agg, last_record)。"""
    if _is_subagent_sid(session_id):
        return _empty_stats(), None
    agg = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheCreationTokens": 0,
        "cacheReadTokens": 0,
        "reasoningTokens": 0,
    }
    last = None
    for r in rows:
        if r.get("sessionId") == session_id or (
            not r.get("sessionId") and r.get("slug") == session_id
        ):
            agg["inputTokens"] += int(_num(r.get("inputTokens")))
            agg["outputTokens"] += int(_num(r.get("outputTokens")))
            agg["cacheCreationTokens"] += int(_num(r.get("cacheCreationTokens")))
            agg["cacheReadTokens"] += int(_num(r.get("cacheReadTokens")))
            agg["reasoningTokens"] += int(_num(r.get("reasoningTokens")))
            if last is None or r.get("ts", 0) >= last.get("ts", 0):
                last = r
    return agg, last


def _empty_stats():
    """标准空 stats dict（所有指标键始终存在）。"""
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheCreationTokens": 0,
        "cacheReadTokens": 0,
        "reasoningTokens": 0,
        "avgDurationMs": 0.0,
    }


def format_tokens(n):
    """>=1_000_000 -> x.xM（1 位小数）；>=1_000 -> x.xk（1 位小数）；否则原值。"""
    n = int(n)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000.0)
    if n >= 1_000:
        return "%.1fk" % (n / 1_000.0)
    return str(n)


def _hit_rate(stats):
    """缓存命中率 = cacheRead / input（db 的 input_tokens 已含 cacheRead 部分）。"""
    denom = stats.get("inputTokens", 0)
    return (stats.get("cacheReadTokens", 0) / denom if denom > 0 else 0.0) * 100.0


def _line_from_stats(stats):
    """由标准 stats dict 生成完整统计行文本（含全部指标）。"""
    return (u"\u23f1%.1fs \u00b7 in %s \u00b7 out %s \u00b7 cache hit %.1f%%"
            % (stats["avgDurationMs"] / 1000.0,
               format_tokens(stats["inputTokens"]),
               format_tokens(stats["outputTokens"]),
               _hit_rate(stats)))


def _db_connect(db_path):
    """只读打开 db.sqlite；失败返回 None。"""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        conn.execute("PRAGMA query_only=ON")
        return conn
    except Exception:
        return None


def _truncate(s, n):
    """截断到 n 个字符（保留首尾所见），失败返回原文。"""
    if s is None:
        return None
    s_out = str(s).strip()
    if len(s_out) <= n:
        return s_out
    return s_out[:n]


def read_mark_raw(data_dir):
    """读取 current-session.json；返回 (session_id, updated_at_ms)。
    文件不存在/坏 JSON/缺字段返回 (None, 0)。不做新鲜度判断。"""
    path = os.path.join(data_dir, MARK_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return None, 0
    sid = None
    try:
        sid = obj.get("session_id")
    except Exception:
        sid = None
    if not sid or not isinstance(sid, str):
        return None, 0
    try:
        updated = int(obj.get("updated_at") or 0)
    except Exception:
        updated = 0
    return sid, updated


def read_mark_file(data_dir):
    """兼容包装：读取 current-session.json 标记；返回 session_id 或 None。
    保留原 30 秒新鲜度语义（供既有调用点使用）。"""
    sid, updated = read_mark_raw(data_dir)
    if not sid or not updated or (time_ms() - updated) > MARK_FRESH_MS:
        return None
    return sid


def _log_line_ts_ms(obj):
    """行内时间字段（ts/time/timestamp；ISO 8601 字符串或 epoch 毫秒）-> epoch ms。
    全部解析失败返回 0（调用方兜底 time_ms()）。"""
    for key in ("ts", "time", "timestamp"):
        try:
            v = obj.get(key)
        except Exception:
            return 0
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                return int(v)
            s = str(v).strip()
            if not s:
                continue
            if s.isdigit():
                return int(s)
            # fromisoformat 在 3.11 前不认 "Z" 后缀，统一替换为 +00:00
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return 0


def tail_session_resume(state, log_dir=None):
    """增量 tail 今日 zcode-YYYY-MM-DD.jsonl，返回最新 session.resumed 的 (session_id, ts_ms)。

    state 键：log_date（str）、log_offset（int）、log_last_sid、log_last_ts。
    规则：
      - 文件名按本地日期 datetime.date.today() 生成；跨天时 offset 归零、重新打开；
      - 从 state["log_offset"] seek（字节偏移），只读新增字节；末行不完整（无 \\n
        结尾）时 offset 回退到最后一个完整行尾，下次再读；
      - 逐行 json.loads，筛 event/type 字段为 "session.resumed" 的行，取 sessionId；
        行内时间字段（ts/time/timestamp，ISO 或 epoch 毫秒）解析失败则用 time_ms()；
      - 文件不存在/权限错/解析全败：返回 (None, 0)，不抛；
      - 无新增字节时返回 state 记住的上次结果（log_last_sid, log_last_ts）。

    日志行结构（2026-09-07 取样 ~/.zcode/cli/log/zcode-2026-09-07.jsonl 确认）：
      {"timestamp":"2026-09-07T00:35:50.153Z","level":"info","event":"session.resumed",
       "module":"core.runtime","message":"Session resumed","traceId":"...","spanId":"...",
       "sessionId":"sess_6863b1b6-f165-4751-8811-b2da1420b2b5","status":"completed",
       "context":{"appliedMessageCount":537,...}}
      即：事件名在 event 字段（非 type），时间为 ISO 8601 UTC 的 timestamp 字段，
      会话 id 在 sessionId 字段；子代理 resume 的 sessionId 以 sess_subagent_ 开头
      （过滤在判定层做，tailer 原样上报）。
    """
    if state is None:
        state = {}
    if log_dir is None:
        log_dir = LOG_DIR_DEFAULT
    try:
        today = datetime.date.today().isoformat()
    except Exception:
        return None, 0
    # 跨天：offset 归零，重新打开新日期文件
    if state.get("log_date") != today:
        state["log_date"] = today
        state["log_offset"] = 0
    path = os.path.join(log_dir, "zcode-%s.jsonl" % today)
    offset = state.get("log_offset") or 0
    if not isinstance(offset, int) or offset < 0:
        offset = 0
    try:
        size = os.path.getsize(path)
    except Exception:
        return None, 0
    if offset > size:
        offset = 0  # 文件被截断/轮换：从头读
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read()
    except Exception:
        return None, 0
    new_end = offset + len(raw)
    if raw and not raw.endswith(b"\n"):
        # 末行不完整：offset 回退到最后一个完整行尾，残余下次再读
        last_nl = raw.rfind(b"\n")
        if last_nl < 0:
            raw_complete = b""
            new_end = offset
        else:
            raw_complete = raw[:last_nl + 1]
            new_end = offset + last_nl + 1
    else:
        raw_complete = raw
    state["log_offset"] = new_end
    last_sid = state.get("log_last_sid")
    try:
        last_ts = int(state.get("log_last_ts") or 0)
    except Exception:
        last_ts = 0
    if raw_complete:
        try:
            text = raw_complete.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            try:
                ev = obj.get("event")
                if ev is None:
                    ev = obj.get("type")
            except Exception:
                continue
            if ev != "session.resumed":
                continue
            sid = obj.get("sessionId")
            if not sid or not isinstance(sid, str):
                continue
            ts = _log_line_ts_ms(obj)
            if not ts:
                ts = time_ms()
            last_sid = sid
            last_ts = ts
    state["log_last_sid"] = last_sid
    state["log_last_ts"] = last_ts
    if not last_sid:
        return None, 0
    return last_sid, last_ts


# legacy: superseded by resolve_session_sticky（保留作向后兼容/对照，不再被刷新链路调用）
def resolve_current_session(rows, data_dir, db_path, live_session_id=None):
    """
    综合确定「当前会话」id（fail-closed：判定不充分宁可返回 None 占位，绝不猜）。
    优先级：
      0. live_stream active 且带 sessionId（正在发请求的会话就是当前对话，
         0.4.2 新增最高优先级；非 subagent）-> "live"；
      1. data_dir/current-session.json 新鲜标记（< 30 秒，非 subagent）-> "mark"；
      2. db 最近 DB_ACTIVE_WINDOW_MS（60 秒）内有模型调用的最新主会话
         （model_usage started_at DESC 第一条、非 subagent）-> "db"；
      3. token-stats.jsonl ts 最大主会话记录 -> "jsonl"（调用方需标注
         「（最近会话累计）」，因为这只是「最近有记录的会话」而非确切当前会话）；
      4. 都没有 -> (None, "none")，调用方显示占位文案。
    live_session_id 为 None（无流 / proxy 未提取到）时回退 1-4 原链。
    """
    if live_session_id and not _is_subagent_sid(live_session_id):
        return live_session_id, "live"
    sid = read_mark_file(data_dir)
    if sid and not _is_subagent_sid(sid):
        return sid, "mark"
    if db_path:
        db_sid = db_recent_session_id(db_path, DB_ACTIVE_WINDOW_MS)
        if db_sid:
            return db_sid, "db"
    sid = current_session(rows)
    if sid:
        return sid, "jsonl"
    return None, "none"


def resolve_session_sticky(state, rows, data_dir, db_path):
    """信号驱动 + 粘滞的当前会话判定。返回 (session_id, source)。

    候选信号（各带真实发生时间戳，禁止用读取时刻伪造）：
      mark   : read_mark_raw(data_dir)      -> (sid, updated_at)
      resume : tail_session_resume(state)   -> (sid, ts)
      db     : db_recent_session_activity(db_path, DB_ACTIVE_WINDOW_MS)
               -> (sid, started_at)（窗口外/无行返回 (None, 0)）
    state 键：sticky_sid、sticky_set_at（本函数维护）；
              tail_session_resume 另维护 log_* 键。

    切换规则：
      1. 过滤 subagent sid 后，取时间戳最新的信号（平局按 mark>resume>db 优先）；
      2. sticky 为空（首次）：取该信号 sid；无任何信号 -> current_session(rows)
         的 jsonl 兜底（保持旧行为，source="jsonl"）；都没有 -> (None, "none")；
      3. 最新信号 sid != sticky 且信号 ts > sticky_set_at -> 切换 sticky，
         source 为信号类型；
      4. 否则保持 sticky，source="sticky"（含无任何信号的空闲轮询）。
    """
    if state is None:
        state = {}
    candidates = []
    try:
        sid, ts = read_mark_raw(data_dir)
        if sid and ts and not _is_subagent_sid(sid):
            candidates.append((ts, "mark", sid))
    except Exception:
        pass
    try:
        sid, ts = tail_session_resume(state)
        if sid and ts and not _is_subagent_sid(sid):
            candidates.append((ts, "resume", sid))
    except Exception:
        pass
    try:
        sid, ts = db_recent_session_activity(db_path, DB_ACTIVE_WINDOW_MS)
        if sid and ts and not _is_subagent_sid(sid):
            candidates.append((ts, "db", sid))
    except Exception:
        pass
    newest = max(candidates, key=lambda c: c[0]) if candidates else None
    sticky = state.get("sticky_sid")
    try:
        sticky_set_at = int(state.get("sticky_set_at") or 0)
    except Exception:
        sticky_set_at = 0
    if not sticky:
        if newest is not None:
            state["sticky_sid"] = newest[2]
            state["sticky_set_at"] = newest[0]
            return newest[2], newest[1]
        sid = current_session(rows)
        if sid:
            state["sticky_sid"] = sid
            state["sticky_set_at"] = 0  # jsonl 兜底无真实信号时刻，任何信号可校正
            return sid, "jsonl"
        return None, "none"
    if newest is not None and newest[2] != sticky and newest[0] > sticky_set_at:
        state["sticky_sid"] = newest[2]
        state["sticky_set_at"] = newest[0]
        return newest[2], newest[1]
    return sticky, "sticky"


def db_recent_session_id(db_path, window_ms=DB_ACTIVE_WINDOW_MS):
    """db 侧「最近活跃」判定：window_ms 毫秒内有 model_usage 行的最新主会话 id。

    只把**最近 60 秒内确有模型调用**的主会话当作当前会话（非 subagent），
    无时间窗的「最新一条」不再作为兜底（那是猜测，fail-closed 不猜）。
    started_at 为 epoch 毫秒（与 record_usage 游标同口径）。失败返回 None。
    """
    conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        since = time_ms() - int(window_ms)
        row = conn.execute(
            "SELECT session_id FROM model_usage "
            "WHERE session_id LIKE 'sess_%' "
            "AND session_id NOT LIKE 'sess_subagent_%' "
            "AND started_at >= ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (since,),
        ).fetchone()
        if row and row[0]:
            return row[0]
        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_recent_session_activity(db_path, window_ms=DB_ACTIVE_WINDOW_MS):
    """db 侧「最近活动」信号：window_ms 毫秒内最新主会话模型调用行的
    (session_id, started_at)。与 db_recent_session_id 同查询，但返回真实
    活动时间戳供粘滞判定信号竞争（处置：db 候选不得用读取时刻伪造 ts）。
    窗口外/无行/失败返回 (None, 0)。"""
    conn = _db_connect(db_path)
    if conn is None:
        return None, 0
    try:
        since = time_ms() - int(window_ms)
        row = conn.execute(
            "SELECT session_id, started_at FROM model_usage "
            "WHERE session_id LIKE 'sess_%' "
            "AND session_id NOT LIKE 'sess_subagent_%' "
            "AND started_at >= ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (since,),
        ).fetchone()
        if row and row[0]:
            try:
                return row[0], int(row[1] or 0)
            except Exception:
                return row[0], 0
        return None, 0
    except Exception:
        return None, 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_latest_session_id(db_path):
    """db 侧确定当前活跃主会话：最新 model_usage 的 session_id（跳过 subagent），
    兜底最新 session（同样跳过 subagent）。"""
    conn = _db_connect(db_path)
    if conn is None:
        return None, "db access failed"
    try:
        row = conn.execute(
            "SELECT session_id FROM model_usage "
            "WHERE session_id NOT LIKE 'sess_subagent_%' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return row[0], None
        row = conn.execute(
            "SELECT id FROM session "
            "WHERE id NOT LIKE 'sess_subagent_%' "
            "ORDER BY time_updated DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return row[0], None
        return None, None
    except Exception as e:
        return None, str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_latest_model_id(db_path, session_id):
    """会话最新 model_usage 行的 model_id（ORDER BY started_at DESC）；无则 None。
    subagent 会话不参与展示。"""
    if not session_id or _is_subagent_sid(session_id):
        return None
    conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT model_id FROM model_usage WHERE session_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return (row[0] if row and row[0] else None)
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_session_title(db_path, session_id):
    """session 表按 session_id 取 title（去首尾空白）；subagent / 无则 ''。"""
    if not session_id or _is_subagent_sid(session_id):
        return ""
    conn = _db_connect(db_path)
    if conn is None:
        return ""
    try:
        row = conn.execute(
            "SELECT title FROM session WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
        return ""
    except Exception:
        return ""
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_aggregate_session(db_path, session_id):
    """只读聚合某**主会话**的 completed 行（subagent 会话返回 None，不参与统计）。
    主数据源行级聚合：avgDuration = AVG(completed 行 duration_ms)。
    返回标准 stats dict 或 None（会话无数据/读取失败）。"""
    if _is_subagent_sid(session_id):
        return None
    conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cache_read_input_tokens),0), "
            "COALESCE(SUM(cache_creation_input_tokens),0), "
            "COALESCE(SUM(reasoning_tokens),0), "
            "COALESCE(AVG(duration_ms),0) "
            "FROM model_usage WHERE session_id = ? AND status='completed' "
            "AND COALESCE(query_source,'') <> 'subagent'",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        inp, outp, cache_rd, cache_cre, reas, avg_dur = row
        inp = int(inp or 0)
        outp = int(outp or 0)
        cache_rd = int(cache_rd or 0)
        cache_cre = int(cache_cre or 0)
        reas = int(reas or 0)
        if inp == 0 and outp == 0 and cache_rd == 0 and cache_cre == 0:
            return None
        return {
            "inputTokens": inp,
            "outputTokens": outp,
            "cacheReadTokens": cache_rd,
            "cacheCreationTokens": cache_cre,
            "reasoningTokens": reas,
            "avgDurationMs": float(avg_dur or 0.0),
        }
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_session_speed(db_path, session_id):
    """当前会话的输出速度（tok/s，只读 db；jsonl 无行级数据故仅此一路）。

    - recent = 最近一条 completed 非 subagent 行的 output_tokens/(duration_ms/1000)
      （ORDER BY started_at DESC LIMIT 1；该行 duration_ms 为 NULL/0 时无速度）；
    - avg = SUM(output_tokens)/SUM(duration_ms)*1000（会话平均，completed 行）。
    返回 (recent, avg)，各自可为 None（无 db / 无会话 / 无有效数据）。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None, None
    conn = _db_connect(db_path)
    if conn is None:
        return None, None
    try:
        recent = None
        row = conn.execute(
            "SELECT output_tokens, duration_ms FROM model_usage "
            "WHERE session_id = ? AND status='completed' "
            "AND COALESCE(query_source,'') <> 'subagent' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is not None and row[0] and row[1]:
            dur = _num(row[1])
            if dur > 0:
                recent = row[0] / (dur / 1000.0)
        avg = None
        row2 = conn.execute(
            "SELECT COALESCE(SUM(output_tokens),0), COALESCE(SUM(duration_ms),0) "
            "FROM model_usage WHERE session_id = ? AND status='completed' "
            "AND COALESCE(query_source,'') <> 'subagent'",
            (session_id,),
        ).fetchone()
        if row2 is not None and row2[0] and row2[1]:
            avg = row2[0] / row2[1] * 1000.0
        return recent, avg
    except Exception:
        return None, None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def recent_turn_stats(db_path, session_id):
    """当前会话**最近一轮** turn_usage 统计（0.4.0；表 PRIMARY KEY 为
    (session_id, turn_id)，按 started_at 取最新一行；subagent 过滤同前，
    不参与统计）。只读 db。

    返回 dict（无数据 / 读取失败返回 None）：
      {turn_id, status, startedAt, completedAt, durationMs,
       timeToFirstTokenMs, toolCallCount, toolErrorCount,
       inputTokens, outputTokens, cacheReadTokens, computedTotalTokens}
    供「本轮统计」行与状态判定（turn 未完成特征）使用。
    """
    if not session_id or _is_subagent_sid(session_id):
        return None
    conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT turn_id, status, started_at, completed_at, duration_ms, "
            "time_to_first_token_ms, tool_call_count, tool_error_count, "
            "input_tokens, output_tokens, cache_read_input_tokens, "
            "computed_total_tokens "
            "FROM turn_usage WHERE session_id = ? "
            "AND COALESCE(session_id, '') NOT LIKE 'sess_subagent_%' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        (turn_id, status, started_at, completed_at, duration_ms,
         ttft, tool_calls, tool_errors, inp, outp, cache_rd, total) = row
        return {
            "turn_id": turn_id,
            "status": status,
            "startedAt": started_at,
            "completedAt": completed_at,
            "durationMs": duration_ms,
            "timeToFirstTokenMs": ttft,
            "toolCallCount": int(tool_calls or 0),
            "toolErrorCount": int(tool_errors or 0),
            "inputTokens": int(inp or 0),
            "outputTokens": int(outp or 0),
            "cacheReadTokens": int(cache_rd or 0),
            "computedTotalTokens": int(total or 0),
        }
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_latest_model_usage_status(db_path, session_id):
    """会话最新 model_usage 行的状态特征（0.4.0，状态判定用）：
    返回 {status, tool_call_count} 或 None（无数据 / 读取失败）。
    subagent 会话不参与展示。"""
    if not session_id or _is_subagent_sid(session_id):
        return None
    conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT status, tool_call_count FROM model_usage "
            "WHERE session_id = ? "
            "AND COALESCE(query_source, '') <> 'subagent' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "tool_call_count": int(row[1] or 0)}
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _read_status_state(data_dir):
    """读 ZCode 钩子时序状态文件 status-state.json（status_event.py 原子写）；
    不存在 / 坏 JSON 返回 None（绝不抛）。0.6.0 起状态徽标唯一来源。"""
    try:
        with open(os.path.join(data_dir, STATUS_STATE_NAME), "r",
                  encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def db_latest_speed(db_path):
    """最近一条 completed 非 subagent model_usage 的精确速度（0.6.0）：
    output_tokens / (duration_ms / 1000)。返回 float 或 None（无数据 / 失败）。
    tok/s 数据源从实时流改为 db 精确值，不再依赖 live_stream。"""
    conn = _db_connect(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT output_tokens, duration_ms FROM model_usage "
            "WHERE status = 'completed' "
            "AND COALESCE(query_source, '') <> 'subagent' "
            "AND output_tokens > 0 AND duration_ms > 0 "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        outp, dur = row
        if not outp or not dur:
            return None
        return outp / (dur / 1000.0)
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def status_detector(status_state, mu_row, tu_row, now=None):
    """状态判定（0.6.0；纯函数，可独立单测）。返回 (status, speed_tok_per_s)：
      status ∈ {'generating', 'tool', 'idle'}；speed 恒 None（生成中时由
      调用方以 db_latest_speed 填充精确速度）。

      状态来源 = ZCode 钩子时序（status-state.json，status_event.py 写）：
        a. 最近事件 generating 且距 now < STATUS_IDLE_AFTER_MS（60s）
           -> 'generating'（绿⚡）；
        b. 最近事件 tool 且距 now < STATUS_IDLE_AFTER_MS -> 'tool'（蓝🔧）；
        c. 否则 'idle'（无文件 / 事件超龄 / 事件未知 / 会话未识别）。

      0.6.0 起不再读 live_stream、不再依赖代理（live_stream 即使存在也不
      作为状态来源）。mu_row / tu_row 参数保留（历史签名兼容），不再参与
      状态判定——状态完全由钩子时序决定。
    """
    if now is None:
        now = time.time()
    if isinstance(status_state, dict):
        event = status_state.get("event")
        ts = status_state.get("ts")
        if event and ts:
            try:
                elapsed_ms = (now * 1000.0) - float(ts)
            except Exception:
                elapsed_ms = STATUS_IDLE_AFTER_MS
            if elapsed_ms < STATUS_IDLE_AFTER_MS:
                if event == "generating":
                    return "generating", None
                if event == "tool":
                    return "tool", None
    return "idle", None


def resolve_turn_status(status_state, db_path, session_id):
    """0.6.0 统一解析「状态 + 本轮统计」（GUI 每帧与 --once 共用）：
    返回 (status, speed_tok_per_s, turn_stats)：
      - status 走 status_detector（ZCode 钩子时序：generating / tool / idle）；
      - speed 仅生成中非 None：取 db_latest_speed（最近一次 completed
        非 subagent model_usage 的精确 tok/s），不再依赖实时流；
      - turn_stats = recent_turn_stats 输出（最近一轮，无数据为 None）。
    会话未识别（None）时返回 ('idle', None, None)，不猜。
    """
    if not session_id:
        return "idle", None, None
    mu_row = db_latest_model_usage_status(db_path, session_id)
    tu_row = recent_turn_stats(db_path, session_id)
    status, spd = status_detector(status_state, mu_row, tu_row)
    if status == "generating":
        spd = db_latest_speed(db_path)
    return status, spd, tu_row


def cumulative_text(stats):
    """会话累计小字（0.4.0 第三区）：`in <k/M> · out <k/M>`；stats 为 None 返回 None。"""
    if not stats:
        return None
    return (u"in %s \u00b7 out %s"
            % (format_tokens(stats.get("inputTokens") or 0),
               format_tokens(stats.get("outputTokens") or 0)))


def build_stats_line(rows, db_path=None, session_id=None):
    """
    聚合统计。**数据源优先级改为 db 优先**：db 行级聚合（模型调用完成即
    落库，快一轮）-> jsonl 该会话累计（兜底，避免冷启动显示「—」）->
    「本轮结束后更新」。

    session_id 为 None 时：先 jsonl ts 最大主会话，再 db 最新活跃主会话。

    返回 (text, source, used_sid, stats)：
      - text  完整统计行文本（含全部指标，未按配置裁剪）；
      - source 'db' | 'jsonl' | 'jsonl-latest' | 'pending' | 'none'；
      - used_sid 实际参与聚合的会话 id（可能为 None）；
      - stats  标准 stats dict；text 为 STATS_PENDING / 无数据时为 None。
    """
    def _db_or_jsonl(sid):
        """db 主源 -> jsonl 兜底；返回 (text, source, stats) 或 None(无数据)。"""
        if db_path:
            st = db_aggregate_session(db_path, sid)
            if st is not None:
                return _line_from_stats(st), "db", st
        agg, last = aggregate_session(rows, sid)
        if last is not None:
            st = {
                "inputTokens": agg["inputTokens"],
                "outputTokens": agg["outputTokens"],
                "cacheCreationTokens": agg["cacheCreationTokens"],
                "cacheReadTokens": agg["cacheReadTokens"],
                "reasoningTokens": agg["reasoningTokens"],
                "avgDurationMs": _num(last.get("avgDurationMs")),
            }
            return _line_from_stats(st), "jsonl", st
        return None

    if session_id is not None:
        got = _db_or_jsonl(session_id)
        if got is None:
            return STATS_PENDING, "pending", session_id, None
        text, src, stats = got
        return text, src, session_id, stats

    sid = current_session(rows)
    if sid is not None:
        got = _db_or_jsonl(sid)
        if got is None:
            return STATS_PENDING, "pending", sid, None
        text, src, stats = got
        return text, src, sid, stats

    if db_path:
        cur_sid, _err = db_latest_session_id(db_path)
        if cur_sid:
            got = _db_or_jsonl(cur_sid)
            if got is None:
                return STATS_PENDING, "pending", cur_sid, None
            text, src, stats = got
            return text, src, cur_sid, stats

    last_row = next((r for r in reversed(rows)
                     if not _is_subagent_sid(r.get("sessionId"))), None)
    if last_row is not None:
        st = {
            "inputTokens": int(_num(last_row.get("inputTokens"))),
            "outputTokens": int(_num(last_row.get("outputTokens"))),
            "cacheCreationTokens": int(_num(last_row.get("cacheCreationTokens"))),
            "cacheReadTokens": int(_num(last_row.get("cacheReadTokens"))),
            "reasoningTokens": int(_num(last_row.get("reasoningTokens"))),
            "avgDurationMs": _num(last_row.get("avgDurationMs")),
        }
        return _line_from_stats(st), "jsonl-latest", last_row.get("sessionId"), st
    return None, "none", None, None


def session_label(db_path, session_id):
    """会话显示标识：session.title 截断 ~16 字符；无 title -> (未命名会话)。"""
    if not session_id:
        return u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09"  # （未命名会话）
    title = db_session_title(db_path, session_id)
    if title:
        return _truncate(title, 16) or u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09"
    return u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09"


def resolve_gui_info(rows, data_dir, db_path, cfg=None, cur=None,
                     live_session_id=None, sess_state=None):
    """
    每帧（GUI / --once）统一解析展示信息，返回 dict：
      {text, source, session_id, model, session_label, line1, line2, stats}
    其中：
      - session 判定走 resolve_session_sticky（mark/resume/db 信号竞争 + 粘滞；
        sess_state=None 时内部自建临时 dict，向后兼容、退化为无粘滞每拍重判）；
      - 统计按该会话 db 优先聚合（build_stats_line）；stats 为标准 dict；
      - model / title 读 db（会话判定为新会话时才重读，否则沿用 cur 缓存）；
      - line1 按配置 show_model/show_session 裁剪；model 保留完整值（tooltip）。
    任何异常兜底为 None / 「—」，绝不外抛。

    live_session_id 来自 live_stream.json（0.4.2 新增）：stream active 且
    sessionId 存在时传该 id，用于会话判定链最高优先级。
    """
    cfg = cfg or dict(DEFAULT_CONFIG)
    info = {
        "text": None,
        "source": "none",
        "session_id": None,
        "model": None,
        "session_label": None,
        "line1": None,
        "line2": None,
        "stats": None,
        "speed_recent": None,
        "speed_avg": None,
        "recent_note": False,
        "error": None,
    }
    try:
        if sess_state is None:
            sess_state = {}  # 向后兼容：临时态退化为无粘滞（每拍走首次分支）
        sid, source = resolve_session_sticky(sess_state, rows, data_dir, db_path)
        info["session_id"] = sid
        info["source"] = source
        if sid is None:
            # fail-closed：判定不充分时显示占位，绝不猜一个会话
            info["line1"] = SESSION_UNKNOWN
            info["line2"] = SESSION_UNKNOWN
            info["text"] = SESSION_UNKNOWN
            return info

        # model / title：会话变化（或缺省）才重读 db（model/title 不频繁变化）
        reuse = bool(cur and cur.get("session_id") == sid
                     and cur.get("model") is not None
                     and cur.get("session_label") is not None)
        if reuse:
            model = cur.get("model")
            label = cur.get("session_label")
        else:
            model = db_latest_model_id(db_path, sid)
            label = session_label(db_path, sid)
            if not model:
                model = "model?"
            if not label:
                label = u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09"
        info["model"] = model
        info["session_label"] = label
        # line1 文本版（按配置裁剪；model 保留完整值供 tooltip）
        parts1 = []
        if cfg.get("show_model", True):
            parts1.append(_truncate(model, 20) or "model?")
        if cfg.get("show_session", True):
            parts1.append(label)
        info["line1"] = u" \u00b7 ".join(parts1) if parts1 else u"\u2014"

        text, _src, used_sid, stats = build_stats_line(rows, db_path, sid)
        info["recent_note"] = (source == "jsonl" and stats is not None)
        if info["recent_note"] and text:
            text = text + u" " + SESSION_RECENT_NOTE
        info["text"] = text
        info["line2"] = text
        info["stats"] = stats
        # 速度（tok/s）：db 行级（jsonl 无单次 duration，速度仅 db 有数据）；
        # 附到 stats dict 供指标块渲染，同时平铺到 info 顶层供 --once 输出。
        spd_recent, spd_avg = db_session_speed(db_path, sid)
        info["speed_recent"] = spd_recent
        info["speed_avg"] = spd_avg
        if stats is not None:
            stats["speedTokPerSec"] = spd_recent
            stats["speedAvgTokPerSec"] = spd_avg
        if used_sid is not None:
            info["session_id"] = used_sid
    except Exception as e:
        info["error"] = str(e)
        for k in ("line1", "line2", "text"):
            if info.get(k) is None:
                info[k] = STATS_PENDING
    return info


# ---------------------------------------------------------------------------
# 显示项拼接（按配置 show_* 决定第二行拼哪些指标）
# ---------------------------------------------------------------------------

def build_line2_parts(stats, cfg):
    """
    按配置把 stats 拆成带样式的片段列表，用于第二行渲染 + tooltip 悬停热区。
    返回 [ (kind, text, font, fg, tooltip_text), ... ]
      kind: 'duration' | 'label' | 'in' | 'out' | 'cache' | 'cache_read' |
            'reasoning' | 'sep' | 'text'
    stats 为 None（无数据）时返回 [('text', STATS_PENDING)]（与 --once 文案统一）。
    全关则返回 [('text', STATS_PENDING)]。
    """
    if not stats:
        return [("text", STATS_PENDING, FONT_MAIN, FG, None)]
    show_dur = cfg.get("show_avg_duration", True)
    show_in = cfg.get("show_input", True)
    show_out = cfg.get("show_output", True)
    show_cache_read = cfg.get("show_cache_read", True)
    show_cache_hit = cfg.get("show_cache_hit", True)
    show_reasoning = cfg.get("show_reasoning", False)

    dur_s = stats.get("avgDurationMs", 0) / 1000.0
    inp = stats.get("inputTokens", 0)
    outp = stats.get("outputTokens", 0)
    cache_rd = stats.get("cacheReadTokens", 0)
    hit = _hit_rate(stats)
    reas = stats.get("reasoningTokens", 0)

    parts = []

    def _add_sep():
        if parts and parts[-1][0] != "sep":
            parts.append(("sep", u" \u00b7 ", FONT_MAIN, SEP_COLOR, None))

    if show_dur:
        parts.append(("duration", u"\u23f1%.1fs" % dur_s, FONT_NUM, FG,
                      u"\u5e73\u5747\u8017\u65f6\uff1a\u5f53\u524d\u5bf9\u8bdd\u5e73\u5747\u6bcf\u6b21\u6a21\u578b\u8c03\u7528\u7684\u65f6\u957f\uff08\u4e0d\u542b\u5b50\u4ee3\u7406\uff09"))
    if show_in:
        _add_sep()
        parts.append(("label", u"in ", FONT_MAIN, FG_DIM,
                      u"in\uff1a\u8f93\u5165 token \u7d2f\u8ba1\uff08\u542b\u7f13\u5b58\u8bfb\u53d6\u90e8\u5206\uff09"))
        parts.append(("in", format_tokens(inp), FONT_NUM, FG, None))
    if show_out:
        _add_sep()
        parts.append(("label", u"out ", FONT_MAIN, FG_DIM,
                      u"out\uff1a\u8f93\u51fa token \u7d2f\u8ba1"))
        parts.append(("out", format_tokens(outp), FONT_NUM, FG, None))
    if show_cache_hit:
        _add_sep()
        parts.append(("label", u"cache ", FONT_MAIN, FG_DIM,
                      # 与 TIP_HIT 同口径：输入总量已含缓存读取部分，不重复相加
                      u"cache hit\uff1a\u7f13\u5b58\u547d\u4e2d\u7387 = "
                      u"\u7f13\u5b58\u8bfb\u53d6 \u00f7 \u8f93\u5165\u603b\u91cf"
                      u"\uff08\u8f93\u5165\u5df2\u542b\u7f13\u5b58\u8bfb\u53d6"
                      u"\u90e8\u5206\uff09\uff0c\u8d8a\u9ad8\u8d8a\u7701\u94b1"))
        parts.append(("cache", u"hit %.1f%%" % hit, FONT_NUM, ACCENT_GREEN, None))
    if show_cache_read:
        # cache read 与 cache hit 独立开关：两者都开时分开显示（红/蓝数字），
        # 均符合"关键数字等宽 + 主题色"规范。
        _add_sep()
        parts.append(("label", u"cache read ", FONT_MAIN, FG_DIM,
                      u"cache read\uff1a\u7f13\u5b58\u8bfb\u53d6 token \u7d2f\u8ba1\uff08\u547d\u4e2d\u90e8\u5206\uff09"))
        parts.append(("cache_read", format_tokens(cache_rd), FONT_NUM, FG, None))
    if show_reasoning and reas:
        _add_sep()
        parts.append(("label", u"reasoning ", FONT_MAIN, FG_DIM,
                      u"reasoning\uff1a\u601d\u8003\uff08reasoning\uff09token \u7d2f\u8ba1"))
        parts.append(("reasoning", format_tokens(reas), FONT_NUM, FG, None))

    if not parts:
        return [("text", STATS_PENDING, FONT_MAIN, FG, None)]
    return parts


def stats_to_text(stats, cfg):
    """按配置把 stats 渲染成一行文本（--once 的 line 输出；GUI 侧改为指标块渲染）。"""
    if stats is None:
        return STATS_PENDING
    parts = build_line2_parts(stats, cfg)
    return "".join(p[1] for p in parts)


# ---------------------------------------------------------------------------
# 彩色指标块（GUI 第二行 Canvas 渲染的纯数据层；--once 不经过这里）
# ---------------------------------------------------------------------------

# 各指标块 tooltip 文案（与旧 build_line2_parts 保持同文）
TIP_DUR = u"\u5e73\u5747\u8017\u65f6\uff1a\u5f53\u524d\u5bf9\u8bdd\u5e73\u5747\u6bcf\u6b21\u6a21\u578b\u8c03\u7528\u7684\u65f6\u957f\uff08\u4e0d\u542b\u5b50\u4ee3\u7406\uff09"
TIP_IN = u"in\uff1a\u8f93\u5165 token \u7d2f\u8ba1\uff08\u542b\u7f13\u5b58\u8bfb\u53d6\u90e8\u5206\uff09"
TIP_OUT = u"out\uff1a\u8f93\u51fa token \u7d2f\u8ba1"
TIP_HIT = u"cache hit\uff1a\u7f13\u5b58\u547d\u4e2d\u7387 = \u7f13\u5b58\u8bfb\u53d6 \u00f7 \u8f93\u5165\u603b\u91cf\uff08\u8f93\u5165\u5df2\u542b\u7f13\u5b58\u8bfb\u53d6\u90e8\u5206\uff09\uff0c\u8d8a\u9ad8\u8d8a\u7701\u94b1"
TIP_CRD = u"cache read\uff1a\u7f13\u5b58\u8bfb\u53d6 token \u7d2f\u8ba1\uff08\u547d\u4e2d\u90e8\u5206\uff09"
TIP_RSN = u"reasoning\uff1a\u601d\u8003\uff08reasoning\uff09token \u7d2f\u8ba1"
TIP_SPD = (u"\u901f\u5ea6\uff1a\u6700\u8fd1\u4e00\u6b21\u8bf7\u6c42\u7684\u8f93\u51fa\u901f\u5ea6"
           u" = \u8f93\u51fa token \u00f7 \u8be5\u6b21\u8017\u65f6")
           # 速度：最近一次请求的输出速度 = 输出 token ÷ 该次耗时


def build_metric_blocks(stats, cfg):
    """
    按配置把 stats 拆成「彩色指标块」描述列表（纯数据；块宽由渲染层按字体实测）。
    返回 [ {kind, icon, icon_color, label, value, value_color, tip, progress}, ... ]：
      - progress 仅缓存命中块非 None（0-100 的命中率数值，用于微型进度条）；
      - stats 为 None / 显示项全关 -> 单个占位块 {kind:'pending', text:...}，
        文案与 fail-closed 口径一致（「（本轮结束后更新）」）。
    """
    if not stats:
        return [{"kind": "pending", "text": STATS_PENDING, "tip": None}]
    blocks = []
    if cfg.get("show_avg_duration", True):
        blocks.append({
            "kind": "duration", "icon": ICON_DUR, "icon_color": FG_DIM,
            "label": u"\u8017\u65f6",
            "value": u"%.1fs" % (stats.get("avgDurationMs", 0) / 1000.0),
            "value_color": FG, "tip": TIP_DUR, "progress": None,
        })
    if cfg.get("show_input", True):
        blocks.append({
            "kind": "input", "icon": ICON_IN, "icon_color": FG,
            "label": "in",
            "value": format_tokens(stats.get("inputTokens", 0)),
            "value_color": FG, "tip": TIP_IN, "progress": None,
        })
    if cfg.get("show_output", True):
        blocks.append({
            "kind": "output", "icon": ICON_OUT, "icon_color": FG,
            "label": "out",
            "value": format_tokens(stats.get("outputTokens", 0)),
            "value_color": FG, "tip": TIP_OUT, "progress": None,
        })
    if cfg.get("show_cache_hit", True):
        blocks.append({
            "kind": "cache_hit", "icon": ICON_HIT, "icon_color": ACCENT_GREEN,
            "label": u"\u7f13\u5b58\u547d\u4e2d",
            "value": u"%.1f%%" % _hit_rate(stats),
            "value_color": ACCENT_GREEN, "tip": TIP_HIT,
            "progress": _hit_rate(stats),
        })
    if cfg.get("show_cache_read", True):
        blocks.append({
            "kind": "cache_read", "icon": ICON_CRD, "icon_color": FG_DIM,
            "label": "cache read",
            "value": format_tokens(stats.get("cacheReadTokens", 0)),
            "value_color": FG, "tip": TIP_CRD, "progress": None,
        })
    spd = stats.get("speedTokPerSec")
    if cfg.get("show_speed", True) and spd is not None:
        # 速度块：⚡ 32.4 tok/s（标识/数值橙黄；无 db 行级数据时不画、不留空位）。
        # tooltip 顺带展示会话平均（有数据时）。
        tip_spd = TIP_SPD
        spd_avg = stats.get("speedAvgTokPerSec")
        if spd_avg is not None:
            tip_spd = tip_spd + u"\uff1b\u4f1a\u8bdd\u5e73\u5747\uff1a%.1f tok/s" % spd_avg
        blocks.append({
            "kind": "speed", "icon": ICON_SPD, "icon_color": ACCENT_ORANGE,
            "label": "",
            "value": u"%.1f tok/s" % spd,
            "value_color": ACCENT_ORANGE, "tip": tip_spd, "progress": None,
        })
    if cfg.get("show_reasoning", False) and stats.get("reasoningTokens", 0):
        blocks.append({
            "kind": "reasoning", "icon": ICON_RSN, "icon_color": ACCENT_PURPLE,
            "label": "reasoning",
            "value": format_tokens(stats.get("reasoningTokens", 0)),
            "value_color": FG, "tip": TIP_RSN, "progress": None,
        })
    if not blocks:
        return [{"kind": "pending", "text": STATS_PENDING, "tip": None}]
    return blocks


def compute_block_widths(blocks, measure_icon, measure_label, measure_value):
    """
    实测各指标块单块宽度（像素，不含块间 gap；纯数据层，可独立单测）。
    measure_* 为 callable(text)->px：GUI 用 tkinter.font.Font.measure，
    测试可用 lambda t: len(t)*N 桩。
    """
    ws = []
    for blk in blocks:
        if blk.get("kind") == "pending":
            ws.append(measure_label(blk["text"]) + BLOCK_PAD * 2)
        else:
            ws.append(BLOCK_PAD * 2 + measure_icon(blk["icon"]) + 5
                      + measure_label(blk["label"]) + 5
                      + measure_value(blk["value"]))
    return ws


def plan_statusbar_layout(block_ws, row1_w_fn, note_w, work_w,
                          close_w=CLOSE_RESERVE_W, pad_l=12, pad_r=10):
    """
    决定状态条自适应布局（纯函数；宽度全部由调用方实测后传入，可独立单测）。

    入参：
      block_ws:  第二行各指标块单块宽度列表（顺序即渲染顺序；长度 = 块数）
      row1_w_fn: row1_w_fn(label_max) -> 第一行内容宽（按「标题截到
                 label_max」实测；label_max 取 LABEL_MAX_STEPS 档位）
      note_w:    「（最近会话累计）」标注宽（无标注传 0）
      work_w:    窗口宽上限（所在显示器工作区宽 - 16px）

    返回 {win_w, label_max, gap, show_note, n_blocks, blocks_row_w}。

    不变量（优先级高于美观，绝不违反）：
      - n_blocks 恒等于 len(block_ws)：指标块（含 cache read）任何情况都
        完整渲染，布局层永不因宽度丢块（旧版「放不下整块不画」已废除）；
      - 超上限时的收缩优先级：截短会话标题 -> 缩块间距（8/6/4）->
        省略「（最近会话累计）」标注（最后手段）；
      - win_w 恒 <= work_w（极端小屏全档位仍超时钳到上限，块仍全部渲染）。
    """
    n = len(block_ws)
    blocks_base = sum(block_ws)
    # 收缩候选序列（妥协程度递增；第一个放得下的即胜出）：
    # 1) 优先截短标题（16→12→10→8→6→4，间距/标注不动）；
    # 2) 再缩块间距（6→4，标题保持最短档）；
    # 3) 最后省略「（最近会话累计）」标注。
    candidates = [(lm, GAP_STEPS[0], True) for lm in LABEL_MAX_STEPS]
    candidates += [(LABEL_MAX_STEPS[-1], gap, True) for gap in GAP_STEPS[1:]]
    candidates.append((LABEL_MAX_STEPS[-1], GAP_STEPS[-1], False))
    for lm, gap, show_note in candidates:
        if show_note and note_w <= 0:
            continue
        blocks_row_w = blocks_base + gap * max(n - 1, 0)
        note_extra = (gap + 2 + note_w) if show_note else 0
        row2_w = pad_l + blocks_row_w + note_extra + pad_r
        win_w = max(row1_w_fn(lm), row2_w) + close_w
        if win_w <= work_w:
            return {"win_w": win_w, "label_max": lm, "gap": gap,
                    "show_note": show_note, "n_blocks": n,
                    "blocks_row_w": blocks_row_w}
    # 全档位仍超上限（极端小屏）：最紧凑档（最短标题 + 最小间距 + 省标注），
    # win_w 钳到上限；块仍全部渲染（物理上才可能溢出，逻辑上永不丢块）。
    gap = GAP_STEPS[-1]
    lm = LABEL_MAX_STEPS[-1]
    blocks_row_w = blocks_base + gap * max(n - 1, 0)
    row2_w = pad_l + blocks_row_w + pad_r
    win_w = min(max(row1_w_fn(lm), row2_w) + close_w, work_w)
    return {"win_w": win_w, "label_max": lm, "gap": gap,
            "show_note": False, "n_blocks": n, "blocks_row_w": blocks_row_w}


def plan_statusbar_layout_3zone(badge_w, title_w_fn, turn_w, cum_w, note_w,
                                work_w, close_w=CLOSE_RESERVE_W,
                                pad_l=12, pad_r=10):
    """
    0.4.0 三区自适应布局（纯函数；宽度全部由调用方实测后传入，可独立单测）。

    入参：
      badge_w:   状态徽标宽（含内边距；show_status=false 时传 0）
      title_w_fn: title_w_fn(label_max) -> 对话名内容宽（标题截到
                 label_max；label_max 取 LABEL_MAX_STEPS 档位）
      turn_w:    本轮统计行宽（show_recent_turn=false 时传 0）
      cum_w:     累计小字宽（show_cumulative=false 时传 0）
      note_w:    「（最近会话累计）」标注宽（无标注传 0）
      work_w:    窗口宽上限（所在显示器工作区宽 - 16px）

    返回 {win_w, label_max, badge_gap, show_cum, show_note}。

    不变量（优先级高于美观，绝不违反）：
      - 状态徽标 / 本轮统计 / 累计小字**永不因宽度裁剪**；
      - 超上限时的收缩优先级：截短对话名（16→12→10→8→6→4）->
        缩间距（徽标-标题 8/6/4，第二行累计间距同步）->
        省略累计小字的「（最近会话累计）」标注（最后手段）；
      - win_w 恒 <= work_w（极端小屏全档位仍超时钳到上限，徽标与统计
        仍全部渲染，物理上才可能溢出，逻辑上永不裁统计）。
    """

    def _row1_w(lm, bg):
        return pad_l + badge_w + bg + title_w_fn(lm) + pad_r + close_w

    def _row2_w(bg, sc, sn):
        nw = (note_w + 6) if (sc and sn) else 0
        return pad_l + turn_w + bg + (cum_w if sc else 0) + nw + pad_r + close_w

    # 收缩候选序列（妥协程度递增；第一个放得下的即胜出）：
    # 1) 截短标题；2) 缩间距；3) 省累计标注（仅当累计区开启时才有意义）。
    # show_cum 恒等于 cum_w>0（配置决定），布局层**永不整区丢弃累计**。
    for lm in LABEL_MAX_STEPS:
        for bg in BADGE_GAP_STEPS:
            for sn in (True, False):
                if sn and note_w <= 0:
                    continue
                w = max(_row1_w(lm, bg), _row2_w(bg, cum_w > 0, sn))
                if w <= work_w:
                    return {"win_w": w, "label_max": lm, "badge_gap": bg,
                            "show_cum": cum_w > 0, "show_note": sn}
    # 全档位仍超上限（极端小屏）：最紧凑档（最短标题 + 最小间距 + 省标注），
    # win_w 钳到上限；徽标/统计仍完整渲染。
    lm = LABEL_MAX_STEPS[-1]
    bg = BADGE_GAP_STEPS[-1]
    sc = cum_w > 0
    w = max(_row1_w(lm, bg), _row2_w(bg, sc, False))
    return {"win_w": min(w, work_w), "label_max": lm, "badge_gap": bg,
            "show_cum": sc, "show_note": False}


def round_rect(cv, x0, y0, x1, y1, radius=6, **kwargs):
    """近似圆角矩形（tkinter 无原生圆角：create_polygon + smooth=True）。"""
    r = min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0)
    pts = [
        x0 + r, y0, x1 - r, y0,
        x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1,
        x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r,
        x0, y0 + r, x0, y0,
    ]
    return cv.create_polygon(pts, smooth=True, **kwargs)


# ---------------------------------------------------------------------------
# 贴边计算（纯函数，可独立单测；zrect = (left, top, right, bottom) 像素）
# ---------------------------------------------------------------------------

def dock_rect(zrect, bar_w, bar_h, margin=MARGIN, mode="below"):
    """
    计算状态条左上角 (x, y) 贴到 ZCode 窗口的位置。

    mode="below": 贴窗口底部外沿下方（y = bottom + margin，默认）；
    mode="inside": 贴窗口底部内侧（y = bottom - bar_h - margin）。
    水平居中；窗口过窄时退回左侧留白。窗口矩形无效返回 None。
    """
    left, top, right, bottom = zrect
    zc_w = right - left
    zc_h = bottom - top
    if zc_w <= 0 or zc_h <= 0:
        return None
    x = left + max((zc_w - bar_w) // 2, margin)
    if mode == "inside":
        y = max(top, bottom - bar_h - margin)
    else:
        y = bottom + margin
    return max(x, 0), max(y, 0)


def clamp_to_work_area(xy, bar_w, bar_h, zrect, margin=MARGIN):
    """
    用 zrect 所在显示器的工作区把 (x,y) 夹回来（防止盖到任务栏 / 移出屏幕）。
    纯函数：work_area 由 zrect 计算。
    """
    if xy is None:
        return None
    x, y = xy
    if zrect is not None and len(zrect) == 4:
        wa = work_area_of_rect(zrect)
        if wa is not None:
            wl, wt, wr, wb = wa
            x = min(max(x, wl + margin), max(wl, wr - bar_w - margin))
            y = min(max(y, wt + margin), max(wt, wb - bar_h - margin))
    return x, y


def collapsed_dock_xy(zrect, handle_w, handle_h=HANDLE_H, margin=MARGIN):
    """
    收起把手的停靠位（纯计算，可独立单测）：贴所在显示器**屏幕底部边缘**、
    水平对齐 ZCode 窗口底部中心 x 附近（ZCode 底部中心减半把手宽），并钳制
    在工作区内（不盖任务栏、不出屏）。zrect 无效 / 工作区取不到返回 None。
    """
    if not zrect or len(zrect) != 4:
        return None
    wa = work_area_of_rect(zrect)
    if wa is None:
        return None
    wl, wt, wr, wb = wa
    if wr <= wl or wb <= wt:
        return None
    cx = (zrect[0] + zrect[2]) // 2
    x = min(max(cx - handle_w // 2, wl + margin),
            max(wl, wr - handle_w - margin))
    y = wb - handle_h - margin
    y = min(max(y, wt + margin), max(wt, wb - handle_h - margin))
    return x, y


# ---------------------------------------------------------------------------
# Win32 包装（懒加载，仅在 GUI 路径初始化；--once 不依赖 Windows API）
# ---------------------------------------------------------------------------

_WIN = None


def win():
    global _WIN
    if _WIN is None:
        _WIN = _WinApi()
    return _WIN


class _WinApi(object):
    def __init__(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # FindWindowW(None, 'ZCode') 精确标题
        user32.FindWindowW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        self.find_window = user32.FindWindowW

        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        self.get_window_rect = user32.GetWindowRect

        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        self.is_iconic = user32.IsIconic

        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        self.get_foreground_window = user32.GetForegroundWindow

        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.get_window_thread_process_id = user32.GetWindowThreadProcessId

        user32.MoveWindow.argtypes = [
            wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.BOOL,
        ]
        user32.MoveWindow.restype = wintypes.BOOL
        self.move_window = user32.MoveWindow

        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        self.show_window = user32.ShowWindow

        # 64 位：HWND_TOPMOST 必须以 c_void_p(-1) 传入
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        self.set_window_pos = user32.SetWindowPos

        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        self.get_parent = user32.GetParent

        # 64 位安全读写扩展样式（WS_EX_NOACTIVATE 用）。
        # 64 位进程用 *LongPtrW；老 32 位进程无该符号时退回 *LongW。
        try:
            glp = user32.GetWindowLongPtrW
            slp = user32.SetWindowLongPtrW
            glp.argtypes = [wintypes.HWND, ctypes.c_int]
            glp.restype = ctypes.c_ssize_t
            slp.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            slp.restype = ctypes.c_ssize_t
        except AttributeError:
            glp = user32.GetWindowLongW
            slp = user32.SetWindowLongW
            glp.argtypes = [wintypes.HWND, ctypes.c_int]
            glp.restype = ctypes.c_long
            slp.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            slp.restype = ctypes.c_long
        self.get_window_long = glp
        self.set_window_long = slp

        user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
        user32.MonitorFromRect.restype = wintypes.HMONITOR
        self.monitor_from_rect = user32.MonitorFromRect

        # MonitorFromPoint 是**按值**传 POINT（8 字节），argtypes 用结构体本身
        # （非指针），ctypes 自动按值转换——与 MonitorFromRect 的 LPCRECT 不同。
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        self.monitor_from_point = user32.MonitorFromPoint

        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        self.get_monitor_info = user32.GetMonitorInfoW

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD,
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.open_process = kernel32.OpenProcess
        self.get_exit_code_process = kernel32.GetExitCodeProcess
        self.close_handle = kernel32.CloseHandle
        self.query_full_process_image_name = kernel32.QueryFullProcessImageNameW


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def find_zcode_window():
    """定位 ZCode 主窗口句柄；失败返回 0。

    首选：FindWindowW(None, 'ZCode') 精确标题匹配（不子串，避免含 zcode
    的窗口误命中）。命不中时兜底：按进程 exe 名 == ZCode.exe 枚举顶层
    可见窗口（GetWindowThreadProcessId -> OpenProcess ->
    QueryFullProcessImageNameW 比对 exe 名），返回第一个匹配主窗口——
    覆盖用户 ZCode 标题是终端风格（如 "user@DESKTOP-..."）时找不到的
    场景。仍找不到返回 0（poll 据此隐藏小条）。
    """
    try:
        hwnd = win().find_window(None, u"ZCode") or 0
        if hwnd:
            return hwnd
    except Exception:
        hwnd = 0
    try:
        return _find_zcode_window_by_exe() or 0
    except Exception:
        return 0


def _find_zcode_window_by_exe():
    """按 exe 名枚举兜底：遍历顶层可见窗口，进程 exe 名含 zcode（小写）即
    返回其句柄；未找到返回 0（绝不抛——兜底失败等价找不到）。"""
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    result = {"hwnd": 0}

    def _enum_cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetParent(hwnd):
                return True   # 只取顶层窗口
            pid = pid_of(hwnd)
            if not pid:
                return True
            exe = process_exe_path(pid).replace("\\", "/").lower()
            if "zcode" in os.path.basename(exe):
                result["hwnd"] = hwnd
                return False   # 找到即停
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_enum_cb), 0)
    except Exception:
        return 0
    return result["hwnd"]


def window_rect_of(hwnd):
    """返回 (left, top, right, bottom) 或 None。"""
    try:
        rect = wintypes.RECT()
        if win().get_window_rect(hwnd, ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return None


def pid_of(hwnd):
    """窗口所属进程 pid；失败返回 None。"""
    try:
        pid = wintypes.DWORD()
        win().get_window_thread_process_id(hwnd, ctypes.byref(pid))
        return pid.value or None
    except Exception:
        return None


def process_alive(pid):
    """pid 进程是否存活。OpenProcess 失败视为死亡（可自愈陈旧 pid 文件）。"""
    if not pid:
        return False
    try:
        h = win().open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
        if not h:
            return False
        code = wintypes.DWORD()
        alive = False
        if win().get_exit_code_process(h, ctypes.byref(code)):
            alive = (code.value == STILL_ACTIVE)
        win().close_handle(h)
        return alive
    except Exception:
        return False


def process_exe_path(pid):
    """pid 进程 exe 绝对路径；失败返回 ''。"""
    if not pid:
        return ""
    try:
        h = win().open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(4096)
        size = wintypes.DWORD(len(buf))
        win().query_full_process_image_name(h, 0, buf, ctypes.byref(size))
        win().close_handle(h)
        return buf.value or ""
    except Exception:
        return ""


def _zcode_process_alive():
    """ZCode 进程是否存活：EnumWindows 遍历可见顶层窗口，按窗口所属进程
    exe 名小写含 "zcode"（非精确 exe 名、非窗口标题——用户终端标题可能是
    "user@DESKTOP-..." 等任意字符串）判定；命中任一即返回 True。

    OpenProcess 失败按「存活」处理（不误藏——收起态把手宁可多显示一拍，
    也不在 ZCode 真退出前被藏掉；进程表轮询下一拍自然会收敛）。
    任何异常返回 True（同样的不误藏原则），绝不抛。
    """
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = {"alive": False}

    def _enum_cb(hwnd, _lparam):
        try:
            if found["alive"]:
                return False
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetParent(hwnd):
                return True   # 只取顶层窗口
            pid = pid_of(hwnd)
            if not pid:
                return True
            exe = process_exe_path(pid).replace("\\", "/").lower()
            if "zcode" in exe:
                found["alive"] = True
                return False   # 找到即停
        except Exception:
            return True
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_enum_cb), 0)
    except Exception:
        return True
    return found["alive"]


def is_foreground_zcode():
    """前台窗口是否属于 ZCode 进程（pid 一致 或 exe 名含 zcode 模糊匹配）。"""
    try:
        hz = find_zcode_window()
        if not hz:
            return False
        zpid = pid_of(hz)
        fg = win().get_foreground_window()
        if not fg:
            return False
        fpid = pid_of(fg)
        if zpid and fpid and fpid == zpid:
            return True
        exe = process_exe_path(fpid).replace("\\", "/").lower()
        if "zcode" in os.path.basename(exe):
            return True
        return False
    except Exception:
        return False


def is_zcode_minimized():
    try:
        hz = find_zcode_window()
        if not hz:
            return False
        return bool(win().is_iconic(hz))
    except Exception:
        return False


def work_area_of_rect(zrect):
    """zrect 所在显示器工作区 (left,top,right,bottom)；失败返回 None。"""
    try:
        rect = wintypes.RECT(*zrect)
        hm = win().monitor_from_rect(ctypes.byref(rect), MONITOR_DEFAULTTONEAREST)
        if not hm:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if win().get_monitor_info(hm, ctypes.byref(info)):
            w = info.rcWork
            return (w.left, w.top, w.right, w.bottom)
    except Exception:
        pass
    return None


def _primary_work_area():
    """主显示器工作区 (left,top,right,bottom)；失败返回 None（0.2.2 收起把手
    回退停靠的最后兜底：小条自身显示器工作区取不到时退回主屏）。"""
    try:
        hm = win().monitor_from_point(wintypes.POINT(0, 0),
                                      MONITOR_DEFAULTTOPRIMARY)
        if not hm:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if win().get_monitor_info(hm, ctypes.byref(info)):
            w = info.rcWork
            return (w.left, w.top, w.right, w.bottom)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 手势状态机（收起把手的单击/双击/拖动裁决；纯逻辑层，可独立单测）
# ---------------------------------------------------------------------------
# 事件序列裁决规则（来自审查 P1，必须严格满足）：
#   ButtonPress-1   记录按下坐标 + 时间，无动作；若 250ms 单击待定未决
#                   （第二次按下到达）-> after_cancel 取消待定（防二次触发）。
#   B1-Motion       位移 > CLICK_MOVE_PX -> 拖动态 dragging=True，只跟随移动 +
#                   clamp_to_work_area（调用方），跳过单击/双击逻辑；
#                   未达阈值不触发。
#   ButtonRelease-1 拖动态 -> 结束拖动、on_persist 持久化把手坐标
#                   （handle_x/handle_y，save_config_keys 原子写）；
#                   双击第二次抬起 -> 不动作（收起态双击无意义）；
#                   单击抬起 -> after(CLICK_RECOGNIZE_MS) 排 250ms 待定展开，
#                   该时间内第二次按下到达则取消；单击只触发一次手势。
#   after 返回值必须保存并 after_cancel；不依赖 sleep/绝对时间锚定，
#   只依事件序列 + 250ms 相对延迟。

CLICK_RECOGNIZE_MS = 250   # 单击待定期（第二次按下到达则取消）
CLICK_MOVE_PX = 3          # 位移超过该像素视为拖动（>3px 才是拖动）

E_ACTION_NONE = "none"           # 无动作
E_ACTION_DRAG = "drag"           # 位移超阈值 -> 调用方跟随移动 + clamp
E_ACTION_PERSIST = "persist"     # 拖动释放 -> 调用方持久化 handle_x/y
E_ACTION_ARM_CLICK = "arm_click" # 单击抬起 -> 调用方排 250ms 待定展开


class GestureState(object):
    """收起把手的手势状态机（纯逻辑；gui 侧注入 after/after_cancel 与
    副作用回调，测试注入可控假调度器/记录回调）。"""

    def __init__(self, after=None, after_cancel=None, now=None,
                 on_expand=None, on_persist=None):
        self.dragging = False        # 拖动态（位移 >CLICK_MOVE_PX 后置 True）
        self.press_xy = None         # 最近一次按下（x_root, y_root）
        self.press_t = None          # 最近一次按下时间
        self.click_pending = False   # 250ms 单击待定中
        self.click_id = None         # root.after 返回值（after_cancel 用）
        self.in_double_press = False # press 已取消待定（第二次按下到达）
        self._after = after or (lambda ms, fn: None)
        self._after_cancel = after_cancel or (lambda i: None)
        self._now = now or (lambda: int(time.time() * 1000))
        self.on_expand = on_expand or (lambda: None)     # 单击确认 -> 展开
        self.on_persist = on_persist or (lambda x, y: None)  # 拖动结束 -> 存坐标

    def press(self, x_root, y_root):
        """ButtonPress-1：记录按下坐标 + 时间，返回 E_ACTION_NONE。
        若存在未决单击待定（250ms 内第二次按下）-> 取消待定、置
        in_double_press（双击的第二次抬起不再触发展开）。"""
        self.press_xy = (int(x_root), int(y_root))
        self.press_t = self._now()
        if self.click_pending:
            self._cancel_arm()
            self.in_double_press = True
        return E_ACTION_NONE

    def motion(self, ev):
        """B1-Motion：位移 >CLICK_MOVE_PX -> 进入拖动态并返回 E_ACTION_DRAG
        （调用方跟随移动 + clamp_to_work_area）；已处拖动态则持续跟随；
        未达阈值返回 E_ACTION_NONE（保持单击/双击待判）。"""
        if not self.press_xy:
            return E_ACTION_NONE
        if self.dragging:
            return E_ACTION_DRAG
        dx = ev["x_root"] - self.press_xy[0]
        dy = ev["y_root"] - self.press_xy[1]
        if dx * dx + dy * dy > CLICK_MOVE_PX * CLICK_MOVE_PX:
            self.dragging = True
            return E_ACTION_DRAG
        return E_ACTION_NONE

    def release(self, ev, cur_xy=None):
        """ButtonRelease-1（返回给调用方的裁决动作）：
        - 拖动态 -> 结束拖动，on_persist(cur_xy) 持久化坐标，E_ACTION_PERSIST；
        - 双击第二次抬起 -> 不动作（收起态双击无意义），E_ACTION_NONE；
        - 单击抬起 -> 排 250ms 待定（_arm_click），E_ACTION_ARM_CLICK。"""
        if self.dragging:
            self.dragging = False
            self.press_xy = None
            was_double = self.in_double_press
            self.in_double_press = False
            if cur_xy is not None:
                self.on_persist(cur_xy[0], cur_xy[1])
            return E_ACTION_PERSIST  # was_double 仅记录（拖动手感照常持久化）
        if self.in_double_press:
            self.in_double_press = False
            self.press_xy = None
            return E_ACTION_NONE
        self._arm_click()
        self.press_xy = None
        return E_ACTION_ARM_CLICK

    def _arm_click(self):
        self.click_pending = True
        try:
            self.click_id = self._after(CLICK_RECOGNIZE_MS, self._fire_click)
        except Exception:
            self.click_id = None

    def _fire_click(self):
        """250ms 待定到期：若仍 pending（未被第二次按下取消）才执行展开。"""
        if not self.click_pending:
            return
        self.click_pending = False
        self.click_id = None
        self.on_expand()

    def _cancel_arm(self):
        self.click_pending = False
        try:
            if self.click_id is not None:
                self._after_cancel(self.click_id)
        except Exception:
            pass
        self.click_id = None

    def cancel_click(self):
        """收起/展开切换时清理未决状态：取消单击待定 + 拖动态 + 双击态。"""
        self._cancel_arm()
        self.dragging = False
        self.in_double_press = False


# ---------------------------------------------------------------------------
# 一次性统计（--once）
# ---------------------------------------------------------------------------

def read_stats_once(data_dir, db_path, cfg=None):
    """读一次统计，返回 {'ok','line','source','sessionId','model','sessionLabel',
    'line1','speedTokPerSec','speedAvgTokPerSec','status','recentTurn','version',...}；
    任何异常不抛。line 按 cfg 的 show_* 裁剪（与旧第二行口径一致）。"""
    try:
        rows, _err = read_jsonl(os.path.join(data_dir, JSONL_NAME))
        info = resolve_gui_info(rows, data_dir, db_path, cfg=cfg, cur=None,
                                live_session_id=None)
        if info.get("stats") is not None:
            line = stats_to_text(info["stats"], cfg or dict(DEFAULT_CONFIG))
            if info.get("recent_note"):
                line = line + u" " + SESSION_RECENT_NOTE
        else:
            line = info.get("text")
        if line is None:
            line = STATS_PENDING
        st = info.get("stats") or {}
        # 0.6.0：状态徽标来源改为钩子时序 status-state.json（不再读 live_stream）
        status_state = (_read_status_state(data_dir)
                        if (cfg or dict(DEFAULT_CONFIG)).get("show_live", True)
                        else None)
        status, spd, turn_stats = resolve_turn_status(
            status_state, db_path, info.get("session_id"))
        return {
            "ok": True,
            "line": line,
            "source": info.get("source"),
            "sessionId": info.get("session_id"),
            "model": info.get("model"),
            "sessionLabel": info.get("session_label"),
            "line1": info.get("line1") or SESSION_UNKNOWN,
            "speedTokPerSec": st.get("speedTokPerSec"),
            "speedAvgTokPerSec": st.get("speedAvgTokPerSec"),
            "status": status,
            "recentTurn": turn_stats,
            "version": STATUSBAR_VERSION,
            "live": None,  # 0.6.0：live_stream 不再作为状态来源（钩子时序替代）
        }
    except Exception as e:
        return {"ok": False, "line": STATS_PENDING, "source": "error", "error": str(e)}


def _print_utf8_line(s):
    """强制 UTF-8 输出单行（避免 cp936 控制台编不出 ⏱/·）。"""
    try:
        sys.stdout.buffer.write((s + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        try:
            print(s)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GUI（tkinter 懒加载；仅在非 --once 路径使用）
# ---------------------------------------------------------------------------

def _pid_file(data_dir):
    return os.path.join(data_dir, PID_NAME)


def _ensure_single_instance(data_dir):
    """防多开：已有存活 pid -> SystemExit(0)；否则写入自身 pid。"""
    os.makedirs(data_dir, exist_ok=True)
    pidfile = _pid_file(data_dir)
    old = None
    try:
        with open(pidfile, "r", encoding="ascii") as f:
            old = f.read().strip()
    except Exception:
        pass
    if old:
        try:
            old = int(old)
        except Exception:
            old = None
        if old is not None and process_alive(old):
            raise SystemExit(0)
    tmp = pidfile + ".tmp"
    try:
        with open(tmp, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
        os.replace(tmp, pidfile)
    except Exception:
        pass


def _cleanup_pid(data_dir):
    pidfile = _pid_file(data_dir)
    try:
        with open(pidfile, "r", encoding="ascii") as f:
            cur = f.read().strip()
        if cur and int(cur) == os.getpid():
            os.remove(pidfile)
    except Exception:
        pass


def run_gui(data_dir, db_path, refresh_ms, cfg, config_path=None,
            interval_fixed=False):
    """interval_fixed=True 表示 refresh_ms 来自 --interval-ms 显式指定，
    此时配置热加载的 refresh_ms 变化不再覆盖它（CLI 显式参数优先）。"""
    import tkinter as tk  # 懒加载：GUI 路径才依赖桌面
    import tkinter.font as tkfont  # 指标块宽度按字体实测

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.attributes("-toolwindow", True)
    except Exception:
        pass
    root.configure(bg=BG)
    root.resizable(False, False)
    root.geometry("%dx%d+0+0" % (WINDOW_W, WINDOW_H))

    # ---- 单 Canvas 绘制层（彩色指标块风：圆角块/文字/进度条全画在 Canvas 上）----
    canvas = tk.Canvas(root, bg=BG, highlightthickness=0,
                       width=WINDOW_W, height=WINDOW_H)
    canvas.pack(fill="both", expand=True)

    # 字体实测对象（块宽按文本实测，不拍脑袋定宽）
    f_dim = tkfont.Font(root=root, font=FONT_DIM)
    f_main = tkfont.Font(root=root, font=FONT_MAIN)
    f_num = tkfont.Font(root=root, font=FONT_NUM)
    f_icon = tkfont.Font(root=root, font=ICON_FONT)

    # ---- 右键菜单 ----
    menu = tk.Menu(root, tearoff=0, bd=0, bg=MENU_BG, fg=FG,
                   activebackground=MENU_ACTIVE, activeforeground=FG)
    menu.add_command(label=u"\u91cd\u65b0\u8d34\u8fb9", command=lambda: re_dock())
    menu.add_command(label=u"\u6536\u8d77\u5230\u8fb9\u7f18",
                     command=lambda: collapse_bar())
    menu.add_separator()

    # 「显示项」子菜单：每个 show_* 一项，checkbutton 勾选态绑定当前配置；
    # 点击即切换 -> 立即重画 -> 原子写回 statusbar-config.json（失败只记 err 日志）。
    show_vars = {}
    display_menu = tk.Menu(menu, tearoff=0, bd=0, bg=MENU_BG, fg=FG,
                           activebackground=MENU_ACTIVE, activeforeground=FG)
    for _lbl, _key in SHOW_MENU_ITEMS:
        _var = tk.BooleanVar(value=bool(cfg.get(_key, False)))
        show_vars[_key] = _var
        display_menu.add_checkbutton(
            label=_lbl, variable=_var,
            command=(lambda k: lambda: toggle_show(k))(_key))
    menu.add_cascade(label=u"\u663e\u793a\u9879", menu=display_menu)
    menu.add_separator()
    menu.add_command(label=u"\u9000\u51fa statusbar", command=lambda: quit_app())

    def on_right_click(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # 右键菜单绑到 Canvas（全区域可呼出）
    canvas.bind("<Button-3>", on_right_click)

    def quit_app():
        try:
            root.destroy()
        except Exception:
            pass
        _cleanup_pid(data_dir)
        sys.exit(0)

    def on_close():
        quit_app()

    # close「×」小块改由 Canvas 绘制并按 tag 绑定（见 _draw_close）
    root.protocol("WM_DELETE_WINDOW", on_close)

    try:
        root.update_idletasks()
    except Exception:
        pass

    # 取窗口原生 hwnd：tk 顶层由 winfo_id 的父级承载（已核实）
    try:
        wid = root.winfo_id()
        hwnd = win().get_parent(wid) or wid
    except Exception:
        hwnd = None

    if hwnd:
        # 不抢焦点：加 WS_EX_NOACTIVATE，鼠标点击小条不会把前台抢成
        # pythonw（否则前台判定误判 -> 小条自隐藏/拖动被打断）。
        try:
            style = win().get_window_long(hwnd, GWL_EXSTYLE)
            win().set_window_long(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
        except Exception:
            pass
        try:
            win().set_window_pos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )
        except Exception:
            pass

    state = {
        "shown": False,
        "last_xy": None,          # 最近一次贴边 MoveWindow 的目标（仅贴边用）
        "last_info": None,
        "db_read_count": 0,
        "cur_w": WINDOW_W,        # 当前生效窗口宽（自适应 + 防抖后的值）
        # ---- 配置热加载 ----
        "cfg_mtime": None,        # statusbar-config.json 上次读取的 mtime
        "refresh_ms": refresh_ms, # 当前生效刷新间隔（热加载可更新）
        # ---- 拖动状态（与贴边 last_xy 互相独立，互不污染）----
        "dragging": False,        # 正在拖动（按下->释放）
        "drag_offset": None,      # (x_root - winfo_rootx, y_root - winfo_rooty)
        "manual_position": False, # 拖动过 -> poll 跳过吸回
        "manual_xy": None,        # 拖动后的稳定位置（供 re_dock 前保持）
        "close_box": None,        # close 小块的 Canvas 坐标（拖动按下时排除）
        # ---- 靠边收起（collapsed 把手）----
        "collapsed": bool(cfg.get("collapsed", False)),  # 启动按配置进收起/展开态
        "manual_handle": False,   # 收起态把手被手动拖过 -> poll 不再吸回（直到展开）
        "press_xy": None,         # 完整态按下时指针屏幕坐标（区分「单击」与「拖动」）
        "last_drag_xy": None,     # 收起态拖动最近一次被 clamp 后的目标坐标
        "hover_grace_until": 0,   # 收起冷却截止（epoch 毫秒）：此之前悬停展开不排程
        "ignore_click_until": 0,  # 双击尾巴遮蔽截止（epoch 毫秒）：此之前收起态
                                  # 释放事件不喂手势机（防收起后残余 release 又展开）
        "handle_armed": False,    # 悬停展开武装标志：需 leave->enter 才重新武装
    }
    hover_expand_id = None        # 把手悬停自动展开的 after 计时器（cancel 用）

    # ---- 收起把手手势状态机（单击/双击/拖动裁决）----
    # 注入 GUI 的 after / after_cancel / 副作用回调；函数体在调用期解析
    # （expand_bar 等定义在下方，闭包捕获变量名即可）。

    def _gesture_after(ms, fn):
        def _wrapped():
            try:
                fn()
            except Exception:
                try:
                    _log_err(data_dir, "handle gesture callback error:\n%s"
                             % traceback.format_exc())
                except Exception:
                    pass
        try:
            return root.after(ms, _wrapped)
        except Exception:
            return None

    def _gesture_click_expand():
        """单击确认展开（手势状态机 _fire_click -> on_expand）：source
        =="click"。**不受收起冷却窗拦截**——单击是用户主动明确意图，收起
        冷却窗内点一下立即展开（0.1.6 把点击来源一并拦掉导致「小把手点
        不开」）。防自恢复不改由手势机侧兜住：双击第二次抬起
        （in_double_press）不武装单击待定，残余 release 不再走 click 路径。"""
        try:
            expand_bar(source="click")
        except Exception:
            try:
                _log_err(data_dir, "handle gesture expand error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    def _gesture_persist(x, y):
        # 拖动结束 -> 持久化把手坐标（原子写），poll 不再吸回直到展开
        cfg["handle_x"] = int(x)
        cfg["handle_y"] = int(y)
        state["manual_handle"] = True
        save_config_keys(config_path, cfg, data_dir, ["handle_x", "handle_y"])
        _sync_cfg_mtime()

    hand_gesture = GestureState(
        after=_gesture_after,
        after_cancel=lambda i: root.after_cancel(i),
        now=time_ms,
        on_expand=_gesture_click_expand,
        on_persist=_gesture_persist,
    )

    # 记录配置文件初始 mtime（热加载基线；文件暂不存在为 None）
    try:
        state["cfg_mtime"] = os.path.getmtime(config_path)
    except Exception:
        state["cfg_mtime"] = None

    def _after(ms, fn):
        """root.after 包装：回调异常写 err 日志后继续，绝不断刷新/轮询循环。"""
        def _wrapped():
            try:
                fn()
            except Exception:
                try:
                    _log_err(data_dir, "statusbar after-callback error:\n%s"
                             % traceback.format_exc())
                except Exception:
                    pass
        try:
            root.after(ms, _wrapped)
        except Exception:
            pass

    # ---- tooltip（单一全局 toplevel 复用，跟随鼠标）----
    tip = None
    tip_after_id = None

    def _ensure_tip():
        nonlocal tip
        if tip is None:
            try:
                tip = tk.Toplevel(root)
                tip.withdraw()
                tip.overrideredirect(True)
                try:
                    tip.attributes("-topmost", True)
                except Exception:
                    pass
                tip.configure(bg=BG_SECOND)
                tip_lbl = tk.Label(
                    tip, text="", bg=BG_SECOND, fg=FG, font=FONT_DIM,
                    anchor="w", justify="left",
                    padx=8, pady=5, wraplength=380,
                )
                tip_lbl.pack()
                tip.tip_lbl = tip_lbl
            except Exception:
                tip = None
        return tip

    def _tip_visible():
        try:
            return tip is not None and tip.winfo_ismapped()
        except Exception:
            return False

    def show_tooltip(text, x, y):
        """在 (x, y) 附近显示 tooltip（按小条所在显示器工作区夹紧；失败退回主屏）。"""
        t = _ensure_tip()
        if t is None:
            return
        try:
            t.tip_lbl.config(text=text)
            t.update_idletasks()
            tw = t.winfo_reqwidth()
            th = t.winfo_reqheight()
            wl = wt = 0
            wr = t.winfo_screenwidth()
            wb = t.winfo_screenheight()
            if hwnd:
                rect = window_rect_of(hwnd)
                if rect:
                    wa = work_area_of_rect(rect)
                    if wa:
                        wl, wt, wr, wb = wa
            tx = min(x + 12, max(wl, wr - tw - 4))
            ty = min(y + 14, max(wt, wb - th - 4))
            t.geometry("+%d+%d" % (tx, ty))
            t.deiconify()
            t.lift()
        except Exception:
            pass

    def hide_tooltip():
        try:
            if tip is not None:
                tip.withdraw()
        except Exception:
            pass

    def tooltip_enter(text):
        """悬停进入：延时 ~400ms 后显示（避免乱闪）。"""
        nonlocal tip_after_id
        try:
            if tip_after_id is not None:
                root.after_cancel(tip_after_id)
        except Exception:
            pass
        try:
            tip_after_id = root.after(400, lambda: _show_tip_at_pointer(text))
        except Exception:
            tip_after_id = None

    def _show_tip_at_pointer(text):
        try:
            show_tooltip(text, root.winfo_pointerx(), root.winfo_pointery())
        except Exception:
            pass

    def tooltip_leave():
        """悬停离开：立即隐藏并取消延时任务。"""
        nonlocal tip_after_id
        try:
            if tip_after_id is not None:
                root.after_cancel(tip_after_id)
                tip_after_id = None
        except Exception:
            pass
        hide_tooltip()

    def bind_hover(tag, tip_text, rect_id=None, base_fill=None, hover_fill=None):
        """Canvas tag 级悬停：块背景高亮 + tooltip（延时显示/跟随/离开隐藏）。"""
        def enter(_e):
            if rect_id is not None and hover_fill is not None:
                try:
                    canvas.itemconfigure(rect_id, fill=hover_fill)
                except Exception:
                    pass
            if tip_text:
                tooltip_enter(tip_text)

        def leave(_e):
            if rect_id is not None and base_fill is not None:
                try:
                    canvas.itemconfigure(rect_id, fill=base_fill)
                except Exception:
                    pass
            tooltip_leave()

        def motion(e):
            if tip_text and _tip_visible():
                show_tooltip(tip_text, e.x_root, e.y_root)

        canvas.tag_bind(tag, "<Enter>", enter)
        canvas.tag_bind(tag, "<Leave>", leave)
        canvas.tag_bind(tag, "<Motion>", motion)

    def _draw_close(win_w):
        """右上「×」close 小块（圆角底 + hover 红 + 点击退出；每次重画时重建）。
        win_w 为本帧生效窗口宽（自适应）。"""
        cw, ch = 24, 16
        x1 = win_w - 10
        x0 = x1 - cw
        y0, y1 = 4, 4 + ch
        rect = round_rect(canvas, x0, y0, x1, y1, 5, fill=BG_SECOND, outline="")
        ctxt = canvas.create_text((x0 + x1) / 2.0, (y0 + y1) / 2.0,
                                  text=u"\u00d7", font=FONT_CLOSE,
                                  fill=FG_DIM, tags=("close",))
        state["close_box"] = (x0, y0, x1, y1)

        def enter(_e):
            try:
                canvas.itemconfigure(rect, fill=CLOSE_HOVER_BG)
                canvas.itemconfigure(ctxt, fill=CLOSE_HOVER_FG)
            except Exception:
                pass

        def leave(_e):
            try:
                canvas.itemconfigure(rect, fill=BG_SECOND)
                canvas.itemconfigure(ctxt, fill=FG_DIM)
            except Exception:
                pass

        canvas.tag_bind("close", "<Enter>", enter)
        canvas.tag_bind("close", "<Leave>", leave)

        def on_click(_e):
            quit_app()
            return "break"   # 阻断 Canvas 级拖动绑定

        canvas.tag_bind("close", "<Button-1>", on_click)

    def _avail_work_w():
        """窗口宽上限 = 所在显示器工作区宽 - 16px；取不到工作区退回屏幕宽。"""
        try:
            if hwnd:
                rect = window_rect_of(hwnd)
                if rect:
                    wa = work_area_of_rect(rect)
                    if wa and wa[2] > wa[0]:
                        return (wa[2] - wa[0]) - 16
        except Exception:
            pass
        try:
            return root.winfo_screenwidth() - 16
        except Exception:
            return WINDOW_W

    def _apply_width(new_w):
        """窗口宽自适应落地（带 ±WIDTH_HYSTERESIS 防抖）：
        - 变化小于阈值 -> 保持当前宽（数字位数跳动不引起窗口频繁缩放闪烁）；
        - cur_w 为 None（收起<->展开刚切换）-> 跳过防抖强制生效；
        - 拖动中 -> 不改尺寸（避免干扰拖动；释放后下一拍渲染补上）；
        - 生效时只改宽度、保持左上角不动（贴边模式 poll 下一拍按新宽重新居中，
          手动定位模式位置完全不受影响）。
        返回本帧实际生效宽度（防抖后可能与 new_w 不同，渲染以返回值为准）。"""
        cur = state.get("cur_w")
        if cur is not None and abs(new_w - cur) < WIDTH_HYSTERESIS:
            return cur
        if state.get("dragging"):
            return (cur if cur is not None else new_w)
        state["cur_w"] = new_w
        try:
            canvas.config(width=new_w)
        except Exception:
            pass
        try:
            if hwnd:
                xy = current_window_xy()
                if xy:
                    win().move_window(hwnd, xy[0], xy[1], new_w, WINDOW_H, True)
            else:
                root.geometry("%dx%d+0+0" % (new_w, WINDOW_H))
        except Exception:
            pass
        return new_w

    # ---- 靠边收起：小把手（collapsed 模式）----
    # 状态机：state["collapsed"] 单一事实源；收起入口 = 双击状态条任意区域 /
    # 右键「收起到边缘」；展开 = 把手悬停 ~0.5s（Move 刷新计时）或单击；
    # 切换即原子写回配置（save_config_keys），重启按 collapsed 字段恢复。

    def _cancel_hover_expand():
        """取消把手悬停自动展开的计时器（离开把手 / 已展开时调用）。"""
        nonlocal hover_expand_id
        try:
            if hover_expand_id is not None:
                root.after_cancel(hover_expand_id)
        except Exception:
            pass
        hover_expand_id = None

    def _schedule_hover_expand():
        """（重新）启动悬停 0.5s 自动展开计时（Move 事件刷新 = 重置计时）。

        守卫（互斥清晰）：
          - 收起冷却期内（collapse_bar 后 COLLAPSE_HOVER_GRACE_MS 内）不排程
            —— 防双击收起后把手恰在指针下的立即自展开；
          - 未武装（handle_armed=False，收起瞬间被 disarm）不排程 ——
            悬停展开要求指针先离开把手再重新进入（leave->enter）才再次武装；
          - 手势按压/拖动进行中不排悬停展开（按下即取消悬停，避免悬停展开与
            单击/双击/拖动裁决冲突）。
        排程成功后 consume 武装（handle_armed=False），一次 leave->enter 只武装一次。"""
        nonlocal hover_expand_id
        if time_ms() < state.get("hover_grace_until", 0):
            return
        if not state.get("handle_armed", False):
            return
        if hand_gesture.press_xy is not None or hand_gesture.dragging:
            return
        state["handle_armed"] = False   # 武装只消费一次
        _cancel_hover_expand()
        try:
            hover_expand_id = root.after(HANDLE_HOVER_MS,
                                         lambda: expand_bar(source=None))
        except Exception:
            hover_expand_id = None

    def _sync_cfg_mtime():
        """写回配置后同步热加载基线 mtime，避免下一拍重读同值文件。"""
        try:
            state["cfg_mtime"] = os.path.getmtime(config_path)
        except Exception:
            pass

    def collapse_bar():
        """收起：完整状态条 -> 小把手。清完整态手动定位/清理把手手势待定，
        持久化 collapsed=true，立即以把手尺寸重画。把手停靠位由 poll 决定：
        无记忆 -> 工作区底部居中；有记忆 -> 记忆位置（越界回退右下角）。"""
        if state.get("collapsed"):
            return
        state["collapsed"] = True
        cfg["collapsed"] = True
        save_config_keys(config_path, cfg, data_dir, ["collapsed"])
        _sync_cfg_mtime()
        hide_tooltip()
        _cancel_hover_expand()
        hand_gesture.cancel_click()          # 收起瞬间清理未决单击待定/拖动态
        # 双击尾巴遮蔽：双击最后一次抬起的残余释放事件若在收起后仍到达手势机
        # （此时 cancel_click 已把 in_double_press 清为 False，release 会误走
        # _arm_click -> 250ms 后 click 路径展开，把刚收起的条又拉回完整态），
        # 这里置遮蔽窗：窗口内下次 release 不再喂手势机，双击尾巴被吃掉。
        state["ignore_click_until"] = time_ms() + DOUBLE_CLICK_TAIL_MS
        # 防自恢复：收起后进入悬停展开冷却窗，并把把手 disarm —— 把手恰在
        # 指针下也不会 500ms 后自展开；需 leave->enter 且过冷却后才恢复悬停展开。
        state["hover_grace_until"] = time_ms() + COLLAPSE_HOVER_GRACE_MS
        state["handle_armed"] = False
        # 完整态默认贴边（manual_position 是完整态拖动记忆；收起态不沿用）
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["close_box"] = None
        try:
            render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(data_dir, "render after collapse error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    def expand_bar(source=None):
        """展开：把手 -> 完整状态条（取简单：重新贴边 ZCode 底部，不还原
        收起前的手动位置）。持久化 collapsed=false，清掉把手手动记忆（下次
        收起按记忆位置重新出现；本次展开后把手坐标记忆保留在配置文件）。
        恢复完整条高度并强制宽度生效（cur_w=None 绕过防抖）。

        source 区分展开路径（冷却守卫对点击来源豁免）：
          - 悬停计时（默认/None）与右键菜单（"menu"）：仍受收起冷却守卫——
            刚收起 COLLAPSE_HOVER_GRACE_MS 内不展开，避免双击收起的同桌
            手势（残留轻按把手）+ 把手恰在指针下 500ms 悬停自展开，把
            状态条又拉回完整态（防自恢复）。
          - 单击确认（_gesture_click_expand 经手势状态机 _fire_click ->
            on_expand 传入 "click"）：**不受冷却拦截**——单击是用户主动明确
            意图，收起冷却窗内点一下必须立即展开（0.1.6 把点击来源也一并
            拦掉导致「收起后小把手怎么点都没反应」）；防自恢复的双击尾巴
            由手势机侧守卫兜住：双击第二次抬起（in_double_press）不会
            武装单击待定，残余 release 落在把手上也不再走 click 路径展开。"""
        if not state.get("collapsed"):
            return
        if source != "click" and time_ms() < state.get("hover_grace_until", 0):
            return  # 非点击来源在收起冷却窗内不展开（悬停/菜单防自恢复）
        state["collapsed"] = False
        cfg["collapsed"] = False
        save_config_keys(config_path, cfg, data_dir, ["collapsed"])
        _sync_cfg_mtime()
        hide_tooltip()
        _cancel_hover_expand()
        hand_gesture.cancel_click()
        state["ignore_click_until"] = 0   # 展开后清双击尾巴遮蔽（防残留遮蔽正常点击）
        state["manual_handle"] = False   # 展开后清标志：下次收起重新按记忆位置
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["hover_grace_until"] = 0   # 展开后清冷却：本次会话下次收起重新计时
        state["handle_armed"] = False    # 展开后 disarm：下次收起仍需 leave->enter
        state["cur_w"] = None      # 绕过宽度防抖：72 -> ~620 必须立刻生效
        try:
            canvas.config(height=WINDOW_H)  # 恢复完整条高度（把手态是 HANDLE_H）
        except Exception:
            pass
        try:
            render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(data_dir, "render after expand error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    def _apply_handle_geometry(new_w):
        """收起把手尺寸落地：cur_w 直接生效（把手宽稳定，无需防抖），窗口
        高度切到 HANDLE_H；位置本函数不动（poll 下一拍按 collapsed 停靠位
        贴屏幕底部），仅保持当前左上角。"""
        state["cur_w"] = new_w
        try:
            canvas.config(width=new_w, height=HANDLE_H)
        except Exception:
            pass
        try:
            if hwnd:
                xy = current_window_xy()
                if xy:
                    win().move_window(hwnd, xy[0], xy[1], new_w, HANDLE_H, True)
            else:
                root.geometry("%dx%d+0+0" % (new_w, HANDLE_H))
        except Exception:
            pass

    def _render_handle(info):
        """收起态渲染（render_ui 的 collapsed 分支调用；canvas 已清空）：
        ~72x18 深底小把手 + 顶部 1px 分隔线 + 「◐ 84.9%」浓缩缓存命中率
        （绿字）；悬停 0.5s / 单击展开（单击经 drag_stop 位移判定走 expand）。"""
        stats = (info or {}).get("stats")
        hit_txt = (u"%.1f%%" % _hit_rate(stats)) if stats else u"\u2014"
        w = BLOCK_PAD * 2 + f_icon.measure(ICON_HIT) + 5 + f_num.measure(hit_txt)
        _apply_handle_geometry(w)
        canvas.create_rectangle(0, 0, w, 1, fill=EDGE_LINE, outline="")
        tx = BLOCK_PAD
        cy = HANDLE_H / 2.0
        canvas.create_text(tx, cy, text=ICON_HIT, font=ICON_FONT,
                           fill=ACCENT_GREEN, anchor="w", tags=("hdl",))
        tx += f_icon.measure(ICON_HIT) + 5
        canvas.create_text(tx, cy, text=hit_txt, font=FONT_NUM,
                           fill=ACCENT_GREEN, anchor="w", tags=("hdl",))

        def _enter(_e):
            # 仅当已武装（leave->enter 后）才排悬停展开；arm 事件会立刻 consume，
            # 冷却期内的 enter 因冷却守卫不排程，小幅度移动不重复武装。
            if state.get("handle_armed", False):
                _schedule_hover_expand()      # 悬停 0.5s 自动展开
            tooltip_enter(HANDLE_TIP)         # 「已收起——悬停或单击展开」

        def _motion(e):
            if state.get("handle_armed", False):
                _schedule_hover_expand()      # Move 刷新（重置）展开计时
            if _tip_visible():
                show_tooltip(HANDLE_TIP, e.x_root, e.y_root)

        def _leave(_e):
            _cancel_hover_expand()
            state["handle_armed"] = True      # 离开把手 -> 重新武装（下次进入可展开）
            tooltip_leave()

        canvas.tag_bind("hdl", "<Enter>", _enter)
        canvas.tag_bind("hdl", "<Motion>", _motion)
        canvas.tag_bind("hdl", "<Leave>", _leave)

    def render_ui(info):
        """整幅重画（0.4.0 对话级三区：同一回调内 delete+create，Tk 单次刷帧
        无闪烁）：顶部 1px 分隔线 + 第一行（状态徽标 + 对话名）+ 第二行
        （本轮统计）+ 右侧小字（会话累计）+ close 小块。
        窗口宽按内容自适应（plan_statusbar_layout_3zone）：状态徽标 / 本轮
        统计 / 累计小字**永不因宽度裁剪**，超上限时依次收对话名 -> 缩间距 ->
        省累计标注。"""
        canvas.delete("all")
        if state.get("collapsed"):
            # 收起态：只画底部小把手（◐ 命中率），几何走 HANDLE_H 分支
            _render_handle(info)
            return
        show_status = cfg.get("show_status", True)
        show_turn = cfg.get("show_recent_turn", True)
        show_cum = cfg.get("show_cumulative", True)
        status = info.get("status") or "idle"
        speed = info.get("status_speed")
        model = info.get("model")
        label = info.get("session_label")
        turn = info.get("turn_stats")
        cum = info.get("stats")

        _fmap = {FONT_MAIN: f_main, FONT_NUM: f_num, FONT_DIM: f_dim,
                 ICON_FONT: f_icon}

        def _turn_segments(ts):
            """本轮统计分段（(text, font, fg) 列表）；无数据返回单段占位。
            0.5.0：生成中时无论 speed 是否有值，尾部追加实时速度或占位文案
            「生成中…」，确保首 token 前用户也看到生成中状态。"""
            if not ts:
                segs = [(TURN_PENDING, FONT_MAIN, FG_DIM)]
            else:
                inp = int(ts.get("inputTokens") or 0)
                cache_rd = int(ts.get("cacheReadTokens") or 0)
                hit = (cache_rd / float(inp) * 100.0) if inp > 0 else 0.0
                segs = [
                    (ICON_DUR, ICON_FONT, FG_DIM),
                    (u"%.1fs" % ((ts.get("durationMs") or 0) / 1000.0),
                     FONT_NUM, FG),
                    (u" \u00b7 in ", FONT_MAIN, FG_DIM),
                    (format_tokens(inp), FONT_NUM, FG),
                    (u" \u00b7 out ", FONT_MAIN, FG_DIM),
                    (format_tokens(ts.get("outputTokens") or 0), FONT_NUM, FG),
                    (u" \u00b7 cache hit ", FONT_MAIN, FG_DIM),
                    (u"%.1f%%" % hit, FONT_NUM, FG),
                ]
            # 0.5.0：生成中时追加实时速度或占位文案（speed None 则显示「生成中…」）
            if cfg.get("show_live", True) and status == "generating":
                if speed is not None:
                    segs.append((u" \u00b7 \u26a1", FONT_MAIN, FG_DIM))
                    segs.append((u"%.1f tok/s" % speed, FONT_DIM, FG_DIM))
                else:
                    segs.append((u" \u00b7 生成中\u2026", FONT_MAIN, ACCENT_GREEN))
            return segs

        def _draw_turn_stats(x, y, ts):
            """第二行：本轮统计分段绘制（数字等宽防跳字）+ 整行悬停 tooltip。"""
            segs = _turn_segments(ts)
            for t, f, c in segs:
                canvas.create_text(x, y, text=t, font=f, fill=c,
                                   anchor="w", tags=("m_turn",))
                x += _fmap[f].measure(t)
            bind_hover("m_turn", TIP_TURN)

        # ---- 文本拼装 ----
        badge_txt = STATUS_TEXT.get(status, STATUS_TEXT["idle"])
        # 0.4.3：tok/s 不再放第一行徽标旁（0.4.2 曾在此以小字灰字显示，用户
        # 反馈太弱找不到），改到第二行本轮统计尾部（见 _turn_segments）。
        turn_segs = _turn_segments(turn)
        turn_w = sum(_fmap[f].measure(t) for t, f, _ in turn_segs)
        cum_txt = cumulative_text(cum)
        has_note = bool(cum and info.get("recent_note"))

        # ---- 先实测、后布局（三区宽度全部 tkinter.font 实测）----
        badge_w = 0
        if show_status:
            badge_w = BADGE_PAD_X * 2 + f_main.measure(badge_txt)
        cum_w = f_dim.measure(cum_txt) if (show_cum and cum_txt) else 0
        note_w = f_dim.measure(SESSION_RECENT_NOTE) if has_note else 0
        # 模型名并入不可裁槽（与徽标同槽：徽标+模型名恒不裁，对话名按剩余宽截短）
        if cfg.get("show_model", True) and model:
            badge_w += f_dim.measure(_truncate(model, 20) or "model?") + 8

        def _title_w(lm):
            if not label:
                return 0
            return f_dim.measure(_truncate(label, lm) or u"")

        plan = plan_statusbar_layout_3zone(badge_w, _title_w, turn_w, cum_w,
                                           note_w, _avail_work_w())
        win_w = _apply_width(plan["win_w"])
        badge_gap = plan["badge_gap"]
        show_cum_now = bool(plan["show_cum"] and cum_txt)
        show_note_now = bool(plan["show_note"] and has_note)

        # 顶部 1px 分隔线（提质感）
        canvas.create_rectangle(0, 0, win_w, 1, fill=EDGE_LINE, outline="")

        # ---- 第一行：状态徽标（色块 + 深色粗体字）+ 模型名（蓝）+ 对话名 ----
        x = 12
        if show_status:
            color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
            badge_only_w = BADGE_PAD_X * 2 + f_main.measure(badge_txt)
            rect = round_rect(canvas, x, ROW1_CY - BADGE_H / 2.0,
                              x + badge_only_w, ROW1_CY + BADGE_H / 2.0, 9,
                              fill=color, outline="")
            canvas.create_text(x + badge_only_w / 2.0, ROW1_CY, text=badge_txt,
                               font=BADGE_FONT, fill=BADGE_FG,
                               anchor="center", tags=("m_badge",))
            bind_hover("m_badge",
                       STATUS_TIPS.get(status, STATUS_TIPS["idle"]),
                       rect, color, color)
            x += badge_only_w
            x += badge_gap
        if cfg.get("show_model", True) and model:
            mtxt = _truncate(model, 20) or "model?"
            canvas.create_text(x, ROW1_CY, text=mtxt, font=FONT_DIM,
                               fill=ACCENT_BLUE, anchor="w", tags=("m_model",))
            x += f_dim.measure(mtxt) + 8
            bind_hover("m_model", u"模型：%s" % model)
        if label:
            canvas.create_text(x, ROW1_CY,
                               text=_truncate(label, plan["label_max"])
                               or u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09",
                               font=FONT_DIM, fill=FG_DIM, anchor="w",
                               tags=("m_sess",))
            tip = u"\u4f1a\u8bdd\uff1a%s" % label
            if model:
                tip = tip + u"\n\u6a21\u578b\uff1a%s" % model
            bind_hover("m_sess", tip)
        elif not show_status:
            # fail-closed：徽标也关掉且无对话名 -> 会话未识别占位
            canvas.create_text(12, ROW1_CY, text=SESSION_UNKNOWN,
                               font=FONT_DIM, fill=FG_DIM, anchor="w")

        # ---- 右上 close 小块 ----
        _draw_close(win_w)

        # ---- 第二行：本轮统计（左侧）+ 会话累计小字（右侧）----
        if show_turn:
            _draw_turn_stats(10, ROW2_TURN_Y, turn)
        if show_cum_now:
            # 累计小字右对齐（close 小块左侧留白），jsonl 兜底判定时左侧
            # 附「（最近会话累计）」标注（超宽时布局先省略标注，不裁统计）。
            cx = win_w - CLOSE_RESERVE_W - 6
            canvas.create_text(cx, ROW2_TURN_Y, text=cum_txt, font=FONT_DIM,
                               fill=FG_DIM, anchor="e", tags=("m_cum",))
            bind_hover("m_cum", TIP_CUM)
            if show_note_now:
                canvas.create_text(cx - f_dim.measure(cum_txt) - 6,
                                   ROW2_TURN_Y, text=SESSION_RECENT_NOTE,
                                   font=FONT_DIM, fill=FG_DIM, anchor="e",
                                   tags=("m_cum_note",))

    def current_window_xy():
        """取小条当前屏幕坐标 (x, y)；失败返回 None。"""
        try:
            if not hwnd:
                return None
            wid = root.winfo_id()
            child_hwnd = win().get_parent(wid) or wid
            if not child_hwnd:
                return None
            rect = wintypes.RECT()
            if win().get_window_rect(child_hwnd, ctypes.byref(rect)):
                return rect.left, rect.top
        except Exception:
            pass
        return None

    def _default_dock_xy(bar_w):
        """坐标/工作区瞬时不可得时的回退停靠（0.2.2）：优先小条自身所在
        显示器工作区（get_window_rect 直取顶层句柄——与 current_window_xy
        的 child 解析失败场景互补），再退主显示器（_primary_work_area）；
        仍失败返回 None。定位用生产 default_handle_xy（工作区底部居中）。"""
        wa = None
        try:
            rect = wintypes.RECT()
            if hwnd and win().get_window_rect(hwnd, ctypes.byref(rect)):
                wa = work_area_of_rect((rect.left, rect.top,
                                        rect.right, rect.bottom))
        except Exception:
            wa = None
        if wa is None:
            wa = _primary_work_area()
        if wa is None:
            return None
        return default_handle_xy(wa, bar_w)

    def drag_start(event):
        """按下：完整态（未收起）延用偏移拖动；收起态走手势状态机 press
        （记录坐标+时间，无动作；250ms 内第二次按下到达会取消单击待定）。
        close 小块上的按下走退出逻辑，不进入任何拖动。"""
        if state.get("dragging") or hand_gesture.dragging:
            return
        # close 小块上的按下走退出逻辑（把手态无 close，恒 None）
        cb = state.get("close_box")
        if cb and cb[0] <= event.x <= cb[2] and cb[1] <= event.y <= cb[3]:
            return
        if state.get("collapsed"):
            _cancel_hover_expand()   # 按下即取消悬停展开，交手势裁决
            hand_gesture.press(event.x_root, event.y_root)
            return
        xy = current_window_xy()
        if xy is None:
            return
        state["dragging"] = True
        state["press_xy"] = (event.x_root, event.y_root)
        state["drag_offset"] = (event.x_root - xy[0], event.y_root - xy[1])

    def drag_move(event):
        """按住左键移动：
        - 收起态 -> 手势状态机裁决：位移 >3px 才进入拖动态跟随移动
          （仍 clamp_to_work_area；未达阈值不触发，保持单击/双击待判）；
        - 完整态 -> 原偏移拖动跟随（不受手势状态机约束）。"""
        if state.get("collapsed"):
            act = hand_gesture.motion({"x_root": event.x_root,
                                       "y_root": event.y_root})
            if act != E_ACTION_DRAG:
                return
            # 拖动态：目标坐标 clamp_to_work_area（以目标位置构造伪矩形
            # 定显示器），防止拖出屏幕无法自救，移动后记录把手位置。
            xy = current_window_xy()
            if xy is None:
                return
            ox = event.x_root - xy[0]
            oy = event.y_root - xy[1]
            new_x = event.x_root - ox
            new_y = event.y_root - oy
            bar_w = state.get("cur_w") or HANDLE_W_DEFAULT
            bar_h = HANDLE_H
            pseudo = (new_x, new_y, new_x + bar_w, new_y + bar_h)
            clamped = clamp_to_work_area((new_x, new_y), bar_w, bar_h, pseudo)
            if clamped:
                new_x, new_y = clamped
            try:
                if hwnd:
                    win().move_window(hwnd, new_x, new_y, bar_w, bar_h, True)
            except Exception:
                return
            state["last_drag_xy"] = (new_x, new_y)
            return
        if not state.get("dragging"):
            return
        off = state.get("drag_offset")
        if not off:
            return
        ox, oy = off
        new_x = event.x_root - ox
        new_y = event.y_root - oy
        bar_w = state.get("cur_w") or WINDOW_W
        bar_h = WINDOW_H
        pseudo = (new_x, new_y, new_x + bar_w, new_y + bar_h)
        clamped = clamp_to_work_area((new_x, new_y), bar_w, bar_h, pseudo)
        if clamped:
            new_x, new_y = clamped
        try:
            if hwnd:
                win().move_window(hwnd, new_x, new_y, bar_w, bar_h, True)
        except Exception:
            return
        state["manual_xy"] = (new_x, new_y)

    def drag_stop(event):
        """释放：
        - 收起态 -> 手势状态机裁决：拖动态 -> on_persist 持久化把手坐标
          （handle_x/handle_y，原子写，poll 不再吸回）；双击第二次 -> 不动作；
          单击 -> 排 250ms 待定展开（第二次按下到来前），时间到才展开一次。
        - 完整态 -> 结束拖动、标记手动定位（poll 不再吸回），记录稳定位置。"""
        if state.get("collapsed"):
            cur_xy = state.get("last_drag_xy")
            # 双击尾巴遮蔽：收起后 DOUBLE_CLICK_TAIL_MS 内（collapse_bar 置的
            # ignore_click_until）到达的释放事件——双击收起的残余 Release#2——
            # 直接取消待定并丢弃，不喂手势机（否则 in_double_press 已被
            # cancel_click 清掉，release 误走 _arm_click，250ms 后 click 路径
            # 展开，收起被弹回）。
            if time_ms() < state.get("ignore_click_until", 0):
                hand_gesture.cancel_click()
                state["last_drag_xy"] = None
                return
            act = hand_gesture.release({"x_root": event.x_root,
                                        "y_root": event.y_root}, cur_xy)
            state["last_drag_xy"] = None
            if act == E_ACTION_PERSIST:
                # on_persist 已写配置 + manual_handle；此处补记真实当前位置
                pass
            return
        if not state.get("dragging"):
            return
        state["dragging"] = False
        state["drag_offset"] = None
        state["press_xy"] = None
        state["manual_position"] = True
        try:
            xy = current_window_xy()
            if xy is not None:
                state["manual_xy"] = xy
        except Exception:
            pass

    def re_dock():
        """重新贴边：清除手动定位标志（完整态 manual_position 与收起态
        manual_handle），让 poll 下一拍恢复停靠/记忆位置。"""
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["manual_handle"] = False

    def toggle_show(key):
        """右键「显示项」开关：更新内存配置 -> 立即重画 -> 原子写回配置文件。
        写回失败不崩（save_config_show_keys 内已记 err 日志）；重画失败也只记日志。"""
        try:
            val = bool(show_vars[key].get())
        except Exception:
            return
        cfg[key] = val
        save_config_show_keys(config_path, cfg, data_dir)
        _sync_cfg_mtime()   # 写回后 mtime 已变：同步热加载基线，避免下一拍重读同值文件
        # 立即重画（不等下一拍刷新）；启动早期 last_info 可能仍是全 None dict
        try:
            render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(data_dir, "render after toggle_show error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    # 拖动绑到 Canvas 全区域（含两行文字与各指标块；close 小块在 drag_start 内排除）
    canvas.bind("<ButtonPress-1>", drag_start)
    canvas.bind("<B1-Motion>", drag_move)
    canvas.bind("<ButtonRelease-1>", drag_stop)
    # 双击状态条任意区域 -> 收起到边缘（collapsed 把手）；收起态双击由手势
    # 状态机裁决（第二次抬起不作任何展开/收起，双击收起态无意义）
    canvas.bind("<Double-Button-1>",
                lambda _e: collapse_bar() if not state.get("collapsed") else None)

    def refresh_stats():
        info = {"text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None}
        try:
            # 配置热加载：mtime 变化则重读（坏 JSON 保留当前配置 + err 日志一行）
            state["cfg_mtime"] = hot_reload_config(
                cfg, config_path, data_dir, state.get("cfg_mtime"))
            if not interval_fixed:
                try:
                    state["refresh_ms"] = max(
                        int(cfg.get("refresh_ms", REFRESH_MS_DEFAULT)), 200)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            # N6 性能：逐秒全量解析改走指纹缓存，文件未变时复用上次结果
            rows, _err = read_jsonl_cached(os.path.join(data_dir, JSONL_NAME))
            # 会话判定改为信号驱动 + 粘滞（resolve_session_sticky，经
            # resolve_gui_info(sess_state=state)）：不再每拍读 mark 判变化。
            # model/title 缓存复用由 resolve_gui_info 内建 reuse 判定
            # （cur.session_id 与判定 sid 不同即重读）；此处仅保留周期性强刷
            # （db_read_count == 0 时每 DB_READ_INTERVAL 拍重读一次）。
            prev_info = state["last_info"]
            cur_cache = (None if (prev_info is None
                                  or state["db_read_count"] == 0)
                         else prev_info)
            # 0.6.0：状态徽标来源改为 ZCode 钩子时序 status-state.json
            # （status_event.py 写），不再读 live_stream / 不依赖代理；
            # 会话判定走 sticky 信号链（live_session_id 恒 None）。
            status_state = None
            if cfg.get("show_live", True):
                status_state = _read_status_state(data_dir)
            info = resolve_gui_info(rows, data_dir, db_path, cfg=cfg,
                                    cur=cur_cache,
                                    live_session_id=None,
                                    sess_state=state)
            session_changed = (prev_info is not None
                               and info.get("session_id") != prev_info.get("session_id"))
            force_db = (prev_info is None
                        or session_changed
                        or state["db_read_count"] == 0)
            state["force_db"] = force_db  # 诊断用：model/title 强刷已由 cur_cache/reuse 覆盖
            # 钩子时序 -> 状态徽标；生成中时 speed 取 db_latest_speed 精确速度。
            status, spd, turn_stats = resolve_turn_status(
                status_state, db_path, info.get("session_id"))
            info["status"] = status
            info["status_speed"] = spd
            info["turn_stats"] = turn_stats
        except Exception:
            pass
        state["last_info"] = info
        state["db_read_count"] += 1
        if state["db_read_count"] >= DB_READ_INTERVAL:
            state["db_read_count"] = 0
        try:
            render_ui(info)
        except Exception:
            # 渲染层异常落 err 日志（pythonw 无 console，静默空白无法诊断）
            try:
                _log_err(data_dir, "render_ui error:\n%s" % traceback.format_exc())
            except Exception:
                pass
        _after(state.get("refresh_ms", refresh_ms), refresh_stats)

    def _apply_noactivate():
        """持续保障 WS_EX_NOACTIVATE（0.2.1）：创建后仅在 1960 一次设置过，
        拖动/点击激活会重入清除扩展样式（点击 pythonw 后窗口可被激活，导致
        前台判定/焦点行为异常）。每次显示前重新 Apply + SWP_NOACTIVATE。"""
        if not hwnd:
            return
        try:
            style = win().get_window_long(hwnd, GWL_EXSTYLE)
            if not (style & WS_EX_NOACTIVATE):
                win().set_window_long(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
            win().set_window_pos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )
        except Exception:
            pass

    def set_visible(show):
        if not hwnd:
            return
        if show == state["shown"]:
            return
        state["shown"] = show
        try:
            if show:
                win().show_window(hwnd, SW_SHOWNOACTIVATE)
            else:
                win().show_window(hwnd, SW_HIDE)
        except Exception:
            pass

    def poll():
        if not hwnd:
            _after(state.get("refresh_ms", refresh_ms), poll)
            return
        try:
            # 正在拖动：跳过前台/最小化/找窗等全部隐藏判定（拖动中绝不
            # withdraw，否则拖动会被自隐藏打断），也不贴边 MoveWindow；
            # 释放后恢复完整显隐判定 + manual_position 接管停靠位置。
            if state.get("dragging"):
                _apply_noactivate()   # 拖动中显示同样持续保障 NOACTIVATE
                set_visible(True)
                return
            collapsed = bool(state.get("collapsed"))
            if collapsed:
                # 收起态分支（0.2.0 与完整态彻底解耦）：
                # 收起把手**不依赖 ZCode 窗口查找**（用户 ZCode 标题是终端风格
                # 时精确标题/精确 exe 兜底都可能找不到，下一拍 poll 用 SW_HIDE
                # 会把双击收起后的把手藏掉 ->「能缩不能显示」）：
                #   - ZCode 进程真退出（_zcode_process_alive False）-> 藏；
                #   - 真窗口句柄可用且最小化（IsIconic）-> 藏；句柄拿不到
                #     （标题匹配不到/权限不足）则跳过该检查，绝不因此隐藏；
                #   - 否则走现有收起停靠（贴屏幕底部中心），set_visible(True)。
                # 0.2.2 根因修复：坐标/工作区判定**瞬时失败**（current_window_xy
                # 或 work_area_of_rect 返回 None）不再 SW_HIDE（曾导致「双击收起
                # 后把手消失」），回退默认停靠（工作区底部居中，default_handle_xy）
                # 并保持可见；判定抽为模块级 collapsed_poll_decision（依赖显式
                # 注入，可独立重放测试），本闭包只做副作用与诊断落盘。
                bar_w = state.get("cur_w") or HANDLE_W_DEFAULT

                def _collapsed_err(name):
                    # 同因连续失败只记一行（瞬时失败通常下一拍自愈；持续失败
                    # 也不刷屏），判定恢复后由成功路径清 last_collapse_err。
                    _throttled_collapsed_err(state, data_dir, name)

                visible, hxy = collapsed_poll_decision(
                    state, cfg, bar_w,
                    alive=_zcode_process_alive(),
                    zcode_hwnd=find_zcode_window(),
                    is_iconic=win().is_iconic,
                    current_xy=current_window_xy,
                    work_area=work_area_of_rect,
                    valid_and_clamped=lambda xy, wa: valid_and_clamped_handle_xy(
                        xy, wa, bar_w),
                    default_dock=_default_dock_xy,
                    on_err=_collapsed_err,
                )
                if not visible:
                    set_visible(False)
                    return
                if state.get("last_collapse_err") is not None:
                    state["last_collapse_err"] = None   # 判定恢复 -> 允许再记
                if hxy is not None and hxy != state["last_xy"]:
                    win().move_window(hwnd, hxy[0], hxy[1], bar_w, HANDLE_H, True)
                    state["last_xy"] = hxy
                set_visible(True)
                return
            hz = find_zcode_window()
            if not hz:
                set_visible(False)
                return
            if win().is_iconic(hz):          # 最小化 -> 隐藏
                set_visible(False)
                return
            # 0.2.1 判活改判：完整态可见性只由「公开目标（ZCode）存在且非
            # 最小化且窗口矩形可得」决定——贴边/拖动后点击或拖动会激活 pythonw
            # 自身（is_foreground_zcode 变 False，pythonw exe 不含 zcode 匹配
            # 不到），旧逻辑这里 SW_HIDE 会把完整条藏没。现在不再因前台非
            # ZCode 就隐藏；前台仅降级为辅助贴边（见下），不决定窗口死活。
            zrect = window_rect_of(hz)
            if zrect is None:
                set_visible(False)
                return
            # 手动定位：拖动过的小条停在用户放下的位置，不再贴边/吸回
            # （仍受目标存在/最小化/矩形可得显示逻辑控制）。
            if state.get("manual_position"):
                _apply_noactivate()
                set_visible(True)
                return
            # 完整态（未收起）：贴 ZCode 底部外沿（水平居中），钳制工作区。
            # 前台判定仅用于「贴边锚点退一级」（ZCode 非前台但窗口存在时用
            # 当前矩形贴边，仍保持可见），不再作为隐藏条件。
            bar_w = state.get("cur_w") or WINDOW_W
            if is_foreground_zcode():
                xy = dock_rect(zrect, bar_w, WINDOW_H)
            else:
                # 非前台退一级贴边：y 取 bottom（zrect[3]）+ margin，与
                # dock_rect below 模式（y = bottom + margin）同口径。
                xy = clamp_to_work_area((zrect[0], zrect[3] + MARGIN),
                                        bar_w, WINDOW_H, zrect)
            xy = clamp_to_work_area(xy, bar_w, WINDOW_H, zrect)
            if xy is None:
                set_visible(False)
                return
            if xy != state["last_xy"]:
                win().move_window(hwnd, xy[0], xy[1], bar_w, WINDOW_H, True)
                state["last_xy"] = xy
            _apply_noactivate()
            set_visible(True)
        except Exception:
            # 任何异常只隐藏或保持现状，绝不让小条抢焦点、绝不弹错。
            # 0.2.1：仅隐藏本拍，下一拍 poll 仍会按「目标存在/最小化/矩形可得」
            # 重新判定显示（不永久藏死）——异常多为瞬时（如窗口重入瞬间矩形
            # 取不到），fail-closed 防抢焦点但不可把条永远藏没。
            try:
                set_visible(False)
            except Exception:
                pass
        finally:
            _after(state.get("refresh_ms", refresh_ms), poll)

    try:
        refresh_stats()       # 启动即有一行内容，不干等 1 秒
    except Exception:
        pass
    try:
        _after(state.get("refresh_ms", refresh_ms), poll)
    except Exception:
        pass
    try:
        root.mainloop()
    finally:
        _cleanup_pid(data_dir)
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="ZCode token-stats 智能贴边底部状态条",
    )
    ap.add_argument("--data-dir", default=None,
                    help="override data dir (default plugin data dir)")
    ap.add_argument("--db-path", default=None,
                    help="override db.sqlite path (default cli/db/db.sqlite)")
    ap.add_argument("--interval-ms", type=int, default=None,
                    help="refresh/poll interval in ms (default: config refresh_ms, else 1000)")
    ap.add_argument("--config", default=None,
                    help="override statusbar-config.json path "
                         "(default <data-dir>/statusbar-config.json)")
    ap.add_argument("--once", action="store_true",
                    help="read stats once, print one JSON line, exit; no window")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    data_dir = args.data_dir or DATA_DIR_DEFAULT
    # 注：ZCODE_PLUGIN_DATA 不参与解析——钩子环境下该变量指向另一套空目录
    # （data/zcode-token-stats@local/），曾导致与钩子读写分叉。
    db_path = args.db_path or DB_DEFAULT

    # 配置读取（--config 覆盖；默认 <data-dir>/statusbar-config.json，缺省自动生成）
    config_path = args.config
    if not config_path:
        config_path = os.path.join(data_dir, CONFIG_FILE_NAME)
    cfg, _cfg_src = load_config(config_path)

    # 刷新间隔优先级：--interval-ms 显式 > 配置 refresh_ms > 默认 1000
    if args.interval_ms is not None:
        refresh_ms = max(int(args.interval_ms), 200)
    else:
        refresh_ms = max(int(cfg.get("refresh_ms", REFRESH_MS_DEFAULT)), 200)

    # ---- 自证机制：GUI 启动写 boot.log（--once 不建窗口不落 boot）----
    if not args.once:
        try:
            os.makedirs(data_dir, exist_ok=True)
            with open(os.path.join(data_dir, "statusbar.boot.log"), "a",
                      encoding="utf-8") as _bf:
                _bf.write(time.strftime("%Y-%m-%d %H:%M:%S")
                          + " STATUSBAR_VERSION=" + STATUSBAR_VERSION
                          + " pid=" + str(os.getpid()) + chr(10))
        except Exception:
            pass

    if args.once:
        payload = read_stats_once(data_dir, db_path, cfg=cfg)
        _print_utf8_line(json.dumps(payload, ensure_ascii=False))
        return 0

    if not os.path.isdir(data_dir):
        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception:
            pass
    # 防多开（已有存活实例 -> 直接退出）
    try:
        _ensure_single_instance(data_dir)
    except SystemExit:
        return 0
    try:
        return run_gui(data_dir, db_path, refresh_ms, cfg,
                       config_path=config_path,
                       interval_fixed=(args.interval_ms is not None))
    except Exception as e:
        # GUI 初始化失败（无桌面/tkinter 缺失等）：绝不让钩子失败，
        # 清 pid 并把 traceback 落到数据目录 err 日志（pythonw 无 console，
        # 静默退出曾让崩溃无法诊断），再退出 0。
        try:
            _cleanup_pid(data_dir)
        except Exception:
            pass
        try:
            _log_err(data_dir, "docked_statusbar GUI init failed: %r\n%s"
                     % (e, traceback.format_exc()))
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
