# -*- coding: utf-8 -*-
"""0.9.6：把 run_gui 闭包里三处「靠肉眼验」的判定外提成纯函数后钉住其行为，
并钉住外提时撞到的真 bug——收起把手原先拖不动。

动机不是为改行为而改：`docked_statusbar.py` 5113 行中 `run_gui` 占 1381 行、
内含 39 个直接子闭包，而既有 262 个测试只能触达模块级纯函数——闭包里的显隐、
贴边、悬停、拖动落点判定从来没有一个断言覆盖。本轮不动单文件结构，只把三处
判定抽成依赖显式注入的 `expanded_poll_decision` / `hover_expand_should_schedule`
/ `drag_target_xy`，逐条保持原行为；最后的 TestWiring 用源码守卫钉住「GUI 侧确实
换用了纯函数、旧的内联算术已删净」。

`drag_move` 两条路径合并去重时看清了收起那一支：偏移每帧按「指针 - 当前窗口
坐标」现算，代回 `drag_target_xy` 得 `new = 当前位置`——恒等式，把手被 move 回
它已经在的地方，README 的「把手可拖动」是假的。修法是 `drag_grab_offset`：一次
拖拽内把偏移冻结（TestDragGrabOffset 里既重放修好后的逐帧轨迹，也把旧算式原样
写出来当反面证据）。
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb

HZ = 1234                       # 假 ZCode 句柄
ZRECT = (100, 80, 1700, 1000)   # 假 ZCode 窗口矩形
BAR_W = 600


def _clamp(out=None):
    """假 clamp_to_work_area：记录每次入参；out=None -> 原样返回 xy，
    out='fail' -> 返回 None（模拟工作区不可得），否则返回给定的固定值。"""
    calls = []

    def _f(xy, bar_w, bar_h, zrect):
        calls.append((xy, bar_w, bar_h, zrect))
        if out == "fail":
            return None
        if out == "passthrough" or out is None:
            return xy
        return out

    _f.calls = calls
    return _f


def _dock(calls):
    def _f(zrect, bar_w, bar_h):
        calls.append((zrect, bar_w, bar_h))
        return (zrect[0] + 7, zrect[3] + dsb.MARGIN)
    _f.calls = calls
    return _f


def _expanded(state=None, bar_w=BAR_W, hwnd=HZ, iconic=False, rect=ZRECT,
              fg=True, clamp=None, **kw):
    dk = _dock([])
    cl = _clamp() if clamp is None else clamp
    visible, xy = dsb.expanded_poll_decision(
        {} if state is None else state, bar_w,
        zcode_hwnd=hwnd, is_iconic=lambda h: iconic, rect_of=lambda h: rect,
        foreground=lambda: fg, dock=dk, clamp=cl, **kw)
    return visible, xy, cl, dk


class TestExpandedVisibilityRules(unittest.TestCase):
    """完整态显示条件（0.2.1 口径）：窗口存在 + 未最小化 + 矩形可得。"""

    def test_hides_when_window_not_found(self):
        self.assertEqual(_expanded(hwnd=0)[0], False)
        self.assertEqual(_expanded(hwnd=None)[0], False)

    def test_hides_when_minimized(self):
        self.assertEqual(_expanded(iconic=True)[0], False)

    def test_hides_when_rect_unavailable(self):
        self.assertEqual(_expanded(rect=None)[0], False)

    def test_hides_when_work_area_unavailable(self):
        """两条 clamp 支路都要能退出隐藏：dock 结果夹取失败同样藏。"""
        self.assertEqual(_expanded(clamp=_clamp("fail"))[0], False)
        self.assertEqual(_expanded(fg=False, clamp=_clamp("fail"))[0], False)

    def test_stays_visible_when_another_app_is_foreground(self):
        """0.2.1 回归闸：前台判定**不是**隐藏条件。

        旧逻辑在这里 SW_HIDE，表现为「点一下小条它就自己消失」——点击/拖动
        会激活 pythonw 自身，is_foreground_zcode 变 False 就把自己藏没。
        """
        visible, xy, _, _ = _expanded(fg=False)
        self.assertTrue(visible)
        self.assertIsNotNone(xy)

    def test_manual_position_shows_without_moving(self):
        visible, xy, cl, dk = _expanded(state={"manual_position": True})
        self.assertTrue(visible)
        self.assertIsNone(xy)                 # 调用方据此不动坐标、不记 last_xy
        self.assertEqual(dk.calls, [])        # 不贴边
        self.assertEqual(cl.calls, [])        # 连工作区都不必查

    def test_manual_position_still_yields_to_minimized(self):
        """manual_position 只免「吸回贴边」，不免窗口死活判定。"""
        visible, xy, _, _ = _expanded(state={"manual_position": True},
                                      iconic=True)
        self.assertEqual((visible, xy), (False, None))


class TestExpandedDockAnchors(unittest.TestCase):
    """锚点：前台走 dock_rect，非前台退一级贴 zrect 下缘 + margin。"""

    def test_foreground_uses_dock_and_skips_the_fallback_anchor(self):
        visible, xy, cl, dk = _expanded()
        self.assertTrue(visible)
        self.assertEqual(dk.calls, [(ZRECT, BAR_W, dsb.WINDOW_H)])
        self.assertEqual(xy, (ZRECT[0] + 7, ZRECT[3] + dsb.MARGIN))
        # dock 结果再过一次 clamp（一次），非前台那条退一级锚点不该被走到
        self.assertEqual([c[0] for c in cl.calls], [xy])

    def test_background_anchor_is_bottom_plus_margin(self):
        """与 dock_rect below 模式同口径：y = bottom + margin，x 取窗口左缘。"""
        _, xy, cl, dk = _expanded(fg=False)
        self.assertEqual(xy, (ZRECT[0], ZRECT[3] + dsb.MARGIN))
        self.assertEqual(dk.calls, [])
        self.assertEqual([c[0] for c in cl.calls],
                         [(ZRECT[0], ZRECT[3] + dsb.MARGIN), xy])

    def test_bar_w_and_window_h_reach_both_anchors(self):
        """宽度按调用方给的实际渲染宽度透传，高度取 WINDOW_H。"""
        _, _, cl, dk = _expanded(bar_w=431)
        self.assertEqual(dk.calls, [(ZRECT, 431, dsb.WINDOW_H)])
        self.assertEqual(cl.calls[0][1:3], (431, dsb.WINDOW_H))
        _, _, cl, dk = _expanded(bar_w=431, fg=False)
        self.assertEqual(dk.calls, [])
        self.assertEqual(cl.calls[0][1:3], (431, dsb.WINDOW_H))

    def test_real_dock_rect_centers_the_bar(self):
        """换成生产 dock_rect：水平居中、贴下缘外沿 margin。"""
        visible, xy = dsb.expanded_poll_decision(
            {}, 600, zcode_hwnd=HZ, is_iconic=lambda h: False,
            rect_of=lambda h: ZRECT, foreground=lambda: True,
            dock=dsb.dock_rect, clamp=_clamp())
        self.assertTrue(visible)
        self.assertEqual(xy, dsb.dock_rect(ZRECT, 600, dsb.WINDOW_H))

    def test_margin_is_injectable(self):
        _, xy, _, _ = _expanded(fg=False, margin=40)
        self.assertEqual(xy, (ZRECT[0], ZRECT[3] + 40))

    def test_clamped_result_wins_over_anchor(self):
        _, xy, _, _ = _expanded(clamp=_clamp(out=(5, 6)))
        self.assertEqual(xy, (5, 6))


class TestHoverExpandGuard(unittest.TestCase):
    """收起把手悬停展开的三条互斥守卫。"""

    def _s(self, **over):
        st = {"hover_grace_until": 0, "handle_armed": True}
        st.update(over)
        return st

    def test_schedules_when_all_guards_pass(self):
        self.assertTrue(dsb.hover_expand_should_schedule(self._s(), 1000,
                                                         False, False))

    def test_blocked_inside_collapse_grace(self):
        st = self._s(hover_grace_until=1500)
        self.assertFalse(dsb.hover_expand_should_schedule(st, 1499, False, False))
        self.assertTrue(dsb.hover_expand_should_schedule(st, 1500, False, False))

    def test_blocked_until_rearmed(self):
        self.assertFalse(dsb.hover_expand_should_schedule(
            self._s(handle_armed=False), 2000, False, False))

    def test_blocked_while_pressing_or_dragging(self):
        self.assertFalse(dsb.hover_expand_should_schedule(self._s(), 2000,
                                                          True, False))
        self.assertFalse(dsb.hover_expand_should_schedule(self._s(), 2000,
                                                          False, True))

    def test_missing_or_none_grace_reads_as_zero(self):
        for st in ({"handle_armed": True},
                   {"handle_armed": True, "hover_grace_until": None}):
            self.assertTrue(dsb.hover_expand_should_schedule(st, 1, False, False))


class TestDragTargetXy(unittest.TestCase):
    """拖动落点：指针根坐标 - 按下偏移，再按目标伪矩形所在工作区夹取。"""

    def test_offset_arithmetic(self):
        x, y = dsb.drag_target_xy((500, 400), (10, 20), 72, 18, _clamp())
        self.assertEqual((x, y), (490, 380))

    def test_clamp_sees_pseudo_rect_at_the_target(self):
        cl = _clamp()
        dsb.drag_target_xy((500, 400), (10, 20), 72, 18, cl)
        xy, bar_w, bar_h, zrect = cl.calls[0]
        self.assertEqual(xy, (490, 380))
        self.assertEqual((bar_w, bar_h), (72, 18))
        self.assertEqual(zrect, (490, 380, 562, 398))

    def test_clamped_value_wins(self):
        cl = _clamp(out=(0, 0))
        self.assertEqual(dsb.drag_target_xy((500, 400), (0, 0), 72, 18, cl),
                         (0, 0))

    def test_unclamped_when_work_area_unavailable(self):
        """工作区取不到时保持未夹取值（与合并前两处一致，不因兜底失败丢帧）。"""
        cl = _clamp(out="fail")
        self.assertEqual(dsb.drag_target_xy((500, 400), (0, 0), 72, 18, cl),
                         (500, 400))

    def test_real_clamp_keeps_the_handle_on_screen(self):
        """生产 clamp_to_work_area 的可测替身：以真纯函数验证越界回夹。"""
        work = (0, 0, 1920, 1040)

        def real_clamp(xy, bar_w, bar_h, zrect):
            wl, wt, wr, wb = work
            x, y = xy
            x = min(max(x, wl + dsb.MARGIN), max(wl, wr - bar_w - dsb.MARGIN))
            y = min(max(y, wt + dsb.MARGIN), max(wt, wb - bar_h - dsb.MARGIN))
            return x, y

        self.assertEqual(dsb.drag_target_xy((3000, 3000), (0, 0), 72, 18,
                                            real_clamp),
                         (1920 - 72 - dsb.MARGIN, 1040 - 18 - dsb.MARGIN))


class TestDragGrabOffset(unittest.TestCase):
    """0.9.6 真 bug 修复：收起把手原先拖不动（偏移每帧现算 -> 恒等式）。"""

    @staticmethod
    def _replay(start, ptrs, clamp=None, bar_w=72, bar_h=18):
        """按 drag_move 收起分支的写法逐帧重放：cur_xy 取**上一帧的落点**
        （线上就是 GetWindowRect 读到的当前位置），返回每帧 move 目标。"""
        cl = _clamp() if clamp is None else clamp
        cur, off, hist = start, None, []
        for p in ptrs:
            off = dsb.drag_grab_offset(p, cur, off)
            cur = dsb.drag_target_xy(p, off, bar_w, bar_h, cl)
            hist.append((cur, off))
        return hist

    def test_first_drag_frame_does_not_jump(self):
        """抓取那一帧把手不跳：偏移按当帧现算 -> new == 当前位置。"""
        hist = self._replay((900, 1010), [(905, 1012), (1000, 900)])
        self.assertEqual(hist[0][0], (900, 1010))

    def test_handle_follows_the_pointer_after_grab(self):
        """冻结偏移后，把手按指针增量走（0.9.6 前这一条整帧都是原坐标）。"""
        hist = self._replay((900, 1010), [(905, 1012), (1000, 900),
                                            (1000, 900)])
        self.assertEqual([h[0] for h in hist],
                         [(900, 1010), (995, 898), (995, 898)])

    def test_old_per_frame_recompute_was_a_noop(self):
        """把 0.9.6 前那支原样写出来当反面证据：位置永远不动。"""
        cur = (900, 1010)
        for p in [(905, 1012), (1200, 700), (400, 300)]:
            ox, oy = p[0] - cur[0], p[1] - cur[1]
            cur = dsb.drag_target_xy(p, (ox, oy), 72, 18, _clamp())
            self.assertEqual(cur, (900, 1010))

    def test_cached_zero_offset_is_still_a_grab(self):
        """按在把手左上角 -> 偏移 (0, 0) 是合法抓取量，不能被当成「没抓过」。"""
        self.assertEqual(dsb.drag_grab_offset((500, 400), (500, 400), None),
                         (0, 0))
        self.assertEqual(dsb.drag_grab_offset((900, 900), (500, 400), (0, 0)),
                         (0, 0))
        hist = self._replay((900, 1010), [(900, 1010), (1100, 800)])
        self.assertEqual(hist[1][0], (1100, 800))

    def test_no_move_frame_when_window_xy_unavailable(self):
        self.assertIsNone(dsb.drag_grab_offset((500, 400), None, None))
        self.assertEqual(dsb.drag_grab_offset((500, 400), None, (3, 4)),
                         (3, 4))

    def test_drag_stays_inside_the_work_area(self):
        """跟手不能跟出屏：夹取按目标伪矩形生效。"""
        work = (0, 0, 1920, 1040)

        def clamp(xy, bar_w, bar_h, zrect):
            wl, wt, wr, wb = work
            x, y = xy
            return (min(max(x, wl + dsb.MARGIN), max(wl, wr - bar_w - dsb.MARGIN)),
                    min(max(y, wt + dsb.MARGIN), max(wt, wb - bar_h - dsb.MARGIN)))

        hist = self._replay((900, 1010), [(905, 1012), (5000, 5000)],
                             clamp=clamp)
        self.assertEqual(hist[1][0], (1920 - 72 - dsb.MARGIN,
                                      1040 - 18 - dsb.MARGIN))


class TestWiring(unittest.TestCase):
    """GUI 侧确实换用了纯函数，旧的内联判定/算术已删净。"""

    @classmethod
    def setUpClass(cls):
        # Round2 Step 5：run_gui 闭包提为 statusbar_gui.StatusBarApp 方法，
        # 接线断言改钉 statusbar_gui 模块源码（原 dsb.run_gui 函数体已搬迁）。
        import statusbar_gui
        cls.src = inspect.getsource(statusbar_gui)

    def test_poll_uses_the_pure_decisions(self):
        self.assertIn("expanded_poll_decision(", self.src)
        self.assertIn("hover_expand_should_schedule(", self.src)
        self.assertIn("drag_target_xy(", self.src)
        self.assertIn("collapsed_poll_decision(", self.src)

    def test_old_inline_arithmetic_is_gone(self):
        for dead in ("zrect[3] + MARGIN", "pseudo = (new_x, new_y",
                     'if xy != state["last_xy"]', "if not hz:",
                     "is_iconic(hz)",
                     "ox = event.x_root - xy[0]"):     # 恒等式那支（0.9.6 修）
            self.assertNotIn(dead, self.src)

    def test_gesture_and_dock_dependencies_are_injected(self):
        """判定不再隐式读闭包：手势/工作区/dock 全部由调用方喂进来。
        Round2 Step 5 方法化后：win()/模块函数经 deps 桥接（deps.win() /
        deps.window_rect_of / deps.is_foreground_zcode / deps.dock_rect），
        手势状态机经 self.hand_gesture 访问。"""
        self.assertIn("is_iconic=self._win().is_iconic", self.src)
        self.assertIn("rect_of=deps.window_rect_of", self.src)
        self.assertIn("foreground=deps.is_foreground_zcode", self.src)
        self.assertIn("dock=deps.dock_rect", self.src)
        self.assertIn("self.hand_gesture.press_xy is not None", self.src)

    def test_collapsed_drag_fix_is_wired(self):
        """修好跟手要同时满足三件事：偏移冻结、按下作废、poll 拖动期不吸回。"""
        self.assertIn("drag_grab_offset(", self.src)
        self.assertIn('state["handle_drag_offset"] = None', self.src)   # drag_start
        self.assertIn('state["handle_drag_offset"] = off', self.src)    # drag_move
        # poll 的拖动守卫原先只看 state["dragging"]（完整态），收起态每拍都会
        # 把手拽回记忆/默认位；0.9.6 起补上手势机的 dragging（方法化后经
        # self.hand_gesture / 局部别名 hg 访问，语义等价）。
        self.assertIn('if state.get("dragging") or hg.dragging:',
                      self.src)


if __name__ == "__main__":
    unittest.main()
