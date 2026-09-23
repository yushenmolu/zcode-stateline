# -*- coding: utf-8 -*-
"""hot_reload_config 基础覆盖（此前无测试）：mtime 变化热更新指定键、
无效 JSON 不崩且保留当前配置。"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))
import docked_statusbar as dsb


class TestHotReloadConfig(unittest.TestCase):
    """配置热加载的最小行为契约。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sb-hotreload-")
        self.config_path = os.path.join(self._tmp, "statusbar-config.json")
        self.data_dir = self._tmp

    def _write(self, obj):
        with open(self.config_path, "w", encoding="utf-8") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f)

    def test_mtime_change_applies_keys_and_skips_collapsed(self):
        """mtime 变化 -> 指定键原地更新；collapsed 键热加载不同步（写权留给手势）。"""
        cfg = dict(dsb.DEFAULT_CONFIG)
        cfg["collapsed"] = True  # 用户当前收起态：热加载不得翻回 false
        self._write({"show_speed": False, "refresh_ms": 500})
        mtime0 = os.path.getmtime(self.config_path)
        cfg0 = dict(cfg)
        # 首次调用：mtime 与 prev_mtime(0) 不等 -> 合并生效
        m1 = dsb.hot_reload_config(cfg, self.config_path, self.data_dir, 0)
        self.assertEqual(m1, mtime0)
        self.assertFalse(cfg["show_speed"])          # 指定键被热更新
        self.assertEqual(cfg["refresh_ms"], 500)     # refresh_ms clamp 内生效
        self.assertTrue(cfg["collapsed"])            # collapsed 被跳过，未翻回默认 False
        # mtime 未变：原样返回，不再重读
        cfg["show_speed"] = True
        m2 = dsb.hot_reload_config(cfg, self.config_path, self.data_dir, m1)
        self.assertEqual(m2, m1)
        self.assertTrue(cfg["show_speed"])           # 未被重读覆盖
        self.assertEqual(cfg0["collapsed"], cfg["collapsed"])

    def test_invalid_json_keeps_config_and_advances_mtime(self):
        """坏 JSON：不崩、保留当前配置、返回新 mtime（避免每帧刷日志重试）。"""
        cfg = dict(dsb.DEFAULT_CONFIG)
        self._write("{ not valid json !!!")
        mtime_bad = os.path.getmtime(self.config_path)
        before = dict(cfg)
        m1 = dsb.hot_reload_config(cfg, self.config_path, self.data_dir, 0)
        self.assertEqual(m1, mtime_bad)   # 返回新 mtime
        self.assertEqual(cfg, before)     # 配置原样保留
        # 修好后（mtime 前进）能再热更新
        time.sleep(0.01)
        self._write({"show_output": False})
        m2 = dsb.hot_reload_config(cfg, self.config_path, self.data_dir, m1)
        self.assertNotEqual(m2, m1)
        self.assertFalse(cfg["show_output"])

    def test_missing_file_returns_prev_mtime(self):
        """文件不存在：原样返回 prev_mtime，不抛。"""
        cfg = dict(dsb.DEFAULT_CONFIG)
        self.assertEqual(
            dsb.hot_reload_config(cfg, self.config_path, self.data_dir, 123),
            123)


if __name__ == "__main__":
    unittest.main()
