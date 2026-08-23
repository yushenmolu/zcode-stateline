# zcode-token-stats

**ZCode 插件：实时统计 token 用量、缓存命中率与生成速度**——对话内自动统计行 + 智能贴边状态条双通道展示，另带 `/stats` 斜杠命令随时查报告。

> English in one sentence: A ZCode plugin that shows real-time token usage, cache hit rate and speed (duration/TTFT) per conversation — as an in-chat stats line plus a docked status bar, with a `/stats` command for full reports.

- 版本：0.2.0
- 许可：MIT
- 平台：Windows（ZCode 桌面版 + Python 3.10+，仅标准库，无第三方依赖）

## 效果示意

```
┌────────────────────────────────────────────────────────────┐
│  ● glm-4.7  修复导出路径的 bug               [×]           │   ← 第一行：模型名 · 会话标题
│ ┌──────┐ ┌────────┐ ┌──────────┐ ┌─────────┐ ┌──────────┐ │
│ │◷ 31.2s│ │▸in 7.7M│ │◂out 51.3k│ │◐ 44.3%  │ │↻ 3.4M    │ │   ← 第二行：彩色指标块
│ └──────┘ └────────┘ └──────────┘ └─────────┘ └──────────┘ │
└────────────────────────────────────────────────────────────┘
   ↑ 智能贴边在 ZCode 窗口底部外沿，可拖动、悬停有解释、仅 ZCode 前台时显示
```

对话内统计行（模型每次回复末尾自动附带，兜底显示）：

```
⏱31.2s · in 7.7M · out 51.3k · cache hit 44.3%
```

（上图为示意，数字为示例数据。）

## 功能特性

- **智能贴边状态条（主显示）**：无边框置顶深色小条，自动吸附在 ZCode 主窗口底部外沿并跟随窗口移动；ZCode 最小化或切到其他程序时自动隐藏，回到 ZCode 才显示，从不抢焦点。
  - **两行彩色指标块**：第一行 = 当前模型名（蓝）+ 会话标题（灰）+ 关闭按钮；第二行 = 一排圆角指标块（耗时 / in / out / 缓存命中 / cache read / reasoning），等宽字体防跳字，暗色主题 AA 对比度。
  - **按当前对话各自统计**：状态条跟随当前活跃对话，显示**该会话自己**的累计数据；子代理（subagent）会话一律过滤，不串数据。
  - **可拖动**：按住任意区域左键拖到任意位置（自动钳制在屏幕内）；拖动后停在原处，右键 →「重新贴边」恢复自动跟随。
  - **悬停解释（tooltip）**：悬停各指标约 0.4 秒弹出气泡解释含义，截断的模型名/会话标题显示完整值。
  - **右键「显示项」菜单**：8 个指标开关直接勾选切换，立即生效并自动写回配置文件。
  - **低延迟数据源**：每约 1 秒只读直查 ZCode 本地数据库 `model_usage` 行级记录（模型调用完成即落库），jsonl 记录仅作兜底。
- **对话内自动统计行（兜底显示）**：会话启动与每轮提问时通过钩子注入上下文，模型在每次回复末尾自动附一行固定格式的统计；状态条不在时也有数字可看。
- **每轮 Stop 自动记录**：每轮结束自动把该会话新增的已完成调用聚合为一行 JSON，追加到 `token-stats.jsonl` 长期档案（幂等，重复触发不产生重复行）。
- **`/stats` 斜杠命令**：会话内输入 `/stats [会话|历史|模型|缓存|速度]` 输出统计报告。
- **配置热加载**：`statusbar-config.json` 保存后约 1 秒自动生效，无需重启；坏 JSON 不崩溃不丢配置。
- **只读 + 全本地**：对 ZCode 数据库全程只读（`mode=ro` + `PRAGMA query_only=ON`），不上传任何数据、无网络访问。

## 安装

### 方式一：ZCode UI 安装（推荐）

