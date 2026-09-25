# BUGFIX: 状态栏数字恒绿——底部状态色横线

- **状态**：Verified（0.11.3，截图验证通过：工具中=蓝线 #4f9cf7 实测命中）
- **修复文件**：`scripts/statusbar_gui.py`（两处）
- **修复日期**：2026-09-24

## 症状

状态栏上的命中率 / token 数字始终是绿色，不随「生成中 / 工具中 / 空闲」
等状态变化变色，用户无法从颜色感知当前会话处于什么状态。

## 根因

数字（命中率、token 数）的颜色语义是**命中率档位配色**，由
`scripts/docked_statusbar.py` 的 `cache_hit_color()` 决定（<50% 黄、
50–80% 绿、≥80% 亮绿），与「当前状态」是两套独立的配色体系。状态色
（`scripts/statusbar_status.py` 的 `STATUS_COLORS`：generating=绿、
tool=蓝、idle=灰、error=红、cancelled=灰蓝）原本只画在两处小面积
元素上：

- 展开态第一行的状态徽标（色块 + 文字）
- 收起态把手的 ◐ 图标

因此用户看到的主体数字区域永远只反映命中率，察觉不到状态变化——不是
「状态数据没传到」，而是「状态色没画在用户看的地方」。

## 修复方案（用户拍板的方向 B）

**数字保留命中率档位色不变，另加状态色指示**：在状态栏底部加一条
2px 高、贯穿整条宽度的状态色横线，颜色 = 当前状态色。收起态把手和
展开态都加。

改动点（`scripts/statusbar_gui.py`）：

1. `_render_handle`（~879 行）：在把手 canvas 底部
   `y = HANDLE_H-2 .. HANDLE_H`（18px 内底部 2px）画矩形，
   `fill = deps.handle_status_color(info)`，`outline=""`，
   `tags=("hdl",)`（与 ◐ 图标、命中率数字同 tag，把手整体仍是热区）。
2. `render_ui` 展开态分支（~1028 行，顶部 1px 分隔线之后）：在主 canvas
   底部 `y = WINDOW_H-2 .. WINDOW_H`（56px 内底部 2px）画矩形，
   `fill = STATUS_COLORS.get(status, STATUS_COLORS["idle"])`（与状态徽标
   取色口径一致），`outline=""`，无交互 tag（纯装饰，与顶部分隔线一致）。

关键约束的处理：

- **不推高布局**：两处横线都画在既有 canvas 高度（HANDLE_H=18 /
  WINDOW_H=56）的底部 2px 内，canvas / 窗口总高度不变。
- **不挪现有元素**：收起态文字底部约 y=15（cy=9 + 字体半高 ~6）、展开态
  第二行文字底部约 y=48（ROW2_TURN_Y=40 + 半高 ~8），均与底部
  y=16..18 / y=54..56 的横线不重叠，未微调任何坐标。
- **show_status=False 时也画**：展开态横线取色独立于 `if show_status:`
  分支——否则用户关闭徽标后反而彻底丢失状态指示。
- **刷新模式兼容**：`render_ui` 每帧 `canvas.delete("all")` 后整幅重建
  （收起态 `_render_handle` 同），横线按同一模式 create_rectangle 即可，
  无重复叠加问题，无需 itemconfig。

## 排除项（诊断阶段已排除）

- 钩子写入正常：`status-state.json` 新鲜度 17 秒，状态字段实时更新。
- 副本一致：5 个部署副本文件哈希相同，不存在「改的是 A、跑的是 B」。
- `hook.run.failed` 告警与本插件无关（属另一插件，不干扰本状态链路）。

## 验证

- `python -m py_compile scripts/statusbar_gui.py` → COMPILE_OK。
- 部署后人工验证：切换 idle / generating / tool 状态，观察状态栏底部
  2px 横线颜色随状态切换（灰 → 绿 → 蓝），数字颜色仍按命中率档位变化。
> 归档时间：2026-09-25 | 归档人：AI（色带功能已随 v0.11.4/v0.12.0 上线并截图验证）
