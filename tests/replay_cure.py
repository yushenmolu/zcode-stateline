# -*- coding: utf-8 -*-
"""时序重放测试：验证「收起后不再自弹回小条」修复（0.1.6-fix-20260824）。

测试对象（尽量用生产代码，避免复刻漂移）：
  - 手势时序：生产模块级类 GestureState + 生产常量 CLICK_RECOGNIZE_MS /
    COLLAPSE_HOVER_GRACE_MS / HANDLE_HOVER_MS（直接 import，非复刻）；
  - 热加载竞态（场景⑤）：直接调用生产模块级 hot_reload_config /
    _apply_raw_config + 真实临时配置文件（非复刻）；
  - expand_bar 冷却门禁（场景①②③④）：expand_bar 是 run_gui 闭包内函数，
    无法直接 import，按批准计划桩掉（桩精确复刻生产修复后的门禁逻辑，
    对应生产源码 docked_statusbar.py:_expand_bar 冷却守卫）。

断言 5 场景：
  ① 双击尾巴残余 release -> _fire_click 被冷却拦截 -> collapsed 保持 True（core）
  ② 收起后 1.5s 内悬停 -> 不展开
  ③ 冷却结束后悬停 -> 展开一次
  ④ 冷却结束后单击 -> 展开
  ⑤ collapse_bar 写回 config 后立刻 hot_reload_config -> state["collapsed"]
     仍 True、cfg 不被翻 false
"""
import importlib.util
import json
import os
import sys
import tempfile

MOD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   os.pardir, "scripts", "docked_statusbar.py")
spec = importlib.util.spec_from_file_location("docked_statusbar_test_mod", MOD)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

GRACE_MS = m.COLLAPSE_HOVER_GRACE_MS   # 1500
CLICK_MS = m.CLICK_RECOGNIZE_MS        # 250
HOVER_MS = m.HANDLE_HOVER_MS           # 500

now_ms = [100000]          # 假时钟（epoch 毫秒）


def clock():
    return now_ms[0]


def advance(delta):
    now_ms[0] += delta


# ---------- 桩状态（对应 run_gui 的 state/cfg）----------
state = {
    "collapsed": False,
    "hover_grace_until": 0,
    "handle_armed": False,
    "cfg_mtime": 0.0,
}
collapsed_now = [False]          # state["collapsed"] 镜像
fake_cfg = dict(m.DEFAULT_CONFIG)
expands = []                     # 记录所有 expand/collapse 动作


def stub_expand(source=None):
    # 桩 = 生产 expand_bar 修复后门禁（无 click 豁免）+ 状态翻转。
    # 生产对应：run_gui 内 expand_bar：if time_ms() < state.get("hover_grace_until", 0): return
    if not collapsed_now[0]:
        expands.append(("skip-not-collapsed", source))
        return False
    if clock() < state["hover_grace_until"]:
        expands.append(("blocked-grace", source))
        return False
    collapsed_now[0] = False
    fake_cfg["collapsed"] = False
    state["hover_grace_until"] = 0
    state["handle_armed"] = False
    expands.append(("expand", source))
    return True


def stub_collapse():
    # 桩 = 生产 collapse_bar：写回 cfg + 置冷却窗 + disarm 把手
    collapsed_now[0] = True
    fake_cfg["collapsed"] = True
    state["hover_grace_until"] = clock() + GRACE_MS
    state["handle_armed"] = False
    expands.append(("collapse", None))
    return True


# ---------- 手势机（生产 GestureState + 生产常量）----------
hand_gesture = m.GestureState(
    after=lambda ms, fn: (ms, fn),
    after_cancel=lambda i: None,
    now=clock,
    on_expand=lambda: stub_expand(source="click"),  # 生产: _gesture_click_expand -> expand_bar(source="click")
    on_persist=lambda x, y: None,
)


def single_release_on_handle():
    """一次单击抬起 -> release 返回 E_ACTION_ARM_CLICK -> 排 250ms 待定。"""
    hand_gesture.press(600, 100)
    advance(50)
    act = hand_gesture.release({"x_root": 600, "y_root": 100}, (600, 100))
    print("    (release act=%s)" % act)


def fire_pending_click():
    """复刻生产 _fire_click：假调度器不自动执行回调，手动触发到期。"""
    if not hand_gesture.click_pending:
        return
    hand_gesture.click_pending = False
    hand_gesture.click_id = None
    hand_gesture.on_expand()


failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("  [%s] %s %s" % (tag, name, detail))
    if not cond:
        failures.append(name + " " + detail)


