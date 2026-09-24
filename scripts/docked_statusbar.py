#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
docked_statusbar.py — ZCode token-stats「智能贴边底部状态条」（对话级三区：状态徽标+对话名 / 本轮统计 / 会话累计）。

作用：ZCode 没有官方 UI 槽位，本脚本用一个无边框置顶的 tkinter 小条
「智能贴边」到 ZCode 主窗口底部外沿（水平居中）。显示条件是「ZCode 窗口
存在且未最小化且矩形可得」——切到其他程序**不**隐藏（0.2.1，前台判定只让
贴边锚点退一级）；每 ~1 秒刷新为对话级状态显示：
  - 第一行：状态徽标（空闲=灰「空闲」/ 生成中=绿「⚡生成中」带本轮实时 tok/s /
    工具中=蓝「🔧工具中」/ 失败=红「失败」/ 已中断=灰「已中断」）+ 对话名；
  - 第二行：本轮统计——`⏱<秒> · in <k/M> · out <k/M> · cache hit <%>`，进行中标
    「· 实时」（本轮 model_usage 聚合），本轮尚无数据标「· 上一轮」（非会话累计）；
  - 右侧小字：会话累计（`in <k/M> · out <k/M> · hit <%>` 小号次要）。

「当前会话」判定（fail-closed：判定不充分显示占位，绝不猜）——0.9.0 起不再
是「一条优先级链」，而是 resolve_session_sticky 的**信号竞争 + 粘滞**：每路
信号带自己的真实发生时刻，取最新者，平局按下面的优先序；各源的完整口径见
该函数文档。
  mark   : current-session.json（mark_session.py 在 SessionStart /
           UserPromptSubmit 写真实会话 ID），30 秒内有效
  status : status-state.json 最新一条钩子事件（status_event.py 写），60 秒内有效
  resume : 当天 zcode-*.jsonl 的 session.resumed（真实日志时刻），600 秒内有效
  db     : model_usage 最近 180 秒内有交互调用的主会话（取行的 started_at）
  tool   : tool_usage 起点在 600 秒内且仍 running 的主会话（0.9.2 新增——一把
           长工具运行期间 mark/status/db 会同时过窗，只有它还在动）
全部信号都过窗时沿用粘滞值；首次就无任何信号才退到 token-stats.jsonl 兜底，
并在行尾标注「（最近会话累计）」；连兜底也没有 -> 「（会话未识别，待首轮
活动）」占位。

边界（拿不到可靠信号，不是判定链缺陷）：切到另一个标签但**不发消息**时没有
任何事件源（ZCode 不给 UI 事件，`current-session.json` 只在发消息时写；数据库
里也不存在 UI 状态）。UIA 标签标题反查这条路 0.9.2 真机复核过：内容已在树里
（侧栏会话行连标题都读得到），但那些行无一携带选中态、Name 又是「标题+相对
时间」拼的，指认不出「当前是哪个」——实验代码已于 0.9.6 后移除（归档在
docs/archive/bak/uia_tab_probe.py）。粘滞因此会沿用旧会话，直到新会话重新
出现任一信号。发消息后由 mark/status 立即跟随。

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
  - STATUS_IDLE_AFTER_MS 按事件类型区分新鲜窗口：generating=120s（单次生成
    远短于此）、tool=900s（工具可能长时间运行），未知事件 / 时间戳解析失败
    走 STATUS_IDLE_AFTER_MS_DEFAULT=60s 兜底；最近事件超对应窗口一律回落
    「空闲」；
  - show_live 语义保留（控制实时徽标显示；默认 true）；
  - ensure-proxy 不再由 SessionStart 钩子默认拉起（代理降级为可选）。

0.9.0 变更（状态显示不准 + 状态不稳定，五批一次做完）：
  - **去抖**：新增 status_debounce——generating ↔ tool 属同一「正在干活」族，
    切换设 STATUS_DWELL_MS=2s 最小驻留（实测单轮 model_request_count p50=7 /
    p95=67，即每轮几十次蓝绿翻转）；进出 busy 族（idle/error/cancelled）立即
    生效，绝不延迟「停了要显停」；换会话时清驻留（state.badge_shown）。
  - **权威收尾**：status_detector 前置 DB 判定——turn_usage 行的 completed_at
    不早于最后一条钩子事件（容差 TURN_END_TIE_MS=3s）即本轮已结束，直接给
    idle / error / cancelled（后两者只在收尾 OUTCOME_HOLD_MS=20s 内显示）。
    依据：turn_usage 与 model_usage 都只在完成时落库（本机 completed_at 为
    NULL 的行数为 0），而平台 7 个事件里没有 SessionEnd/中断钩子——Stop 漏写
    时现场实测有一条 generating 晚于该会话最后轮次收尾 11 分钟仍挂「生成中」。
    例外：最后一条事件是本轮 UserPromptSubmit（turn_started_at >= 上一轮收尾）
    = 新一轮刚开始，不得被上一轮的收尾压成空闲。
  - **本轮归属**：写端在 UserPromptSubmit 落 turn_started_at；读端新增
    live_turn_stats 按本轮窗口聚合 model_usage（进行中不再显示上一轮数字），
    本轮尚无落库数据时给 turn 行打 stale_turn 标记，渲染层分别标「· 实时」/
    「· 上一轮」。心跳改用 db_session_model_activity_ts（该会话本事件之后最近
    一次真实模型调用完成时刻），取代被后台写入者刷新的 session.time_updated
    （后者把早已结束的轮次一路续成生成中）。
  - **tok/s 口径**：所有速度/本轮查询统一 INTERACTIVE_SOURCE_SQL 排除
    subagent / compact / session_title（本机 model_usage 计数 55711/54/98 vs
    main_turn 9627，后台占绝对多数，混入必然拉偏），并按 since_ms 限定本轮
    窗口；会话累计（db_aggregate_session）刻意不过滤，口径仍等于该会话全部
    落库 token（verify_against_sqlite 的 MATCH ALL 契约）。
  - **按会话分片**：status-state.json 升级为 v2 `{version, sessions:{sid:rec},
    order}`，写端跨进程加锁读-改-写、只增自己槽位、超出 MAX_SESSION_SLOTS=16
    按序裁剪、拿不到锁时退化为「只写自己那条」；读端 _read_status_state 返回
    原始文档，经 status_state_for(doc, sid) 取本会话记录、
    status_state_latest(doc) 供身份竞争。v1 老文件就地兼容（放进其 sid 槽）。
    根治多窗口互踩：v1 全局单条被后写者整体覆盖，读端的 sid 校验只是掩盖，
    副作用是「B 在干活时 A 恒显示空闲」。

0.9.1 变更（「为什么不显示速度」）：
  - **工具中也显示速度**：resolve_turn_status 的速度取值门从 status=='generating'
    扩到 BUSY_FAMILY。一轮里 PreToolUse->tool / PostToolUse->generating 交替，
    工具执行常占一轮大半时间，只在生成中取会让数字大段消失。
  - **收尾后短时保留**：新增 speed_hold（GUI 进程内缓存 speed_last/_at/_sid）。
    model_usage 只在调用结束时落库，轮次一收尾就没有任何「当前」速度来源，
    原实现徽标转空闲当帧数字同帧消失；现于 SPEED_HOLD_MS=60s 内继续显示最后
    已知值并标「（上次）」，超时或换会话即作废（绝不跨会话带值）。
  - 渲染门收进纯函数 speed_display(status, speed, held, show_live, show_speed)：
    busy 有值显 ⚡数值、generating 无值仍显「生成中…」占位（首 token 前无来源，
    这是平台边界不是缺陷）、tool 无值不显、非 busy 有值显数值+「（上次）」。
  - show_speed 配置项此前只管 build_metric_blocks（0.4.0 三区改版后无调用者，
    即死代码），现真的管第二行的数值段（「生成中…」占位仍归 show_live，那是
    状态可见性兜底不是读数），行为不再与配置脱节。
  - **本轮窗口左沿改用 DB 时钟**（同一条速度链路的另一半）：新增
    turn_window_left_edge，取上一轮 turn_usage 行的 completed_at 作 live_turn_stats
    与 db_latest_speed 的下界，turn 行归属判定改比 completedAt，废弃 0.9.0 的
    15s 固定归属容差。依据：本机实测 UserPromptSubmit 落盘的 turn_started_at
    比该轮 started_at 晚 **17.0s**（钩子是宿主异步拉起的进程，滞后量随负载变化），
    而 0.9.0 的容差只有 15s —— 于是本轮首次调用被排除出窗口（明明在跑本轮却标
    「· 上一轮」）、速度晚一个调用才出现、本轮权威 turn 行被误判 stale_turn。

