#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_layout.py — docked_statusbar 的纯几何/布局层（Stage 3 Step 1 抽离）。

本模块只放零 I/O、依赖显式注入的纯函数与它们用的小常量：
tooltip 摆放、把手默认/夹取坐标、收起/完整两态 poll 裁决、悬停展开守卫、
拖动落点/抓取偏移。docked_statusbar 通过 `from statusbar_layout import *`
整体 re-export，旧名（dsb.tooltip_placement 等）全部保留可用。
"""

# ---- 布局常量（被本模块纯函数引用；docked_statusbar 其他部分经 re-export 沿用）----
WINDOW_H = 59         # 状态条高度（1px 顶线 + 信息行 ~20px + 指标行 ~30px + 底部 3px 阴影带）
MARGIN = 6            # 贴边留白
# tooltip 与小条 / 工作区边缘的最小间距（摆放纯函数 tooltip_placement 用）
TIP_EDGE_GAP = 6
HANDLE_H = 24            # 收起把手高度（v0.12.5：18→24，拉开文字与底部色带间距消除重叠）
HANDLE_W_DEFAULT = 72    # 收起把手宽度估算（内容自适应渲染；记忆位置/越界回退用）
HANDLE_FALLBACK_MARGIN = 8  # 越界回退右下角时距工作区右/下缘的留白
DOCK_BOTTOM_GAP = 0      # 把手/展开条与工作区底边的间隙（v0.12.5：贴底 0 间隙，独立于 MARGIN）

__all__ = [
    "WINDOW_H", "MARGIN", "TIP_EDGE_GAP",
    "HANDLE_H", "HANDLE_W_DEFAULT", "HANDLE_FALLBACK_MARGIN", "DOCK_BOTTOM_GAP",
    "tooltip_placement", "default_handle_xy", "valid_and_clamped_handle_xy",
    "hidden_skip_refresh", "collapsed_poll_decision", "expanded_poll_decision",
    "hover_expand_should_schedule", "drag_target_xy", "drag_grab_offset",
]


def tooltip_placement(tw, th, px, py, bar_rect, work_rect):
    """tooltip 左上角 (x, y)——小条自己的气泡**绝不许压住小条**（0.9.4）。

    旧实现按「指针右下」摆放，再被工作区下缘往回夹。小条本来就贴在 ZCode
    窗口底部（≈工作区下缘），于是几行高的气泡被夹回来正好盖住它要解释的那行
    数字（用户反馈：「详细信息框跟主显示框冲突了」）。现在把小条矩形当成必须
    避开的障碍，纵向只有两个合法落点：

      1. 优先小条**正上方**（整块矩形落在小条上缘之上）；
      2. 上方放不下（小条被拖到屏幕顶）→ 小条**正下方**；
      3. 两侧都放不下（小条几乎占满工作区高）→ 选空隙较大的一侧，再把 y 夹
         进工作区。此时压住小条不可避免，但至少不飞出屏幕；
      4. 横向以指针为中心，再夹进工作区（气泡比工作区还宽时靠左不靠右）。

    bar_rect / work_rect 均为 (left, top, right, bottom)。bar_rect 拿不到
    （句柄矩形瞬时失败）时退回「指针右下 + 工作区夹紧」的旧行为。
    """
    wl, wt, wr, wb = work_rect
    x = max(wl, min(int(px) - tw // 2, wr - tw))
    if bar_rect and len(bar_rect) == 4:
        _bl, bt, _br, bb = bar_rect
        above = bt - TIP_EDGE_GAP - wt
        below = wb - TIP_EDGE_GAP - bb
        if th <= above:
            return x, bt - TIP_EDGE_GAP - th
        if th <= below:
            return x, bb + TIP_EDGE_GAP
        if above >= below:
            return x, max(wt, bt - TIP_EDGE_GAP - th)
        return x, min(wb - th, bb + TIP_EDGE_GAP)
    return x, max(wt, min(int(py) + 14, wb - th))


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
    # v0.12.5：默认停靠贴工作区底边 0 间隙（DOCK_BOTTOM_GAP），不再用 MARGIN 留白
    y = wb - handle_h - DOCK_BOTTOM_GAP
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


def hidden_skip_refresh(state):
    """完全不可见（state["shown"] 为假）时跳过数据组装（Stage 2 性能）：
    窗口被 set_visible(False)/SW_HIDE 后，每秒一拍仍全量跑 6-8 条 DB 查询
    纯属浪费。判定只认 shown——收起把手（collapsed）时把手仍要显示缓存率
    数字，依赖本拍数据，此时 shown 恒为 True，不会被跳过。隐藏期间
    last_info/db_read_count 保持不动；恢复可见由 set_visible 置
    db_read_count=0，下一拍自然全量重读，不残留旧数据。"""
    return not state.get("shown", True)


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


def expanded_poll_decision(state, bar_w, zcode_hwnd, is_iconic, rect_of,
                           foreground, dock, clamp,
                           window_h=WINDOW_H, margin=MARGIN,
                           work_area=None, snap_thresh=40,
                           bottom_gap=DOCK_BOTTOM_GAP):
    """完整态 poll 显隐/贴边判定（0.9.6 抽为模块级纯函数，可独立重放测试；
    副作用 show/move 仍由调用方 run_gui.poll 执行，本函数只做裁决）。

    显示口径（0.2.1 起，与收起态刻意不同）：完整条可见 iff「ZCode 窗口存在
    + 未最小化 + 窗口矩形可得」三条，**切到其他程序不隐藏**——前台判定只让
    贴边锚点退一级，不作为隐藏条件（旧逻辑这里 SW_HIDE 会把完整条藏没，
    表现为「点一下小条它就自己消失」）。

    v0.12.5 贴底吸附：ZCode 窗口下沿距工作区底边 ≤ snap_thresh（默认 40px）
    时，完整条直接吸附到工作区底边（y = wb - window_h - bottom_gap），不再
    按 zrect.bottom + margin 跟随——窗口贴底时小条也贴屏幕底（0 间隙）；
    窗口悬在中间时仍按原逻辑跟随窗口下沿。work_area 为 None 时退回旧跟随
    行为（向后兼容，测试可不传）。

    参数（全部显式注入，不隐式依赖闭包/全局）：
      - state:          run_gui state（读 manual_position）
      - bar_w:          完整条当前宽度
      - zcode_hwnd:     ZCode 顶层窗口句柄；0/None -> 藏
      - is_iconic(h):   该窗口是否最小化（True -> 藏）
      - rect_of(h):     该窗口屏幕矩形；取不到返回 None（-> 藏）
      - foreground():   ZCode 是否前台（只决定贴边锚点，不决定显隐）
      - dock(zrect, bar_w, bar_h):         前台贴边坐标
      - clamp(xy, bar_w, bar_h, zrect):    工作区夹取；xy 为 None 时返回 None
      - window_h / margin: 条高与非前台贴边的额外留白
      - work_area(zrect): 矩形所在显示器工作区；None -> 跳过贴底吸附
      - snap_thresh:    ZCode 窗口下沿距工作区底多少 px 内吸附贴底
      - bottom_gap:     吸附后与工作区底边的间隙（默认 DOCK_BOTTOM_GAP=0）

    返回 (visible, xy)：
      - (False, None)   -> 调用方 SW_HIDE
      - (True, None)    -> 调用方 SW_SHOW（manual_position：停在用户放下的位置，
                           不动坐标、也不改 last_xy）
      - (True, (x, y))  -> 调用方 move_window 到 (x,y) 后 SW_SHOW
    """
    if not zcode_hwnd:
        return False, None
    if is_iconic(zcode_hwnd):          # 最小化 -> 隐藏
        return False, None
    zrect = rect_of(zcode_hwnd)
    if zrect is None:
        return False, None
    # 手动定位：拖动过的小条停在用户放下的位置，不再贴边/吸回（仍受目标
    # 存在/最小化/矩形可得显示逻辑控制）。
    if state.get("manual_position"):
        return True, None

    # v0.12.5 贴底吸附：ZCode 窗口下沿距工作区底边 ≤ snap_thresh 时，
    # 完整条直接吸附到工作区底（0 间隙），不再跟随窗口下沿。
    if work_area is not None:
        try:
            wa = work_area(zrect)
        except Exception:
            wa = None
        if wa and len(wa) == 4:
            wl, wt, wr, wb = wa
            if wb - zrect[3] <= snap_thresh:
                x = zrect[0] + max((zrect[2] - zrect[0] - bar_w) // 2, margin)
                x = min(max(x, wl + margin), max(wl, wr - bar_w - margin))
                return True, (x, wb - window_h - bottom_gap)

    if foreground():
        xy = dock(zrect, bar_w, window_h)
    else:
        # 非前台退一级贴边：y 取 bottom（zrect[3]）+ margin，与 dock_rect
        # below 模式（y = bottom + margin）同口径。
        xy = clamp((zrect[0], zrect[3] + margin), bar_w, window_h, zrect)
    xy = clamp(xy, bar_w, window_h, zrect)
    if xy is None:
        return False, None
    return True, xy


def hover_expand_should_schedule(state, now_ms, pressing, dragging):
    """把手悬停自动展开的排程守卫（0.9.6 抽为纯函数；计时器副作用仍留在
    _schedule_hover_expand 闭包里）。

    三条互斥守卫，任一命中都不排程：
      - 收起冷却窗内（collapse_bar 后 COLLAPSE_HOVER_GRACE_MS）—— 防双击收起
        后把手恰在指针下立即自展开；
      - 未武装（handle_armed=False，收起瞬间被 disarm）—— 悬停展开要求指针先
        离开把手再重新进入（leave->enter）才再次武装；
      - 手势按压/拖动进行中 —— 按下即取消悬停，避免与单击/双击/拖动裁决冲突。
    """
    if now_ms < int(state.get("hover_grace_until") or 0):
        return False
    if not state.get("handle_armed", False):
        return False
    if pressing or dragging:
        return False
    return True


def drag_target_xy(root_xy, offset, bar_w, bar_h, clamp):
    """拖动落点：指针根坐标减按下偏移，再按**目标位置**所在显示器的工作区
    夹取（以目标坐标构造伪矩形定显示器，防止拖出屏幕无法自救）。完整态与
    收起态两条拖动路径同用（0.9.6 合并去重）。

    clamp 返回 None（工作区不可得）时保留未夹取值——与合并前两处一致。
    """
    x = root_xy[0] - offset[0]
    y = root_xy[1] - offset[1]
    clamped = clamp((x, y), bar_w, bar_h, (x, y, x + bar_w, y + bar_h))
    if clamped:
        x, y = clamped
    return x, y


def drag_grab_offset(root_xy, cur_xy, cached):
    """一次抓取只算一次的偏移（0.9.6：修收起把手拖不动）。

    完整态在 drag_start 里记 `drag_offset`，所以本来就跟手；收起态原先每帧
    现算 `off = 指针 - 当前窗口坐标`，代进 drag_target_xy 得 `new = 指针 - off
    = 当前坐标`——恒等式，把手被 move 回它已经在的地方，看上去完全拖不动。
    偏移必须在一次拖拽内冻结，于是：cached 有值就直接用（本帧沿用抓取那次），
    没有则用当帧的 cur_xy 现算并交回调用方缓存（首帧 new == 当前位置，不跳
    变；之后的帧按指针增量走）。cur_xy 取不到（窗口坐标瞬时失败）返回 None，
    调用方本帧不移动。

    缓存判定用 `is not None` 而非真值：偏移恰为 (0, 0)（按在把手左上角）是
    合法抓取量，`if cached:` 会把它当「还没抓过」重新现算，那个按点又变回不跟手。
    """
    if cached is not None:
        return cached
    if cur_xy is None:
        return None
    return (root_xy[0] - cur_xy[0], root_xy[1] - cur_xy[1])
