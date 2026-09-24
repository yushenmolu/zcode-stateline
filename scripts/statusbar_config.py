#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
statusbar_config.py — docked_statusbar 的配置层（Round2 Step 4 抽离）。

本模块集中「statusbar-config.json」的全部读写：DEFAULT_CONFIG / CONFIG_FILE_NAME /
DATA_DIR_DEFAULT（配置缺省落盘路径用）、load_config / _apply_raw_config /
hot_reload_config / save_config_keys / save_config_show_keys / SHOW_MENU_ITEMS，
以及它们共用的 _log_err（docked_statusbar 其余部分经 re-export 沿用）。
docked_statusbar 通过 `from statusbar_config import *` 整体 re-export，
旧名（dsb.load_config、dsb.DEFAULT_CONFIG、dsb._log_err 等）全部保留。
本模块不依赖 statusbar_db / statusbar_status，import 方向 dsb→config 无环。
"""

import json
import os
import time

__all__ = [
    # 常量
    "DEFAULT_CONFIG", "CONFIG_FILE_NAME", "DATA_DIR_DEFAULT", "SHOW_MENU_ITEMS",
    "DEFAULT_CONTEXT_WINDOW",
    # 错误日志（配置层与 docked_statusbar 其余部分共用，经 re-export 沿用旧名）
    "_log_err",
    # 配置读写
    "load_config", "_apply_raw_config", "hot_reload_config",
    "save_config_keys", "save_config_show_keys",
]

# 缺省数据目录：配置文件缺省落盘位置与 _log_err 兜底日志目录。与
# docked_statusbar 顶部同源（值一致），抽离后由本模块自包含并经 re-export 共享。
DATA_DIR_DEFAULT = os.path.join(
    os.path.expanduser(r"~/.zcode/cli/plugins/data"),
    "local", "zcode-token-stats",
)
CONFIG_FILE_NAME = "statusbar-config.json"

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
    # ---- 0.11.0 成本估算（默认关）：开启后 tooltip 追加 ≈¥X.XX ----
    "show_cost": False,
    # 模型上下文窗口映射（token 数）；查不到用 DEFAULT_CONTEXT_WINDOW。
    "context_window": {
        "kimi-k3": 262144,
        "kimi-k2": 262144,
        "gpt-5": 400000,
        "gpt-5.6": 400000,
        "claude-sonnet-4": 200000,
        "claude-opus-4": 200000,
    },
    # 各模型单价（每百万 token 人民币）：{"model": {"input": x, "output": y}}。
    # 留示例，用户可按实际改；缺某模型单价时该模型不显示价格（不报错）。
    "model_prices": {
        "kimi-k3": {"input": 4.0, "output": 16.0},
        "gpt-5": {"input": 10.0, "output": 30.0},
        "claude-sonnet-4": {"input": 22.0, "output": 110.0},
    },
}

# 缺省上下文窗口：model 不在 context_window 映射里时的回落值。
DEFAULT_CONTEXT_WINDOW = 200000


def _log_err(data_dir, msg):
    """写一条调试/错误日志到数据目录 docked-statusbar-err.log；失败忽略。"""
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "docked-statusbar-err.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


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
                "show_cost",
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
    # context_window / model_prices：dict 逐项合并（用户可覆盖单项或加新模型，
    # 不整体替换默认值）。值类型不对（非 dict）忽略保留默认。
    if isinstance(raw.get("context_window"), dict):
        merged = dict(cfg.get("context_window") or {})
        for _mk, _mv in raw["context_window"].items():
            try:
                _iv = int(_mv)
                if _iv > 0:
                    merged[str(_mk)] = _iv
            except Exception:
                pass
        cfg["context_window"] = merged
    if isinstance(raw.get("model_prices"), dict):
        merged = dict(cfg.get("model_prices") or {})
        for _mk, _mv in raw["model_prices"].items():
            if not isinstance(_mv, dict):
                continue
            try:
                _inp = float(_mv.get("input"))
                _out = float(_mv.get("output"))
                if _inp >= 0 and _out >= 0:
                    merged[str(_mk)] = {"input": _inp, "output": _out}
            except Exception:
                pass
        cfg["model_prices"] = merged


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
    ("状态徽标", "show_status"),          # 状态徽标
    ("本轮统计", "show_recent_turn"),     # 本轮统计
    ("会话累计", "show_cumulative"),      # 会话累计
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