0.9.2 变更（「一把工具跑几分钟，状态条整片失灵」）：
  - **tool_usage 作为工具态的权威实时源**：该表在工具**开始**时就落行
    （status='running'、completed_at 为空），是长工具运行期唯一仍在更新的
    表——实测一把工具能跑 154s+，其间 model_usage 无新行、钩子文件无新事件、
    mark 停在 30s 窗外，于是「工具中」（900s 硬窗口）与「当前是哪个会话」
    （三路信号全部过窗）两套判定同时失去依据。新增 db_tool_activity /
    db_recent_tool_activity / tool_live_ms；进程被强杀留下的 running 僵尸行
    （本机实测最老一条挂了 27 天）按 TOOL_LIVE_MAX_MS=600s 封顶，不采信。
  - 状态判定新增步骤 C（工具证据续期：钩子事件超窗但该会话仍有一把工具没收尾
    -> 继续显「工具中」），并给步骤 A 加例外（turn 行已收尾、收尾之后又开了
    一把仍未收尾的工具 = 新一轮在跑，不再压成 idle）。
  - 会话身份竞争新增 tool 一路信号：mark/status/db 三路同时过窗时，仍 running
    的主会话工具行决定「当前是哪个会话」，不再退回 jsonl 猜。
  - **徽标带原因**：文案内联工具名（「🔧工具中·Bash」，超长名不进徽标），
    tooltip 给出工具名 + 已跑时长；error 带 error_type、cancelled 区分手动
    打断 / 非用户取消、上下文溢出单列。第二行新增「· 工具 N · 错 M」（本轮
    turn_usage 计数，数据缺失整段不出现，不显示「工具 0」）。
  - 「（最近会话累计）」标注改为**身份来源与数字来源两头都看**（read_stats_once
    输出 statsSource / recentNote）：任一路取自 jsonl 兜底就标。0.9.1 及之前只
    看身份来源，于是长工具运行期出现「数字明明取自 db 聚合却被标成累计」，
    反向也错一次（身份由信号确认、数字是 jsonl 兜底反而不标）。
  - UIA 真机复核（结论翻转）：**ZCode 已向 UIA 暴露 Electron 内容**——主窗口
    树里 Button=189（'搜索 Ctrl+K'/'插件市场'…）、ListItem=84（其中 10 项的
    Name 以数据库会话标题开头，就是侧栏那些会话行）、Text=358。此前「树不可达」
    的判断来自 2026-09-07 的一次外部 dump，已失效。但探测仍取不到当前会话，
    两道闸各断一次：① IsSelected 为真的 63 项全是消息正文段，10 个会话行
    无一选中（含正开着的那个会话）；② 会话行的 Name 是
    「标题+相对时间」拼的，`resolve_sid_by_title` 只有「精确相等」和
    「截断标题按前缀 LIKE」两档，方向相反，命中不了。所以
    probe_active_tab_title 依旧恒 None，enable_uia_tab_probe 保持默认关
    （而且单次实测 85ms，开着就是每 3 拍多卡 85ms）。真要启用得同时换
    判别量（SelectionItemPattern / AutomationId / 类名）和加一档
    「Name 以标题开头」的反向匹配。
    这条链路上共挖出 7 处错并全部修掉：`_vtbl_func` 用 `.contents[slot]`（c_void_p
    实例不可下标，TypeError 被 except 吞成 None -> 全模块 COM 恒失败，这才是
    probe 恒 None 的真因）、IID_IUIAutomation 值错（E_NOINTERFACE）、10 字节的
    VARIANT 出参、VT_BSTR 未定义、SysFreeString 按 32 位传指针、
    CreatePropertyCondition 槽 15→23 且条件值 VT_BOOL→VT_I4、TabItem id
    50018→50019（50018 是 Tab）。补了 COM 指针 Release，并加了 3 条真机 COM
    冒烟测（改坏绑定就会红）。
  - 清理：删掉 0.4–0.6 遗留的 live_stream 读取链（live_session_id 参数一路）与
    8 个无调用者的函数、一批死常量（约 230 行），模块头的「当前会话判定」按
    0.9.x 实情重写。

0.9.3 变更（「ZCode 都关了，状态条还挂在屏幕上显工具中」）：
  - **生命周期绑定**：小条此前是分离常驻进程（ensure-docked-statusbar.cmd 用
    start + pythonw 拉起，刻意不归宿主生命周期管），ZCode 退出后只是「找不到
    主窗口就 SW_HIDE」，进程一直挂着。现每拍按**进程表**判活
    （zcode_app_running：CreateToolhelp32Snapshot 遍历，exe 名精确等于
    zcode.exe），连续缺席满 ZCODE_GONE_QUIT_MS=5 秒即 root.quit() 自行退出
    （收起态把手同样退），并清掉 statusbar.pid。
  - 显隐与退出分成两条判定：显隐仍看窗口（ZCode 最小化/收进托盘时窗口拿不到
    -> 只隐藏，宿主还在）；退出只看进程表。不能复用窗口那条——它挂着
    OpenProcess / QueryFullProcessImageNameW 等多个可瞬时失败的调用，用它判
    退出等于宿主活着时随时可能把小条杀掉。快照失败一律 fail-open（判存活）。
  - 阈值按**时长**不按拍数：refresh_ms 可在 250–60000 之间配置，按拍算同一个
    「5 秒」会跨三个数量级。5 秒也覆盖 ZCode 升级/重装的进程交接：空窗在 5 秒
    内则小条不受影响，超过则退出、由新进程的 SessionStart 钩子重开一条（顺带
    就是新版本了）。判定与真机进程表各有一组测试（tests/
    test_lifecycle_binding.py，12 条），含结构体 568 字节/字段偏移的实测断言。

0.9.6 变更（三处闭包判定外提为可测纯函数；顺带修掉外提时撞到的收起把手拖不动）：
  - 动机是覆盖问题，不是手感问题：外提前本文件 5113 行，run_gui 从 3733 行起
    占 1381 行、内含 39 个直接子闭包，而 262 条既有测试只能触达模块级纯函数
    ——完整态显隐/贴边、悬停展开守卫、拖动落点这三处判定从来没有一个断言钉过
    （0.2.1 那个「点一下小条它就自己消失」的 bug 正是这么漏掉的）。沿用
    collapsed_poll_decision 已有的路子：依赖全部显式注入，闭包只留副作用。
  - 新增 expanded_poll_decision（完整态 iff 窗口存在+未最小化+矩形可得，前台
    只降级贴边锚点）、hover_expand_should_schedule（冷却窗 / 未武装 / 按压或
    拖动中三闸）、drag_target_xy（收起态与完整态两条拖动路径原先各抄一遍
    「根坐标减偏移 + 伪矩形夹取」，现合并）。
  - 刻意不做物理拆文件：钩子与注册表按 ${ZCODE_PLUGIN_ROOT} 直引这个脚本的
    路径，拆成包要同步改 hooks 命令行、install.cmd 的复制清单和 sys.path 兜底，
    收益不抵风险——外提纯函数已经拿到「可测」这一项，剩下的只是行长。
  - **修 bug：收起把手拖不动**（合并那两条拖动路径时看清的算术，不是新加的
    需求）。drag_move 收起支每帧现算 `off = 指针 - 当前窗口坐标`，代进去得
    `new = 指针 - off = 当前坐标`——恒等式，把手被 MoveWindow 回它已经在的
    地方；指针怎么走都不动，松开时 on_persist 把这个没动过的坐标写进
    handle_x/handle_y。完整态没这问题，因为它在 drag_start 就冻结了
    drag_offset。三处改动缺一不可：
      - drag_grab_offset：一次拖拽内把抓取偏移冻结（首帧不跳变，其后按指针
        增量走）；缓存判定用 `is not None`，偏移恰为 (0,0)（按在把手左上角）
        是合法抓取量，真值判断会把它当「还没抓过」；
      - drag_start 收起支：新的一次按下作废上次的抓取偏移；
      - poll 的拖动守卫补上 hand_gesture.dragging：收起分支原先只判
        state["dragging"]（完整态专用，收起态恒 False），于是 poll 每拍（~1
        秒）按记忆/默认位把手拽回去——只修偏移的话，症状会从「拖不动」变成
        「拖一下、弹回一下」。
    README 里「把手可拖动且不出屏」这句从 0.2.0（引入把手拖动的那次提交，算式
    与本次修前逐字相同）起就是错的，现在才成立。
  - 除上述修复外行为逐条等价；唯一非等价的小处是 poll 里 _apply_noactivate
    挪到 move_window 之前——它带 SWP_NOMOVE|SWP_NOSIZE，不动位置也不动尺寸，
    先后无涉。新增 tests/test_poll_decisions.py 33 条（全库 295 条），含把旧
    算式原样重放一遍的负面证据，和「GUI 侧确实用了纯函数/守卫已补」的源码断言。