1. 把本仓库放到本机任意目录（或 clone 下来）。
2. 打开 ZCode → **设置（Settings）** → **插件管理（Plugin Management）** → **发现（Discover）**。
3. 点 **+** 添加本地市场，选择一个包含本插件 `.zcode-plugin/plugin.json` 所在目录层级的本地市场目录（ZCode 本地插件市场即插件缓存目录 `%USERPROFILE%\.zcode\cli\plugins\cache\local`，方式二脚本会自动把文件放进去）。
4. 在发现列表中找到 **zcode-token-stats** → 点 **Get（获取）** 安装。
5. 到 **已安装（Installed）** 页确认插件为**启用（enabled）**状态。
6. **完全退出并重新启动 ZCode**，钩子、`/stats` 命令与状态条即生效。

### 方式二：双击 install.cmd 一键安装

1. 确认已装 Python 3.10+（在 PATH 中，或设置 `PYTHON_BIN` 环境变量指向 python.exe）。
2. 双击项目根目录的 `install.cmd`（想先预览会发生什么，先在命令行跑 `install.cmd /dry`）。
3. 脚本会自动：复制插件文件到插件缓存目录 → 注册安装记录到 `installed_plugins.json`（先备份原文件）→ 在 `config.json` 中启用插件（先备份原文件）。
4. **完全退出并重新启动 ZCode**。

> ZCode 数据目录不在默认位置时，设置环境变量 `ZCODE_HOME` 指向你的 `.zcode` 目录后再运行。

## 使用

### 贴边状态条

- ZCode 会话启动时自动拉起；**如果重启后小条没出现**，双击运行 `scripts\docked_statusbar.py` 或手动运行 `hooks\ensure-docked-statusbar.cmd` 兜底启动（`statusbar.pid` 防多开，不会起两条）。
- 点右上角 **×** 或右键 →「退出 statusbar」关闭。
- 命令行验证数据读取（不弹窗）：`python scripts/docked_statusbar.py --once` 输出统计 JSON。
- 可选参数：`--data-dir DIR`（数据目录）、`--db-path PATH`（ZCode 数据库路径）、`--interval-ms MS`（刷新间隔）、`--config PATH`（配置文件路径）。

### /stats 命令

会话内输入 `/stats`、`/stats 历史`、`/stats 模型`、`/stats 缓存`、`/stats 速度` 查看对应报告。

### 配置（statusbar-config.json）

配置文件默认位于数据目录 `%USERPROFILE%\.zcode\cli\plugins\data\local\zcode-token-stats\statusbar-config.json`（首次启动自动生成）。**推荐直接用右键菜单切换**；手改文件约 1 秒内热加载生效。

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `show_model` | `true` | 第一行显示模型名 |
| `show_session` | `true` | 第一行显示会话标题 |
| `show_avg_duration` | `true` | 第二行显示平均耗时 ⏱ |
| `show_input` | `true` | 第二行显示输入 token 累计（in） |
| `show_output` | `true` | 第二行显示输出 token 累计（out） |
| `show_cache_read` | `true` | 第二行显示缓存读取 token（cache read） |
| `show_cache_hit` | `true` | 第二行显示缓存命中率（绿色，含微型进度条） |
| `show_reasoning` | `false` | 第二行显示思考 token（reasoning，紫色） |
| `refresh_ms` | `1000` | 刷新/贴边轮询间隔毫秒（250–60000） |
| `theme` | `"dark"` | 主题（当前仅 dark） |

### 右键菜单

- **显示项**：8 个指标开关（模型/会话/耗时/输入/输出/缓存命中/缓存读取/推理），勾选即显隐，立即重画并自动写回配置。
- **重新贴边**：拖动过后恢复自动跟随 ZCode 窗口底部。
- **退出 statusbar**：关闭状态条。

## 系统要求

