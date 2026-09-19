# -*- coding: utf-8 -*-
"""0.9.4：tooltip 摆放必须避开小条自己，文案长度必须有闸。

用户反馈（附截图）：「这个详细信息框的显示跟主显示框冲突了」。根因是
`show_tooltip` 按「指针右下」摆放再被工作区下缘往回夹，而小条就贴在工作区
底部附近——几行高的气泡被夹回来正好盖住它要解释的那行数字。修法分两层：
摆放交给纯函数 `tooltip_placement`（本文件主测），文案长度由 `TIP_MAX_CHARS`
和真机 Tk 实测高度兜住。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb

WORK = (0, 0, 1920, 1040)          # 主屏工作区（去掉任务栏）
BAR = (660, 976, 1260, 1032)       # 贴边小条：宽 600、高 56、贴着工作区下缘
GAP = dsb.TIP_EDGE_GAP


def _hit(a, b):
    """两个 (l, t, r, b) 矩形是否相交。"""
    al, at, ar, ab = a
    bl, bt, br, bb = b
    return not (ar <= bl or br <= al or ab <= bt or bb <= at)


def _place(tw, th, px, py, bar=BAR, work=WORK):
    x, y = dsb.tooltip_placement(tw, th, px, py, bar, work)
    return (x, y, x + tw, y + th)


class TestPlacementAvoidsBar(unittest.TestCase):
    """小条是必须避开的障碍：优先上方，放不下退下方。"""

    def test_prefers_above_the_bar(self):
        tip = _place(520, 116, 900, 1000)
        self.assertEqual(tip[3], BAR[1] - GAP)          # 底边贴在小条上缘之上
        self.assertFalse(_hit(tip, BAR))

    def test_flips_below_when_no_room_above(self):
        """小条被拖到屏幕顶：上方放不下 -> 落到小条下方。"""
        bar = (660, 10, 1260, 66)
        tip = _place(520, 300, 900, 40, bar=bar)
        self.assertEqual(tip[1], bar[3] + GAP)
        self.assertFalse(_hit(tip, bar))

    def test_picks_the_bigger_gap_when_neither_side_fits(self):
        """小条几乎占满工作区高：选空隙大的一侧，再把 y 夹进工作区。"""
        # 下方空隙 (1040-6-990=44) > 上方 (40-6=34)，但 200 高的气泡两边都放不下
        bar = (660, 40, 1260, 990)
        tip = _place(520, 200, 900, 500, bar=bar)
        self.assertEqual(tip[1], WORK[3] - 200)         # 贴工作区下缘，不飞出屏
        self.assertEqual(tip[3], WORK[3])
        # 反过来：上方空隙更大时向上夹
        bar = (660, 60, 1260, 1010)
        tip = _place(520, 200, 900, 500, bar=bar)
        self.assertEqual(tip[1], WORK[1])

    def test_sweep_never_overlaps_when_a_side_fits(self):
        """扫一遍小条位置 × 指针位置：只要有一侧放得下，就不许相交。"""
        bad = []
        for bt in range(GAP, WORK[3] - 56, 97):
            bar = (660, bt, 1260, bt + 56)
            fits = (116 <= bt - GAP - WORK[1]) or (116 <= WORK[3] - bar[3] - GAP)
            for px in (660, 700, 900, 1200, 1260, 1600):
                tip = _place(520, 116, px, bt + 28, bar=bar)
                if tip[0] < WORK[0] or tip[2] > WORK[2]:
                    bad.append(("x out of work", bt, px, tip))
                if tip[1] < WORK[1] or tip[3] > WORK[3]:
                    bad.append(("y out of work", bt, px, tip))
                if fits and _hit(tip, bar):
                    bad.append(("overlaps bar", bt, px, tip))
        self.assertEqual(bad, [])

    def test_x_follows_pointer_but_stays_in_work_area(self):
        self.assertEqual(_place(200, 40, 900, 1000)[0], 800)       # 以指针为中心
        self.assertEqual(_place(200, 40, 20, 1000)[0], WORK[0])    # 左缘夹紧
        self.assertEqual(_place(200, 40, 1900, 1000)[0], WORK[2] - 200)


class TestPlacementFallbacks(unittest.TestCase):
    """拿不到小条矩形 / 气泡比工作区还大：退旧行为或夹紧，不抛不飞。"""

    def test_no_bar_rect_falls_back_to_pointer_offset(self):
        x, y = dsb.tooltip_placement(200, 40, 500, 600, None, WORK)
        self.assertEqual((x, y), (400, 614))

    def test_tip_taller_than_work_area_clamps_to_top(self):
        tip = _place(520, 4000, 900, 1000)
        self.assertEqual(tip[1], WORK[1])

    def test_wider_than_work_area_aligns_left(self):
        tip = _place(5000, 40, 900, 1000)
        self.assertEqual(tip[0], WORK[0])

    def test_zero_sized_tip_does_not_raise(self):
        self.assertEqual(dsb.tooltip_placement(0, 0, 10, 10, BAR, WORK)[1],
                         BAR[1] - GAP)


class TestTipCopyBudget(unittest.TestCase):
    """文案闸：气泡不再压住小条，但一屏长的段落照样读不动（同一反馈的另一半）。"""

    def test_static_tips_fit_the_budget(self):
        self.assertLessEqual(len(dsb.TIP_TURN), dsb.TIP_MAX_CHARS)
        self.assertLessEqual(len(dsb.TIP_CUM), dsb.TIP_MAX_CHARS)

    def test_profile_line_stays_one_short_sentence(self):
        txt = dsb.turn_request_profile_text({"modelCallCount": 12,
                                             "coldReadCount": 3})
        self.assertLessEqual(len(txt), 60)
        self.assertNotIn(u"\n", txt)

    def test_rendered_box_height_bounded(self):
        """真机 Tk 量一遍：最长的 tooltip 也不许超过 140px（约 2.5 行小条高）。"""
        try:
            import tkinter
        except Exception:
            self.skipTest("tkinter 不可用")
        try:
            root = tkinter.Tk()
        except Exception:
            self.skipTest("无可用显示（Tk 初始化失败）")
        try:
            texts = [dsb.turn_tooltip({"modelCallCount": 2, "coldReadCount": 1}),
                     dsb.TIP_CUM,
                     dsb.status_badge_tip(
                         "error", {"status": "error",
                                   "errorType": "upstream_stream_disconnected",
                                   "toolCallCount": 12, "toolErrorCount": 3,
                                   "contextExceeded": 1})]
            for t in texts:
                lbl = tkinter.Label(root, text=t, font=dsb.FONT_DIM,
                                    wraplength=520, padx=8, pady=5)
                lbl.update_idletasks()
                self.assertLessEqual(lbl.winfo_reqheight(), 140)
                self.assertLessEqual(lbl.winfo_reqwidth(), 560)
                lbl.destroy()
        finally:
            root.destroy()


class TestShowTooltipWiring(unittest.TestCase):
    """GUI 侧确实换用了新摆放：show_tooltip 源码里不再有「指针右下 + 下缘回夹」。"""

    def test_source_uses_the_pure_placer(self):
        import inspect
        src = inspect.getsource(dsb.run_gui)
        self.assertIn("tooltip_placement(", src)
        self.assertNotIn("wb - th - 4", src)


if __name__ == "__main__":
    unittest.main()
