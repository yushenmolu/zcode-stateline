# -*- coding: utf-8 -*-
"""Stage 4 功能改进（第 5 项）单测：命中率数字分档配色 cache_hit_color。

档位：<50% 黄（ACCENT_YELLOW）、50-80% 维持原绿（ACCENT_GREEN）、
>=80% 亮绿（ACCENT_GREEN_HI）。主显示与收起把手统一走该纯函数。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


class TestCacheHitColor(unittest.TestCase):
    """三档边界：49.9/50/79.9/80 及极端值。"""

    def test_low_tier_below_50(self):
        self.assertEqual(dsb.cache_hit_color(0.0), dsb.ACCENT_YELLOW)
        self.assertEqual(dsb.cache_hit_color(49.9), dsb.ACCENT_YELLOW)

    def test_mid_tier_50_to_80_keeps_original_green(self):
        self.assertEqual(dsb.cache_hit_color(50.0), dsb.ACCENT_GREEN)
        self.assertEqual(dsb.cache_hit_color(65.0), dsb.ACCENT_GREEN)
        self.assertEqual(dsb.cache_hit_color(79.9), dsb.ACCENT_GREEN)

    def test_high_tier_at_and_above_80(self):
        self.assertEqual(dsb.cache_hit_color(80.0), dsb.ACCENT_GREEN_HI)
        self.assertEqual(dsb.cache_hit_color(100.0), dsb.ACCENT_GREEN_HI)

    def test_three_tiers_are_distinct(self):
        colors = {dsb.cache_hit_color(p) for p in (25.0, 65.0, 90.0)}
        self.assertEqual(len(colors), 3)


if __name__ == "__main__":
    unittest.main()