0.9.5 变更（纯文档勘误，运行时行为与 0.9.4 完全一致）：
  - 清掉「ZCode 最小化或切到其他程序时自动隐藏」这类写反的显隐描述，README 与
    本 docstring 共 6 处。实际判据：完整态自 0.2.1 起只看「ZCode 窗口存在 + 未
    最小化 + 矩形可得」，前台判定降级为只让贴边锚点退一级（旧逻辑在非前台时
    SW_HIDE，正是当初「点一下小条它就自己消失」那个 bug 的根因）；收起态把手自
    0.2.0 起更与「窗口找不找得到」解耦，只在宿主进程退出或真句柄可查且已最小化
    时藏。留着这句会让下一次改动照它改回去。
  - 复核不靠推理：改文案时前台是别的程序、ZCode 在后台，线上小条
    IsWindowVisible=1 —— 与新文案一致。

0.9.4 变更（「数据不准：中转上命中率 80 多，这边显示 49.8%」）：
  - 先核口径再改代码：两边**公式相同**（缓存读取 ÷ 输入总量，实测同一小时本方
    83.3% vs 中转 82.2%），差异全在总体不同——中转那条小时曲线以 subagent 流量
    为主（该小时 145 行里 134 行是 subagent、命中率 86.2%，同期主会话交互调用
    只有 51.2%；全天口径 85.0% vs 65.9%），小条只看主会话的交互调用。数字是对的，
    但看不出为什么对，于是补两处可自证的量：
  - **累计段补 hit**：右侧会话累计现在是 `in · out · hit <x.x%>`。收起态把手
    本来就画这个数（`◐ 84.9%`），展开反而没有，同一份 stats 两处口径不一。
  - **本轮 tooltip 补请求级画像**：新增「本轮 n 次请求，其中冷读 k 次」。一轮
    2 次请求里 1 次冷读（实测 cr=64/161403）就足以把读数从 90%+ 打到 49.7%，
    没有这句就只能怀疑算错。冷读判据 COLD_READ_RATIO=1%（本机 9386 条调用实测
    双峰：<1% 占 16.6%、1%–5% 仅 0.4%、>=90% 占 75.4%，阈值落在波谷）。
  - 取数：live_turn_stats 同一条聚合顺带算冷读（零额外查询）；turn_usage 那两路
    （权威行 / 「上一轮」）没有逐次分布，新增 db_turn_request_profile 按该轮自己
    的时间窗回 model_usage 数一遍。上下界都缺则不显该句（显示 0 会把「不知道」
    说成「没有冷读」）。
  - 一个反直觉的必要决定：画像那条查询**不过滤** query_source，而 live 那条过滤。
    对账 12 个主会话轮次，turn_usage.input_tokens 与窗口内**全部**行的和 12/12
    相等（含 compact / session_title）——显示的数字里含它们，解释它们的句子就必须
    也含它们。有一轮 3 次调用里 1 次是 compact（cr=0、194141 input），过滤后就只剩
    2 次，「2 次请求无冷读」配着 18% 的命中率出现，比不显示更误导。
  - **tooltip 不再压住小条**（同一批反馈的第二条：「详细信息框跟主显示框冲突
    了」，附截图）。根因与冷读无关，是摆放：旧 `show_tooltip` 按「指针右下」
    放，再被工作区下缘往回夹——小条本就贴在工作区底部附近，几行高的气泡被夹
    回来正好盖住它要解释的那行数字。改为纯函数 tooltip_placement 把小条矩形当
    必须避开的障碍（优先正上方、放不下退正下方、两侧都放不下选空隙大的一侧并
    夹进工作区），文案同时收紧到 TIP_MAX_CHARS=200 字以内、换行宽 380→520px
    （实测最长的本轮 tooltip 从 395x150 降到 532x116）。真机验证：临时数据目
    录起一条 0.9.4，把指针移到「本轮统计」上，按窗口矩形判相交——
    bar=(420,520,1059,576) / tip=(409,398,941,514)，不相交。

数据源：
  - **主数据源 = db.sqlite（model_usage 行级）**：模型调用完成即落库，比
    jsonl（要等 Stop 钩子 record_usage.py 聚合写盘）早一轮，延迟更低。
    按当前会话聚合 status='completed' 且非 subagent 的行（规避 subagent
    会话 id 与 query_source='subagent'，双保险），avgDuration = 行级平均耗时。
  - **tool_usage = 工具态与会话身份的实时源（0.9.2）**：工具一开始就落行，
    补上 model_usage「只在调用结束时落库」留下的空窗（长工具运行期）。
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
    MoveWindow 吸回**（显隐判定照常：仍随 ZCode 窗口存在/未最小化）。右键
    菜单「重新贴边」清除 manual_position，恢复智能自动贴边。拖动中
    （poll 守卫 `state["dragging"] or hand_gesture.dragging`）poll 也不吸回，
    避免拖到一半被抢；0.9.6 起这条同样罩住收起态把手——原先只判完整态那个
    标志（收起态恒 False），poll 每拍把正在拖的把手拽回记忆/默认位。
  - 悬停提示（tooltip）：鼠标悬停在第二行各指标块（或第一行模型/会话）上
    ~400ms 后显示一个无边框置顶小气泡，移开即隐藏。摆放**永远避开小条自己**
    （0.9.4）：优先落在小条正上方，上方放不下退到正下方，横向跟随指针并夹进
    工作区——旧版按「指针右下」放再被工作区下缘回夹，气泡会盖住它要解释的那
    行数字。气泡复用单一全局 toplevel，只改文本，避免反复建窗口。指标块悬停
    同时高亮块背景（#1b1e24 -> #262b33）。
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
      右侧小字（第三区）：会话累计 `in <k/M> · out <k/M> · hit <%>`（灰小号次要，
      jsonl 兜底判定时尾部附「（最近会话累计）」标注）。本轮段 tooltip 附请求级
      画像「本轮 n 次请求，其中冷读 k 次」（冷读 = 该次调用缓存读取不足输入的 1%），
      用来解释「一次冷读就能把两位数的命中率腰斩」这类读数困惑。
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
    恢复原状态；收起态显隐只看「ZCode 进程在否 + 真句柄可查时是否最小化」，
    不依赖窗口找不找得到、也不随前台变化；把手可拖动（0.9.6 起才真的跟手：
    抓取偏移在一次拖拽内冻结，此前每帧现算偏移 => 恒等式，拖不动）且钳制
    工作区。
  - 防多开：数据目录 statusbar.pid 记录本进程 pid；已有存活实例直接退出；
    退出时若 pid 仍是自己的则删除。
  - 生命周期绑定（0.9.3）：每拍拍进程表，ZCode.exe 连续缺席满
    ZCODE_GONE_QUIT_MS=5s 即 root.quit() 自行退出（含收起态把手），并清掉
    statusbar.pid，下次 ZCode 的 SessionStart 钩子重开一条。判退出只看
    进程表、不看窗口：最小化/收进托盘时窗口判定同样失败，那时只隐藏不退出。
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
import queue
import sqlite3
import sys
import time
import traceback