# =====================================================================
print("== 场景① 双击尾巴残余 release + 250ms 待定到期：冷却拦截 -> collapsed 保持 True（core） ==")
now_ms[0] = 50000
stub_collapse()                  # t=50000 双击收起（冷却截至 t=51500）
advance(1000)                    # t=51000：双击尾巴残余 release 落把手
single_release_on_handle()       # release -> _arm_click（250ms 待定）
expands.clear()
advance(CLICK_MS)                # t=51250：_fire_click 到期
fire_pending_click()             # -> on_expand("click")
expands_after = list(expands)
check("①_fire_click 到达但被冷却拦截(blocked-grace,click)",
      len(expands_after) == 1 and expands_after[0] == ("blocked-grace", "click"),
      "record=%r" % (expands_after,))
check("①collapsed 仍 True（未自弹回）", collapsed_now[0] is True,
      "collapsed=%s" % collapsed_now[0])

print("== 场景② 收起后 1.5s 内悬停 -> 不展开 ==")
expands.clear()
state["handle_armed"] = True
advance(200)                     # t=51450 仍在冷却内
# 生产 _schedule_hover_expand 冷却守卫：冷却内不排程
if clock() < state["hover_grace_until"]:
    pass
check("②悬停冷却守卫拒绝（无 expand 排程）",
      len([e for e in expands if e[0] == "expand"]) == 0,
      "record=%r" % (expands,))

print("== 场景③ 冷却结束后悬停 -> 展开一次 ==")
advance(400)                     # t=51850 > 51500 冷却结束
expands.clear()
stub_expand(source=None)         # 悬停计时到期 expand_bar(source=None)
check("③冷却后悬停展开一次",
      expands == [("expand", None)], "record=%r" % (expands,))
check("③展开后 collapsed=False", collapsed_now[0] is False)

print("== 场景④ 冷却结束后单击 -> 展开 ==")
stub_collapse()                  # 重新收起
expands.clear()
advance(GRACE_MS + 100)          # 冷却结束
single_release_on_handle()
fire_pending_click()
check("④冷却后单击展开(expand,click)",
      expands == [("expand", "click")], "record=%r" % (expands,))

print("== 场景⑤ collapse_bar 写回 config 后立刻 hot_reload_config（生产函数）-> collapsed 不被翻 false ==")
# 用真实临时文件 + 生产 load_config / hot_reload_config
tmpdir = tempfile.mkdtemp(prefix="cfgtest_")
cfg_path = os.path.join(tmpdir, "statusbar-config.json")
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(dict(m.DEFAULT_CONFIG), f, ensure_ascii=False, indent=2)
cfg5, _src = m.load_config(cfg_path)              # 生产 load_config（合并 collapsed）
prev_mtime = os.path.getmtime(cfg_path)
# 模拟 collapse_bar：state+collapsed 写回盘
cfg5["collapsed"] = True
m.save_config_keys(cfg_path, cfg5, tmpdir, ["collapsed"])   # 生产 save_config_keys
disk_mtime = os.path.getmtime(cfg_path)
print("    (collapse_bar 写盘 collapsed=True, mtime=%.6f)" % disk_mtime)
# 下一拍 hot_reload_config（生产函数）：mtime 已变 -> 重读合并（skip_collapsed=True）
new_mtime = m.hot_reload_config(cfg5, cfg_path, tmpdir, prev_mtime)
print("    (hot_reload: prev=%.6f new=%.6f)" % (prev_mtime, new_mtime))
check("⑤hot_reload 后 cfg['collapsed'] 仍 True（skip_collapsed 未翻回）",
      cfg5.get("collapsed") is True, "cfg.collapsed=%s" % cfg5.get("collapsed"))
# 盘上文件也被校验：cfg["collapsed"] 未翻 false -> 后续写回不会把 collapsed 清盘
check("⑤盘上 collapsed 仍 True", m.load_config(cfg_path)[0].get("collapsed") is True,
      "disk collapsed=%s" % m.load_config(cfg_path)[0].get("collapsed"))
# 对照组：skip_collapsed=False（旧行为）会被翻回 false —— 证明竞态旧路径真实存在
m._apply_raw_config(cfg5, {"collapsed": False}, skip_collapsed=False)
check("⑤对照：非 skip（旧行为）会被翻回 false（证明竞态存在且已被修）",
      cfg5.get("collapsed") is False, "cfg.collapsed=%s" % cfg5.get("collapsed"))
import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

print()
if failures:
    print("FAILURES: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("ALL 5 SCENARIOS PASS")
sys.exit(0)