- Windows（Win32 API + tkinter，其余平台未适配）。
- ZCode 桌面版。
- Python 3.10+，仅标准库（json / sqlite3 / tkinter / ctypes），无需 pip 安装任何包。
- 默认假设 ZCode 数据目录位于 `%USERPROFILE%\.zcode`（数据库 `~\.zcode\cli\db\db.sqlite`）；不一致时用 `ZCODE_HOME` 环境变量（安装脚本）或 `--db-path` 参数（状态条）覆盖。

## 工作原理

```
会话启动/提问 ──► hooks.json ──► mark-session.cmd ──► mark_session.py ──► current-session.json（当前会话标记）
                            └─► inject-context.cmd ──► inject_context.py ──► 注入统计行上下文（additionalContext）
                            └─► ensure-docked-statusbar.cmd ──► docked_statusbar.py（贴边状态条，每秒只读刷新）
每轮结束 Stop ──► hooks.json ──► on-stop.cmd ──► record_usage.py ──► 只读聚合 db ──► 追加 token-stats.jsonl
手动查报告 ────► /stats ──► db_stats.py ──► 只读聚合 db ──► 会话/历史/按模型统计
```

- **数据库只读**：所有 SQL 经 `sqlite3.connect("file:...?mode=ro")` + `PRAGMA query_only=ON`，绝不写 ZCode 数据库。
- **统计口径**：请求数对全部行计数；token/耗时聚合仅 `status='completed'` 行；缓存命中率 = `cache_read / input`（db 的 input 已含 cache_read 部分）；行级排除子代理调用。
- **幂等记录**：每会话独立时间戳游标（`state.json`），只聚合上次记录之后的调用，重复触发不产生重复行。
- **当前会话判定**（fail-closed，绝不猜）：`current-session.json` 新鲜标记（30 秒内）→ 数据库 60 秒内活跃主会话 → jsonl 最新主会话（带「最近会话累计」标注）；子代理会话每步排除。

## 已知限制

- 仅支持 Windows；多显示器 / 非 100% DPI 缩放未实测。
- 纯切换会话标签但不发消息时状态条不跟随（ZCode 无切换事件，标记过期后回退数据库/jsonl 兜底判定）。
- 对话内统计行依赖模型遵循注入的格式指令，偶发遗漏属正常。
- Stop 触发时最后一笔调用可能仍在途，该轮数据由游标在下一轮补齐（延迟而非丢失）；进程被强杀时该轮统计缺失。
- 缓存命中率为输入侧口径，与厂商账单口径（含 cache_creation）可能不一致。
- ZCode 升级若改 `db.sqlite` 表结构，SQL 需要相应调整。

## 常见问题

- **状态条没出现？** 双击 `scripts\docked_statusbar.py`，或运行 `hooks\ensure-docked-statusbar.cmd`；仍不行看数据目录下 `docked-statusbar-err.log`。
- **提示找不到 Python？** 安装 Python 3.10+（python.org，勾选 Add to PATH），或设置环境变量 `PYTHON_BIN` 指向 python.exe（无窗口版可再设 `PYTHONW_BIN` 指向 pythonw.exe）。
- **数字不更新？** 检查数据目录下 `stats.log`、`cmd-stderr.log`、`inject-stderr.log`、`mark-session-stderr.log`（正常应为空或只有少量跳过记录）；确认 ZCode 数据库在默认位置，否则用 `--db-path` 指定。
- **想彻底清空统计数据？** 停止状态条后删除数据目录 `%USERPROFILE%\.zcode\cli\plugins\data\local\zcode-token-stats`（卸载插件默认保留该目录）。

## 卸载

运行 `uninstall.cmd`（先 `uninstall.cmd /dry` 预览）：移除安装记录与启用项（均先备份）→ 删除插件缓存目录 → 重启 ZCode。统计数据目录默认保留，需要彻底清空再手动删除。通过 UI 安装的用户也可在 ZCode 插件管理页直接卸载。

## 许可

[MIT](LICENSE) — Copyright (c) 2026 zcode-token-stats contributors