# 纯几何/布局层（Stage 3 Step 1 抽离）：tooltip 摆放、把手坐标、两态 poll
# 裁决、拖动落点等零 I/O 纯函数 + 其小常量，整体 re-export 保留旧名可用。
from statusbar_layout import *
from statusbar_layout import __all__ as _layout_all
# DB 查询层（_db_connect / db_* / recent_turn_stats / live_turn_stats /
# tool_live_ms 等）与 _num / time_ms / _is_subagent_sid 已移至 statusbar_db，
# 经下方 import * re-export，旧名 dsb.* 全部保留。
from statusbar_db import *
from statusbar_db import __all__ as _db_all
# 状态机与状态徽标段（_read_status_state / status_detector / status_debounce /
# speed_hold / speed_display / status_badge_text / status_badge_tip /
# resolve_turn_status 及 STATUS_TEXT/STATUS_COLORS/STATUS_TIPS 等常量）已移至
# statusbar_status，经下方 import * re-export，旧名 dsb.* 全部保留。
from statusbar_status import *
from statusbar_status import __all__ as _status_all
# 会话判定层（read_mark_raw / _log_line_ts_ms / tail_session_resume /
# current_session / resolve_session_sticky 及 MARK_FRESH_MS 等判定常量）已移至
# statusbar_session，经下方 import * re-export，旧名 dsb.* 全部保留。
from statusbar_session import *
from statusbar_session import __all__ as _session_all
# 配置层（DEFAULT_CONFIG / load_config / _apply_raw_config / hot_reload_config /
# save_config_keys / save_config_show_keys / SHOW_MENU_ITEMS / _log_err /
# DATA_DIR_DEFAULT / CONFIG_FILE_NAME）已移至 statusbar_config，
# 经下方 import * re-export，旧名 dsb.* 全部保留。
from statusbar_config import *
from statusbar_config import __all__ as _config_all
# GUI 应用层（StatusBarApp：原 run_gui 全部嵌套闭包的方法化 + _run_gui_impl）
# 已移至 statusbar_gui（Round2 Step 5），经下方 import * re-export，旧名
# dsb.StatusBarApp 保留。依赖方向 dsb->gui（gui 经 deps 注入 win/GestureState
# 等，不反向 import dsb，无环）。
from statusbar_gui import *
from statusbar_gui import __all__ as _gui_all

try:
    # 0.8.0 目录监听（事件驱动刷新）；import 失败静默降级为 None，
    # 调用方（run_gui）据此退回纯轮询，绝不影响主流程。
    import dir_watcher
except Exception:
    dir_watcher = None


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATUSBAR_VERSION = "0.11.1"  # 自证版本：肉眼可确认状态条运行的是本版代码
# DATA_DIR_DEFAULT / LOG_DIR_DEFAULT / MARK_FILE_NAME / CONFIG_FILE_NAME
# 已移至 statusbar_config / statusbar_session（经上方 import * re-export）。
DB_DEFAULT = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
JSONL_NAME = "token-stats.jsonl"
PID_NAME = "statusbar.pid"
# STATUS_STATE_NAME / STATUS_IDLE_AFTER_MS* / GENERATING_MAX_LIFETIME_MS /
# BUSY_FAMILY / STATUS_DWELL_MS / TURN_END_TIE_MS / OUTCOME_HOLD_MS /
# SPEED_HOLD_MS 已移至 statusbar_status（经上方 import * re-export）。
# MARK_FRESH_MS / STATUS_SIGNAL_FRESH_MS / RESUME_FRESH_MS 已移至
# statusbar_session（经上方 import * re-export）。
DB_READ_INTERVAL = 1        # 每拍重读 db（查询实测亚毫秒，换取刷新及时性）
STATS_PENDING = u"\uff08\u672c\u8f6e\u7ed3\u675f\u540e\u66f4\u65b0\uff09"  # （本轮结束后更新）
TURN_PENDING = u"\uff08\u672c\u8f6e\u7edf\u8ba1\u5f85\u66f4\u65b0\uff09"  # （本轮统计待更新）——0.4.0 第二行无已完成轮次时占位
SESSION_UNKNOWN = u"\uff08\u4f1a\u8bdd\u672a\u8bc6\u522b\uff0c\u5f85\u9996\u8f6e\u6d3b\u52a8\uff09"  # （会话未识别，待首轮活动）
SESSION_RECENT_NOTE = u"\uff08\u6700\u8fd1\u4f1a\u8bdd\u7d2f\u8ba1\uff09"  # （最近会话累计）——jsonl 兜底判定时的标注
DATA_ANOMALY_NOTE = u"\uff08\u6570\u636e\u5f02\u5e38\uff09"  # （数据异常）——数据组装/刷新落错时第二行尾部标注（info["error"] 的消费者）


WINDOW_W = 620        # 状态条基准宽度（初始 geometry；实际宽度按内容自适应，
                      # 见 plan_statusbar_layout_3zone —— 徽标与统计永不因宽度裁剪）
# WINDOW_H / MARGIN / TIP_EDGE_GAP / HANDLE_H / HANDLE_W_DEFAULT /
# HANDLE_FALLBACK_MARGIN 已移至 statusbar_layout（经下方 import * re-export）。
REFRESH_MS_DEFAULT = 1000  # 数据刷新 / 贴边/前台轮询（用户要求默认 1000ms）
FS_POLL_MS = 200            # 文件事件队列排空间隔（事件驱动刷新的响应上限）
                            # 第二轮 Stage5：50→200。50ms 时队列 99%+ 为空转
                            # （每秒 20 次 after 回调只偶尔有事件）；200ms 仍在
                            # 1 秒兜底链内（refresh_tick 独立保底），事件响应
                            # 从 ≤50ms 变 ≤200ms 人无感知，空转 CPU 降到 1/4。
WATCH_NAMES = ("status-state.json", "current-session.json",
               "token-stats.jsonl", "statusbar-config.json")
# 目录监听白名单：钩子时序 / 会话标记 / jsonl 兜底 / 配置热加载——
# 这四个文件任一变化都意味着「下一拍内容会变」，事件驱动即刻刷新，
# FS_POLL_MS 排空一次队列（响应上限 200ms，替代纯轮询的 1s 延迟）。

# ---- 暗色主题（AA 对比度）----
BG = "#14161a"          # 窗口背景（比纯黑有层次）
BG_SECOND = "#1b1e24"   # 次要背景（tooltip 底 / close 小块底）
FG = "#e6e8eb"          # 主文字（对 BG 对比度 ~13:1，过 AA）
FG_DIM = "#9aa1aa"      # 次要文字 / 标签（~5.8:1，过 AA）
ACCENT_BLUE = "#4f9cf7" # 强调色（模型名 / 第一行小圆点）
ACCENT_GREEN = "#3fb68b"# 命中率 / 省钱（绿）
ACCENT_GREEN_HI = "#56d364"  # 命中率高档（>=80%，亮绿）
ACCENT_YELLOW = "#d29922"    # 命中率低档（<50%，黄）
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

BADGE_FONT = ("Microsoft YaHei UI", 9, "bold")  # 徽标文字（粗体）
BADGE_FG = "#0e1114"          # 徽标文字色（深色，对状态色过 AA）
BADGE_PAD_X = 9               # 徽标内左右留白
BADGE_H = 18                  # 徽标高
BADGE_GAP_STEPS = (8, 6, 4)   # 徽标-标题间距收缩档位（超宽时其次于标题截短）

# ---- 本轮统计 / 会话累计（0.4.0 第二行 + 右侧小字）----
ROW2_TURN_Y = 40              # 第二行（本轮统计 / 累计小字）文字垂直中心
TIP_TURN = (u"口径：「实时」即时聚合 / 无标记权威统计 /「上一轮」未落库 /"
            u"「上次」收尾保留 60s；⚡为最近完成调用速度；「工具 N · 错 M」为本轮工具数与报错数。")
TIP_CUM = (u"口径：hit = 缓存读取 ÷ 输入总量，只算 completed 行；"
           u"累计含 compact 等后台调用，与中转面板（含全部流量）口径不同。")
# TIP_EDGE_GAP 已移至 statusbar_layout。
# 文案长度上限（0.9.4：气泡不再压住小条后，第二道闸是「别长到占半屏」）。
# 520px 换行 + FONT_DIM 实测：175 字 ≈ 4 行 ≈ 82px，加冷读画像句 ≈ 116px。
TIP_MAX_CHARS = 200

# ---- 第二行分段排布（Canvas 直接画文字，无块底）----
BLOCK_PAD = 9            # 段左右留白
ROW1_CY = 13             # 第一行（模型/会话）文字垂直中心

# ---- 自适应宽度（窗口宽按内容实测伸缩；指标块永不因宽度丢块）----
LABEL_MAX_STEPS = (16, 12, 10, 8, 6, 4)  # 会话标题截断上限档位（超宽时优先收紧）
CLOSE_RESERVE_W = 34                     # 右上 close 小块预留宽（24 块宽 + 10 右边距）
WIDTH_HYSTERESIS = 8                     # 宽度变化 <8px 不更新 geometry（防数字跳动闪烁）
ICON_FONT = ("Segoe UI Symbol", 10)  # 块左侧彩色标识字形（Windows 自带符号字体）
ICON_DUR = u"\u25f7"     # ◷ 耗时
ICON_HIT = u"\u25d0"     # ◐ 缓存命中

# ---- 靠边收起（collapsed 把手）----
# HANDLE_H / HANDLE_W_DEFAULT 已移至 statusbar_layout。
HANDLE_HOVER_MS = 500    # 把手悬停多久自动展开（毫秒）
# ---- 收起冷却/展开守卫 ----
COLLAPSE_HOVER_GRACE_MS = 1500   # 收起冷却窗：收起后这段时间内把手不响应悬停展开
DOUBLE_CLICK_TAIL_MS = 500      # 双击尾巴遮蔽窗：收起后此刻内残余 button 释放
                                # 不喂手势机（双击收起的 Release#2 不再 arm_click 展开）
                                 # （防双击收起后把手恰在指针下 -> 500ms 悬停自展开 ->「收起又自己恢复」）
# HANDLE_FALLBACK_MARGIN 已移至 statusbar_layout。
HANDLE_TIP = (u"\u5df2\u6536\u8d77\u2014\u2014"
              u"\u60ac\u505c\u6216\u5355\u51fb\u5c55\u5f00\u5b8c\u6574\u7edf\u8ba1")
              # 已收起——悬停或单击展开完整统计

# 默认显示项配置（DEFAULT_CONFIG）已移至 statusbar_config（经上方 import * re-export）。

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

# ---- 进程表快照（0.9.3 生命周期绑定：ZCode 退出则小条退出）----
ZCODE_EXE_NAME = "zcode.exe"            # 精确比对（小写）进程 exe 名：桌面端主程序
                                        # 与它的 Electron 子进程都叫这个名；CLI 侧
                                        # 没有同名 exe，故不会把钩子/命令行误当宿主
TH32CS_SNAPPROCESS = 0x00000002
ZCODE_GONE_QUIT_MS = 5000               # ZCode 进程连续缺席多久后小条自己退出。
                                        # 取 5 秒：升级/重装时新旧进程交接的空窗
                                        # 一般在此之内（小条不跟着抖没），而真关掉
                                        # 时用户最多看到 5 秒残留。


class _PROCESSENTRY32W(ctypes.Structure):
    """Toolhelp 进程表条目（仅 CreateToolhelp32Snapshot 系列 API 用）。
    th32DefaultHeapID 在 MSDN 是 ULONG_PTR，必须按指针宽度声明，否则
    szExeFile 之前的字段总长在 x64 上少 4 字节 -> 每次读出的 exe 名错位。"""
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


# ---------------------------------------------------------------------------
# 配置读取（容错：坏 JSON / 缺字段 -> 默认值 + err 日志，不崩）
# _log_err / load_config / _apply_raw_config / hot_reload_config /
# SHOW_MENU_ITEMS / save_config_keys / save_config_show_keys 已移至
# statusbar_config（经上方 import * re-export，旧名 dsb.* 全部保留）。
# ---------------------------------------------------------------------------

def _throttled_collapsed_err(state, data_dir, name):
    """收起把手判定失败诊断日志（0.2.2）：同因连续失败只记一行，不刷屏；
    判定恢复后由收起态成功路径清 state["last_collapse_err"]=None。"""
    if state.get("last_collapse_err") == name:
        return False
    state["last_collapse_err"] = name
    _log_err(data_dir,
             "collapsed-handle: %s failed, fallback=default-dock" % name)
    return True


# ---------------------------------------------------------------------------
# 纯数据 / 统计口径（与 inject_context.py 一致，自包含、可独立测试）
# ---------------------------------------------------------------------------

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


# token-stats.jsonl 解析缓存（N6 性能）：文件只增不减，refresh_once 每秒
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


# current_session（jsonl 兜底判定）已移至 statusbar_session（经上方 import * re-export）。


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


def format_tokens_exact(n):
    """精确千分位值（tooltip 补充行用）：2,046,123；主显示仍用 format_tokens 缩写。"""
    try:
        return "{:,}".format(int(n))
    except Exception:
        return str(n)


def _exact_io_line(stats):
    """tooltip 补充行：「精确：in 2,046,123 · out 5,678」（stats 为 None 返回 u""）。"""
    if not stats:
        return u""
    return (u"精确：in %s · out %s"
            % (format_tokens_exact(stats.get("inputTokens") or 0),
               format_tokens_exact(stats.get("outputTokens") or 0)))


def _hit_rate(stats):
    """缓存命中率 = cacheRead / input（db 的 input_tokens 已含 cacheRead 部分）。"""
    denom = stats.get("inputTokens", 0)
    return (stats.get("cacheReadTokens", 0) / denom if denom > 0 else 0.0) * 100.0


def context_window_for(model, cfg):
    """模型的上下文窗口 token 数（查 cfg["context_window"]，缺省回落
    DEFAULT_CONTEXT_WINDOW；cfg 缺省或值非法也给缺省）。

    匹配规则：先精确匹配 model 名；匹配不到再试「前缀匹配」（配置键是 model 的
    前缀，取最长命中）——配置里写 "gpt-5" 就能覆盖 "gpt-5.6-terra" 这类带后缀的
    具体型号。model 为空直接用缺省。
    """
    default = DEFAULT_CONTEXT_WINDOW
    cw_map = (cfg or {}).get("context_window")
    if not model or not isinstance(cw_map, dict) or not cw_map:
        return default
    model = str(model)
    if model in cw_map:
        try:
            v = int(cw_map[model])
            if v > 0:
                return v
        except Exception:
            pass
    # 前缀匹配：取最长的命中键（"gpt-5.6" 优先于 "gpt-5"）
    best = None
    for k, v in cw_map.items():
        k = str(k)
        if model.startswith(k) and (best is None or len(k) > len(best[0])):
            try:
                iv = int(v)
                if iv > 0:
                    best = (k, iv)
            except Exception:
                continue
    return best[1] if best else default


def context_occupancy_text(latest_input, model, cfg):
    """「最近一次调用 input 占上下文窗口 N%」文本；缺数据返回 u""。

    latest_input 为该会话最近一次调用的 input_tokens（int/None）；占分子。窗口
    由 context_window_for(model, cfg) 决定。latest_input 为 None（无调用记录）
    或窗口 <=0 时不显示（避免画出「0%」误导成「这一轮是空的」）。
    """
    if latest_input is None:
        return u""
    try:
        latest_input = int(latest_input)
    except (TypeError, ValueError):
        return u""
    win = context_window_for(model, cfg)
    if not win or win <= 0:
        return u""
    pct = latest_input / float(win) * 100.0
    return (u"最近一次调用 input %s 占上下文窗口（%s）%.1f%%。"
            % (format_tokens_exact(latest_input), format_tokens_exact(win), pct))


def estimate_cost(stats, model, cfg):
    """按 model_prices 单价表估算 stats 的成本（人民币元）；缺单价/开关关返回 None。

    公式：input 成本 + output 成本，各按「每百万 token 单价」折算。返回 float（元）。
      - cfg["show_cost"] 为假 -> None（调用方不显示）；
      - model 不在 model_prices / 单价非数 -> None（缺单价不显示、不报错）；
      - stats 为 None -> None。
    """
    if not (cfg or {}).get("show_cost", False):
        return None
    if not stats:
        return None
    prices = (cfg or {}).get("model_prices")
    if not model or not isinstance(prices, dict):
        return None
    price = _lookup_model_price(model, prices)
    if price is None:
        return None
    inp_cost = (stats.get("inputTokens") or 0) / 1e6 * price["input"]
    out_cost = (stats.get("outputTokens") or 0) / 1e6 * price["output"]
    return inp_cost + out_cost


def _lookup_model_price(model, prices):
    """model_prices 查价：精确匹配 -> 最长前缀匹配；查不到/值非法返回 None。"""
    model = str(model)
    cand = None
    if model in prices:
        cand = prices[model]
    else:
        best = None
        for k in prices:
            k = str(k)
            if model.startswith(k) and (best is None or len(k) > len(best)):
                best = k
        if best is not None:
            cand = prices[best]
    if not isinstance(cand, dict):
        return None
    try:
        inp = float(cand.get("input"))
        out = float(cand.get("output"))
    except (TypeError, ValueError):
        return None
    if inp < 0 or out < 0:
        return None
    return {"input": inp, "output": out}


def cost_text(cost):
    """成本显示文本「≈¥X.XX」；cost 为 None 返回 u""（不显示）。"""
    if cost is None:
        return u""
    return u"≈¥%.2f" % cost


def today_text(today, cfg=None):
    """「今日：in X · out Y · hit Z%」小字段；today 为 None 返回 u""。

    today 为 db_today_stats 的标准 stats dict（全库今日聚合，跨会话）。命中率与
    主显示同公式（_hit_rate）；输入为 0 时不拼 hit（避免把「今日还没数据」说成
    「hit 0.0%」）。cfg 保留以便将来扩展（成本等），当前不使用。
    """
    if not today:
        return u""
    txt = (u"今日：in %s · out %s"
           % (format_tokens(today.get("inputTokens") or 0),
              format_tokens(today.get("outputTokens") or 0)))
    if (today.get("inputTokens") or 0) > 0:
        txt += u" · hit %.1f%%" % _hit_rate(today)
    return txt


def handle_tip(today=None):
    """收起把手 tooltip：固定说明（HANDLE_TIP）+ 可选「今日：in X · out Y · hit Z%」。

    收起时用户只能看到把手，今日用量放在这里是唯一可见入口。today 为
    db_today_stats 的标准 dict；None 时只有固定说明（与旧 HANDLE_TIP 完全一致）。
    """
    tt = today_text(today)
    if not tt:
        return HANDLE_TIP
    return HANDLE_TIP + u"\n" + tt


def _line_from_stats(stats):
    """由标准 stats dict 生成完整统计行文本（含全部指标）。"""
    return (u"\u23f1%.1fs \u00b7 in %s \u00b7 out %s \u00b7 cache hit %.1f%%"
            % (stats["avgDurationMs"] / 1000.0,
               format_tokens(stats["inputTokens"]),
               format_tokens(stats["outputTokens"]),
               _hit_rate(stats)))


def _truncate(s, n):
    """截断到 n 个字符（保留首尾所见），失败返回原文。"""
    if s is None:
        return None
    s_out = str(s).strip()
    if len(s_out) <= n:
        return s_out
    return s_out[:n]


# read_mark_raw / _log_line_ts_ms / tail_session_resume / resolve_session_sticky
# 已移至 statusbar_session（经上方 import * re-export，旧名 dsb.* 全部保留）。





def turn_tool_segments(ts):
    """本轮工具调用段（0.9.2；纯数据）：「\u00b7 工具 N」，其中有报错时追加
    「\u00b7 错 M」（红色）。

    一轮里跑了 5 把工具还是 1 把，直接决定「为什么这么久」；tool_call_count /
    tool_error_count 早在库里，0.9.1 之前只是没画出来。无工具调用返回 []——
    显示「工具 0」是噪声。
    """
    if not ts:
        return []
    try:
        tools = int(ts.get("toolCallCount") or 0)
    except Exception:
        return []
    if not tools:
        return []
    segs = [(u" \u00b7 \u5de5\u5177 ", FONT_MAIN, FG_DIM),
            (str(tools), FONT_NUM, FG)]
    try:
        errs = int(ts.get("toolErrorCount") or 0)
    except Exception:
        errs = 0
    if errs:
        segs.append((u" \u00b7 \u9519 ", FONT_MAIN, FG_DIM))
        segs.append((str(errs), FONT_NUM, STATUS_COLORS["error"]))
    return segs


def turn_request_profile_text(ts):
    """本轮 tooltip 的「请求级画像」句（0.9.4；纯数据，任一计数缺失返回 u""）。

    为什么要有这句：命中率是**按 token 加权**的聚合值，一轮里只要有一次冷读
    （整份 prompt 几乎没进缓存），读数就能从 90%+ 掉到 50% 附近。用户看到的
    「49.7%」是正确的，但看起来像算错（实测反馈：「中转上是 80 多，这边显示 49.8」）。
    给出「n 次请求 / 冷读 k 次」，一眼看得出是被那一次冷读腰斩的。
    """
    if not ts:
        return u""
    try:
        n = int(ts.get("modelCallCount"))
        cold = int(ts.get("coldReadCount"))
    except (TypeError, ValueError):
        return u""
    if n <= 0 or cold < 0 or cold > n:
        return u""
    head = u"本轮 %d 次请求" % n
    if cold == 0:
        return head + u"，无冷读（每次都命中了缓存）。"
    return (head + u"，其中冷读 %d 次（缓存读取不足输入的 %d%%）"
            u"——一次就够把本轮命中率拉低一半。"
            % (cold, int(round(COLD_READ_RATIO * 100))))


def turn_tooltip(ts, latest_input=None, model=None, cfg=None):
    """本轮段 tooltip：关键数字前置，口径说明压缩为一行置底。

    0.11.1 重排（同一反馈：数字沉底读不到）：
      精确行 -> 占用率 -> 成本 -> 请求画像 ->（空行）口径一行。
      无数据时只剩口径说明一行。
    """
    parts = []
    exact = _exact_io_line(ts)
    if exact:
        parts.append(exact)
    occ = context_occupancy_text(latest_input, model, cfg)
    if occ:
        parts.append(occ)
    c = estimate_cost(ts, model, cfg)
    if c is not None:
        parts.append(u"本轮成本 " + cost_text(c) + u"（按单价表估算）。")
    extra = turn_request_profile_text(ts)
    if extra:
        parts.append(extra)
    if parts:
        parts.append(u"")  # 数字与口径说明之间空一行
    parts.append(TIP_TURN)
    return u"\n".join(parts)


def cum_tooltip(stats, today=None, model=None, cfg=None):
    """会话累计 tooltip：关键数字前置，口径说明压缩为一行置底。

    0.11.1 重排：今日行 -> 精确行（会话累计）-> 成本 ->（空行）口径一行。
    无数据时只剩口径说明一行。
    """
    parts = []
    tt = today_text(today)
    if tt:
        parts.append(tt)
    exact = _exact_io_line(stats)
    if exact:
        parts.append(exact + u"（会话累计）")
    c = estimate_cost(stats, model, cfg)
    if c is not None:
        parts.append(u"累计成本 " + cost_text(c) + u"（按单价表估算）。")
    if parts:
        parts.append(u"")  # 数字与口径说明之间空一行
    parts.append(TIP_CUM)
    return u"\n".join(parts)


def cumulative_text(stats, show_hit=True):
    """会话累计小字（0.4.0 第三区）：`in <k/M> · out <k/M> · hit <x.x%>`；
    stats 为 None 返回 None。

    0.9.4 补 hit：收起态把手一直显示的就是这个数（`◐ 84.9%`），展开后反而只剩
    in/out——同一份 stats 两处口径不一，命中率只能靠把手看。用的仍是 _hit_rate
    （与本轮段、--once 的 cache hit 同一公式），不是第三种算法。
    输入为 0 时不拼：_hit_rate 此时返回 0.0，画成「hit 0.0%」是在把一个「还没有
    数据」说成「一次都没命中」。
    """
    if not stats:
        return None
    txt = (u"in %s \u00b7 out %s"
           % (format_tokens(stats.get("inputTokens") or 0),
              format_tokens(stats.get("outputTokens") or 0)))
    if show_hit and (stats.get("inputTokens") or 0) > 0:
        txt += u" \u00b7 hit %.1f%%" % _hit_rate(stats)
    return txt


def build_copy_text(info, cfg=None):
    """右键「复制当前统计到剪贴板」的文本：状态栏当前显示的两行等价文本。

    info 为 run_gui 的 state["last_info"]（缺字段容错）：第一行 = 状态徽标
    文案 + 会话名（+模型），第二行 = 本轮统计全文 + 会话累计。均无数据时
    返回 u""。纯函数（不碰 tkinter），菜单命令负责 clipboard 调用。
    """
    info = info or {}
    if not info:
        return u""
    cfg = cfg or {}
    status = info.get("status") or "idle"
    line1 = status_badge_text(status, info.get("turn_stats"))
    label = info.get("session_label")
    if label:
        line1 += u" · " + label
    if cfg.get("show_model", True) and info.get("model"):
        line1 += u" · " + info["model"]
    parts = [line1]
    cum = info.get("stats")
    if cum:
        parts.append(_line_from_stats(cum))
        cum_txt = cumulative_text(cum)
        if cum_txt:
            parts.append(u"累计：" + cum_txt)
    elif info.get("text"):
        parts.append(info["text"])
    return u"\n".join(parts)


def build_report_text(db_path, session_id=None):
    """右键「打开统计报告」的文本：db_stats 当前会话报告（表格式纯文本）。

    懒加载 db_stats（GUI 路径才用到）；无 db / 查询失败返回错误说明行
    （不向上抛——菜单命令直接展示该文本）。
    """
    try:
        import db_stats
    except Exception as e:
        return u"无法加载 db_stats：%s" % e
    try:
        if session_id:
            agg = db_stats.query_session_stats(db_path, session_id=session_id)
            head = u"会话报告：%s" % session_id
        else:
            agg = db_stats.query_session_stats(db_path)
            head = u"会话报告（最近会话）"
        return head + u"\n\n" + db_stats._fmt_table(agg)
    except Exception as e:
        return u"统计报告生成失败：%s" % e


def _fmt_round_dur(ms):
    """「本轮耗时」缩写：<60s 给 x.xs，>=60s 给 xm yys。ms 为 None 返回 u"—"。"""
    if ms is None:
        return u"—"
    try:
        ms = int(ms)
    except Exception:
        return u"—"
    if ms < 60_000:
        return u"%.1fs" % (ms / 1000.0)
    return u"%dm%02ds" % (ms // 60_000, (ms % 60_000) // 1000)


def build_sessions_overview_text(db_path, limit=3):
    """右键「最近会话速览」的文本：最近 N 个活跃主会话（标题 + 状态 + 本轮耗时）。

    数据来自 statusbar_db.db_recent_sessions；无数据/读取失败返回错误说明行
    （不向上抛——菜单命令直接展示该文本）。纯函数（不碰 tkinter）。
    """
    try:
        rows = db_recent_sessions(db_path, limit=limit)
    except Exception as e:
        return u"最近会话速览生成失败：%s" % e
    head = u"最近 %d 个活跃会话" % int(limit)
    if not rows:
        return head + u"\n\n（暂无会话模型调用记录）"
    status_zh = {
        "completed": u"已完成", "running": u"生成中", "error": u"出错",
        "cancelled": u"已取消",
    }
    lines = [head, u""]
    for i, r in enumerate(rows, 1):
        title = r.get("title") or r.get("session_id") or u"?"
        st = status_zh.get(r.get("status"), r.get("status") or u"未知")
        lines.append(u"%d. %s" % (i, title))
        lines.append(u"   状态：%s · 本轮耗时：%s"
                     % (st, _fmt_round_dur(r.get("last_duration_ms"))))
    return u"\n".join(lines)


def handle_status_color(info):
    """收起把手 ◐ 图标的颜色：跟随当前状态色（收起态也能看出当前状态）。"""
    status = (info or {}).get("status") or "idle"
    return STATUS_COLORS.get(status, STATUS_COLORS["idle"])


def cache_hit_color(pct):
    """命中率数字的分档配色（纯函数）：<50% 黄、50-80% 维持原绿、>=80% 亮绿。
    主显示（build_line2_parts）与收起把手（_render_handle）统一走这里，
    与把手 ◐ 图标的状态色（handle_status_color）互不干扰。"""
    if pct < 50.0:
        return ACCENT_YELLOW
    if pct < 80.0:
        return ACCENT_GREEN
    return ACCENT_GREEN_HI


def build_stats_line(rows, db_path=None, session_id=None, conn=None):
    """
    聚合统计。**数据源优先级改为 db 优先**：db 行级聚合（模型调用完成即
    落库，快一轮）-> jsonl 该会话累计（兜底，避免冷启动显示「—»）->
    「本轮结束后更新」。

    session_id 为 None 时：先 jsonl ts 最大主会话，再 db 最新活跃主会话。
    conn 透传给 db 查询（复用不关闭）；None 时各 helper 自开自关。

    返回 (text, source, used_sid, stats)：
      - text  完整统计行文本（含全部指标，未按配置裁剪）；
      - source 'db' | 'jsonl' | 'jsonl-latest' | 'pending' | 'none'；
      - used_sid 实际参与聚合的会话 id（可能为 None）；
      - stats  标准 stats dict；text 为 STATS_PENDING / 无数据时为 None。
    """
    def _db_or_jsonl(sid):
        """db 主源 -> jsonl 兜底；返回 (text, source, stats) 或 None(无数据)。"""
        if db_path:
            st = db_aggregate_session(db_path, sid, conn=conn)
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
        cur_sid, _err = db_latest_session_id(db_path, conn=conn)
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


def session_label(db_path, session_id, conn=None):
    """会话显示标识：session.title 截断 ~16 字符；无 title -> (未命名会话)。
    conn 透传给 db_session_title（复用不关闭）；None 时自开自关。"""
    if not session_id:
        return u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09"  # （未命名会话）
    title = db_session_title(db_path, session_id, conn=conn)
    if title:
        return _truncate(title, 16) or u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09"
    return u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09"


def resolve_gui_info(rows, data_dir, db_path, cfg=None, cur=None,
                     sess_state=None, db_conn=None):
    """
    每帧（GUI / --once）统一解析展示信息，返回 dict：
      {text, source, session_id, model, session_label, line1, line2, stats}
    其中：
      - session 判定走 resolve_session_sticky（mark/resume/db/tool 信号竞争 + 粘滞；
        sess_state=None 时内部自建临时 dict，向后兼容、退化为无粘滞每拍重判）；
      - db_conn 透传给全部 db 查询（拍级连接复用，不关闭）；None 时自开自关；
      - 统计按该会话 db 优先聚合（build_stats_line）；stats 为标准 dict；
      - model / title 读 db（会话判定为新会话时才重读，否则沿用 cur 缓存）；
      - line1 按配置 show_model/show_session 裁剪；model 保留完整值（tooltip）。
    任何异常兜底为 None / 「—」，绝不外抛。
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
        "stats_source": None,
        "error": None,
        "today_stats": None,
        "latest_input": None,
    }
    try:
        if sess_state is None:
            sess_state = {}  # 向后兼容：临时态退化为无粘滞（每拍走首次分支）
        sid, source = resolve_session_sticky(
            sess_state, rows, data_dir, db_path, conn=db_conn, cfg=cfg,
            # 显式透传 dsb 命名空间的同名函数：保留「往 dsb 打补丁即生效」
            # 的旧 mock 语义（抽离前车与函数同模块；抽离后函数在
            # statusbar_session 解析其全局名，patch.object(dsb, ...) 会
            # 落空——注入把取数通道指回 dsb，行为与抽离前一致）。
            read_mark=read_mark_raw, read_resume=tail_session_resume,
            db_activity=db_recent_session_activity)
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
            model = db_latest_model_id(db_path, sid, conn=db_conn)
            label = session_label(db_path, sid, conn=db_conn)
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

        text, stats_src, used_sid, stats = build_stats_line(
            rows, db_path, sid, conn=db_conn)
        # 「（最近会话累计）」标注的含义是「这串数字不是当前会话的权威值」，
        # 所以要同时看两路：身份是 jsonl 猜的（source=="jsonl"）、或数字本身来自
        # jsonl 兜底（stats_src）。0.9.1 及之前只看前者，于是长工具运行期身份判定
        # 过窗退回 jsonl 猜测时，数字明明取自本会话的 db 聚合也被标成「最近会话
        # 累计」；反之身份由信号确认、数字却是 jsonl 兜底的反而不标——两头都错。
        info["stats_source"] = stats_src
        info["recent_note"] = stats is not None and (
            source == "jsonl" or stats_src in ("jsonl", "jsonl-latest"))
        if info["recent_note"] and text:
            text = text + u" " + SESSION_RECENT_NOTE
        info["text"] = text
        info["line2"] = text
        info["stats"] = stats
        # 速度（tok/s）：db 行级（jsonl 无单次 duration，速度仅 db 有数据）；
        # 附到 stats dict 供指标块渲染，同时平铺到 info 顶层供 --once 输出。
        spd_recent, spd_avg = db_session_speed(db_path, sid, conn=db_conn)
        info["speed_recent"] = spd_recent
        info["speed_avg"] = spd_avg
        if stats is not None:
            stats["speedTokPerSec"] = spd_recent
            stats["speedAvgTokPerSec"] = spd_avg
        # 0.11.0：今日用量（全库今日聚合，跨会话）+ 最近一次调用 input（上下文
        # 占用率分子），供 cum_tooltip / turn_tooltip。会话无关 / 会话内最近一条，
        # 读取失败均为 None（tooltip 据此不显示对应行，不报错）。
        info["today_stats"] = db_today_stats(db_path, conn=db_conn)
        info["latest_input"] = db_latest_model_input(db_path, sid, conn=db_conn)
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
        parts.append(("cache", u"hit %.1f%%" % hit, FONT_NUM,
                      cache_hit_color(hit), None))
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
    """按配置把 stats 渲染成一行文本（--once 的 line 输出；GUI 侧按
    _turn_segments 分段绘制，不经过这里）。"""
    if stats is None:
        return STATS_PENDING
    parts = build_line2_parts(stats, cfg)
    return "".join(p[1] for p in parts)


# ---------------------------------------------------------------------------
# 自适应宽度（纯函数；各宽度由调用方实测后传入，可独立单测）
# ---------------------------------------------------------------------------

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

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        self.create_toolhelp32_snapshot = kernel32.CreateToolhelp32Snapshot
        self.process32_first = kernel32.Process32FirstW
        self.process32_next = kernel32.Process32NextW


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


def zcode_app_running():
    """ZCode 桌面端进程是否存在——按**进程表**判定，不看窗口。

    与显隐判定用的 `_zcode_process_alive()`（按可见顶层窗口）刻意分成两条：
    窗口那条会因 ZCode 最小化 / 收进托盘 / 句柄取不到而失败，那些场合宿主
    还活着，小条只该隐藏，不该跟着退出；退出只在进程表里再没有 ZCode.exe
    时发生。

    任何 API 失败一律返回 True（fail-open）：读不到进程表是小条自己的故障，
    不能因此把用户界面上唯一的那条东西杀掉。
    """
    try:
        snap = win().create_toolhelp32_snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return True
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = win().process32_first(snap, ctypes.byref(entry))
            while ok:
                if (entry.szExeFile or "").lower() == ZCODE_EXE_NAME:
                    return True
                ok = win().process32_next(snap, ctypes.byref(entry))
            return False
        finally:
            try:
                win().close_handle(snap)
            except Exception:
                pass
    except Exception:
        return True


def zcode_gone_exit_due(alive, gone_since_ms, now_ms,
                        hold_ms=ZCODE_GONE_QUIT_MS):
    """生命周期绑定的退出判定（纯函数，不碰 GUI 也不碰 WinAPI）。

    返回 `(quit, gone_since)`：存活 -> `(False, None)`（缺席计时清零）；首次
    发现缺席 -> 记下起点；缺席连续满 `hold_ms` -> `(True, 起点)`。

    阈值按**时长**而不是按拍数：`refresh_ms` 可在 250–60000 毫秒之间配置，
    按拍数算阈值会跨三个数量级，同样的「5 秒」在慢刷新下会变成 5 分钟。
    """
    if alive:
        return False, None
    if gone_since_ms is None:
        gone_since_ms = now_ms
    return (now_ms - gone_since_ms) >= hold_ms, gone_since_ms


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
        info = resolve_gui_info(rows, data_dir, db_path, cfg=cfg, cur=None)
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
        # 0.9.0：分片文档按已判定的 sid 取本会话记录
        status_state = (_read_status_state(data_dir)
                        if (cfg or dict(DEFAULT_CONFIG)).get("show_live", True)
                        else None)
        status_state = status_state_for(status_state, info.get("session_id"))
        status, spd, turn_stats = resolve_turn_status(
            status_state, db_path, info.get("session_id"))
        return {
            "ok": True,
            "line": line,
            "source": info.get("source"),
            "statsSource": info.get("stats_source"),
            "recentNote": bool(info.get("recent_note")),
            "sessionId": info.get("session_id"),
            "model": info.get("model"),
            "sessionLabel": info.get("session_label"),
            "line1": info.get("line1") or SESSION_UNKNOWN,
            "speedTokPerSec": st.get("speedTokPerSec"),
            "speedAvgTokPerSec": st.get("speedAvgTokPerSec"),
            "status": status,
            "recentTurn": turn_stats,
            "version": STATUSBAR_VERSION,
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
    此时配置热加载的 refresh_ms 变化不再覆盖它（CLI 显式参数优先）。

    Round2 Step 5：全部实现（root/canvas/菜单/手势机/tooltip/刷新链/poll
    显隐轮询等原嵌套闭包）已抽为 statusbar_gui.StatusBarApp；本函数只做
    依赖注入（win/GestureState 等主文件私有名经 deps 传入，避免 gui 反向
    import dsb 成环）+ 创建实例并进入主循环。行为与原闭包版逐行一致。"""
    deps = _GuiDeps()
    return _run_gui_impl(deps, data_dir, db_path, refresh_ms, cfg,
                         config_path=config_path,
                         interval_fixed=interval_fixed)


class _GuiDeps(object):
    """run_gui 注入 statusbar_gui 的依赖命名空间（属性级惰性解析：StatusBarApp
    运行期才访问，解析目标均为本文件已定义的名字）。win / GestureState /
    dir_watcher 为私有或 try-import 名，必须经此桥接；常量为少打点时经
    __getattr__ 兜底解析模块全局。"""

    @property
    def win(self):
        return win

    @property
    def GestureState(self):
        return GestureState

    @property
    def dir_watcher(self):
        return dir_watcher

    def __getattr__(self, name):
        # 其余名字（BG/WINDOW_W/JSONL_NAME/zcode_app_running 等模块级常量
        # 与函数）惰性解析模块全局；找不到抛 AttributeError（保持属性语义）。
        g = globals()
        if name in g:
            return g[name]
        raise AttributeError(name)


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
