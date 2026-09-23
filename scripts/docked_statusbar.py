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

try:
    # 0.8.0 目录监听（事件驱动刷新）；import 失败静默降级为 None，
    # 调用方（run_gui）据此退回纯轮询，绝不影响主流程。
    import dir_watcher
except Exception:
    dir_watcher = None


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATUSBAR_VERSION = "0.10.1"  # 自证版本：肉眼可确认状态条运行的是本版代码
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
FS_POLL_MS = 50             # 文件事件队列排空间隔（事件驱动刷新的响应上限）
WATCH_NAMES = ("status-state.json", "current-session.json",
               "token-stats.jsonl", "statusbar-config.json")
# 目录监听白名单：钩子时序 / 会话标记 / jsonl 兜底 / 配置热加载——
# 这四个文件任一变化都意味着「下一拍内容会变」，事件驱动即刻刷新，
# FS_POLL_MS 排空一次队列（响应上限 50ms，替代纯轮询的 1s 延迟）。

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
TIP_TURN = (u"本轮统计：本轮的耗时 / in / out / cache hit（不是会话累计）。"
            u"「实时」= 本轮已完成调用的即时聚合；无标记 = 本轮权威统计；"
            u"「上一轮」= 本轮还没有数据落库；「上次」= 收尾后 60 秒内保留的末值。"
            u"⚡ = 本轮最近一次已完成调用的 tok/s（不是当前流的实时速度）；"
            u"「工具 N · 错 M」= 本轮工具调用数与其中报错数。")
TIP_CUM = (u"会话累计：本会话全部轮次的 in / out / hit。"
           u"hit = 缓存读取 ÷ 输入总量，与本轮段同一公式，只算 completed 行；"
           u"靠边收起后把手上的 ◐ 就是这一个数。"
           u"累计含 compact / 会话标题这类后台调用（本轮段把它们滤掉了）。"
           u"与中转面板不等是总体不同：那边含全部流量（subagent 占大半、"
           u"命中率普遍更高），这边只算当前会话。")
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


def turn_tooltip(ts):
    """本轮段 tooltip：固定说明 + 精确千分位 in/out + 该轮的请求级画像
    （无计数时只有固定说明）。"""
    tip = TIP_TURN
    exact = _exact_io_line(ts)
    if exact:
        tip += u"\n" + exact
    extra = turn_request_profile_text(ts)
    if extra:
        tip += u"\n" + extra
    return tip


def cum_tooltip(stats):
    """会话累计 tooltip：固定说明 + 精确千分位 in/out（stats 为 None 只有固定说明）。"""
    tip = TIP_CUM
    exact = _exact_io_line(stats)
    if exact:
        tip += u"\n" + exact
    return tip


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
    此时配置热加载的 refresh_ms 变化不再覆盖它（CLI 显式参数优先）。"""
    import tkinter as tk  # 懒加载：GUI 路径才依赖桌面
    import tkinter.font as tkfont  # 各段文字宽度按字体实测

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.attributes("-toolwindow", True)
    except Exception:
        pass
    root.configure(bg=BG)
    root.resizable(False, False)
    root.geometry("%dx%d+0+0" % (WINDOW_W, WINDOW_H))

    # ---- 单 Canvas 绘制层（徽标圆角胶囊 + 两行分段文字 + close 小块）----
    canvas = tk.Canvas(root, bg=BG, highlightthickness=0,
                       width=WINDOW_W, height=WINDOW_H)
    canvas.pack(fill="both", expand=True)

    # 字体实测对象（块宽按文本实测，不拍脑袋定宽）
    f_dim = tkfont.Font(root=root, font=FONT_DIM)
    f_main = tkfont.Font(root=root, font=FONT_MAIN)
    f_num = tkfont.Font(root=root, font=FONT_NUM)
    f_icon = tkfont.Font(root=root, font=ICON_FONT)

    # ---- 右键菜单 ----
    menu = tk.Menu(root, tearoff=0, bd=0, bg=MENU_BG, fg=FG,
                   activebackground=MENU_ACTIVE, activeforeground=FG)
    menu.add_command(label=u"\u91cd\u65b0\u8d34\u8fb9", command=lambda: re_dock())
    menu.add_command(label=u"\u6536\u8d77\u5230\u8fb9\u7f18",
                     command=lambda: collapse_bar())
    menu.add_separator()
    menu.add_command(label=u"复制当前统计到剪贴板",
                     command=lambda: copy_current_stats())
    menu.add_command(label=u"打开统计报告",
                     command=lambda: show_report_window())
    menu.add_separator()

    # 「显示项」子菜单：每个 show_* 一项，checkbutton 勾选态绑定当前配置；
    # 点击即切换 -> 立即重画 -> 原子写回 statusbar-config.json（失败只记 err 日志）。
    show_vars = {}
    display_menu = tk.Menu(menu, tearoff=0, bd=0, bg=MENU_BG, fg=FG,
                           activebackground=MENU_ACTIVE, activeforeground=FG)
    for _lbl, _key in SHOW_MENU_ITEMS:
        _var = tk.BooleanVar(value=bool(cfg.get(_key, False)))
        show_vars[_key] = _var
        display_menu.add_checkbutton(
            label=_lbl, variable=_var,
            command=(lambda k: lambda: toggle_show(k))(_key))
    menu.add_cascade(label=u"\u663e\u793a\u9879", menu=display_menu)
    menu.add_separator()
    menu.add_command(label=u"\u9000\u51fa statusbar", command=lambda: quit_app())

    def on_right_click(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # 右键菜单绑到 Canvas（全区域可呼出）
    canvas.bind("<Button-3>", on_right_click)

    def copy_current_stats():
        """右键「复制当前统计到剪贴板」：取最近一拍 info 拼两行文本进剪贴板。
        无数据 / 剪贴板异常只记 err 日志，不打断 GUI。"""
        try:
            text = build_copy_text(state.get("last_info"), cfg)
            if not text:
                return
            root.clipboard_clear()
            root.clipboard_append(text)
        except Exception:
            _log_err(data_dir, "copy_current_stats error:\n%s"
                     % traceback.format_exc())

    report_win = None

    def show_report_window():
        """右键「打开统计报告」：db_stats 当前会话报告，Toplevel + 滚动文本框
        （单实例复用；风格沿用 tooltip 的深底色）。任何异常只记日志。"""
        nonlocal report_win
        try:
            info = state.get("last_info") or {}
            text = build_report_text(db_path, info.get("session_id"))
            if report_win is not None:
                try:
                    report_win.destroy()
                except Exception:
                    pass
                report_win = None
            win = tk.Toplevel(root)
            report_win = win
            win.title(u"统计报告")
            win.configure(bg=BG_SECOND)
            win.geometry("460x420")
            txt = tk.Text(win, bg=BG_SECOND, fg=FG, font=FONT_DIM,
                          relief="flat", wrap="none")
            sb = tk.Scrollbar(win, command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            txt.pack(side="left", fill="both", expand=True)
            txt.insert("1.0", text)
            txt.configure(state="disabled")

            def _on_close():
                nonlocal report_win
                try:
                    win.destroy()
                finally:
                    report_win = None

            win.protocol("WM_DELETE_WINDOW", _on_close)
        except Exception:
            report_win = None
            _log_err(data_dir, "show_report_window error:\n%s"
                     % traceback.format_exc())

    def quit_app():
        # 先停 watcher（幂等；失败不阻塞退出）再销毁窗口：避免回调线程在
        # 解释器收尾阶段还往队列里放事件
        try:
            if watcher is not None:
                watcher.stop()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        _cleanup_pid(data_dir)
        sys.exit(0)

    def on_close():
        quit_app()

    # close「×」小块改由 Canvas 绘制并按 tag 绑定（见 _draw_close）
    root.protocol("WM_DELETE_WINDOW", on_close)

    try:
        root.update_idletasks()
    except Exception:
        pass

    # 取窗口原生 hwnd：tk 顶层由 winfo_id 的父级承载（已核实）
    try:
        wid = root.winfo_id()
        hwnd = win().get_parent(wid) or wid
    except Exception:
        hwnd = None

    if hwnd:
        # 不抢焦点：加 WS_EX_NOACTIVATE，鼠标点击小条不会把前台抢成
        # pythonw（否则前台判定误判 -> 小条自隐藏/拖动被打断）。
        try:
            style = win().get_window_long(hwnd, GWL_EXSTYLE)
            win().set_window_long(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
        except Exception:
            pass
        try:
            win().set_window_pos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )
        except Exception:
            pass

    state = {
        "shown": False,
        "last_xy": None,          # 最近一次贴边 MoveWindow 的目标（仅贴边用）
        "last_info": None,
        "db_read_count": 0,
        "cur_w": WINDOW_W,        # 当前生效窗口宽（自适应 + 防抖后的值）
        # ---- 配置热加载 ----
        "cfg_mtime": None,        # statusbar-config.json 上次读取的 mtime
        "refresh_ms": refresh_ms, # 当前生效刷新间隔（热加载可更新）
        # ---- 拖动状态（与贴边 last_xy 互相独立，互不污染）----
        "dragging": False,        # 正在拖动（按下->释放）
        "drag_offset": None,      # (x_root - winfo_rootx, y_root - winfo_rooty)
        "manual_position": False, # 拖动过 -> poll 跳过吸回
        "manual_xy": None,        # 拖动后的稳定位置（供 re_dock 前保持）
        "close_box": None,        # close 小块的 Canvas 坐标（拖动按下时排除）
        # ---- 靠边收起（collapsed 把手）----
        "collapsed": bool(cfg.get("collapsed", False)),  # 启动按配置进收起/展开态
        "manual_handle": False,   # 收起态把手被手动拖过 -> poll 不再吸回（直到展开）
        "press_xy": None,         # 完整态按下时指针屏幕坐标（区分「单击」与「拖动」）
        "last_drag_xy": None,     # 收起态拖动最近一次被 clamp 后的目标坐标
        "handle_drag_offset": None,   # 收起态本次拖动的抓取偏移（一次拖拽内冻结）
        "hover_grace_until": 0,   # 收起冷却截止（epoch 毫秒）：此之前悬停展开不排程
        "ignore_click_until": 0,  # 双击尾巴遮蔽截止（epoch 毫秒）：此之前收起态
                                  # 释放事件不喂手势机（防收起后残余 release 又展开）
        "handle_armed": False,    # 悬停展开武装标志：需 leave->enter 才重新武装
        # ---- 生命周期绑定（0.9.3）----
        "zcode_gone_since": None,  # ZCode 进程首次被判缺席的时刻（epoch 毫秒）；
                                   # 进程一恢复立即清零（升级/重装的短空窗不退出）
    }

    # ---- 0.8.0 事件驱动刷新：目录监听 + 事件队列 ----
    # 队列在 Tk 主线程创建；watcher 回调在**工作线程**里只准 put 标记，绝不
    # 触碰任何 Tk 对象（Tkinter 非线程安全）；排空与刷新都在主线程做
    # （poll_fs_events，每 FS_POLL_MS 一次）。监听不可用时静默退回纯轮询。
    event_queue = queue.Queue()
    watcher = None
    if dir_watcher is not None and getattr(dir_watcher, "AVAILABLE", False):
        def _on_fs_change(name):
            # 此回调在 watcher 工作线程里执行：只准往队列放标记，
            # 绝不触碰任何 Tk 对象（Tkinter 非线程安全）
            try:
                event_queue.put(name)
            except Exception:
                pass
        try:
            watcher = dir_watcher.DirWatcher(
                data_dir, on_change=_on_fs_change,
                names=set(WATCH_NAMES), debounce_ms=60)
            watcher.start()
        except Exception:
            # 监听不可用不致命：静默退回纯轮询（1 秒兜底仍在跑）
            _log_err(data_dir, "dir_watcher start failed:\n%s"
                     % traceback.format_exc())
            watcher = None
    hover_expand_id = None        # 把手悬停自动展开的 after 计时器（cancel 用）

    # ---- 收起把手手势状态机（单击/双击/拖动裁决）----
    # 注入 GUI 的 after / after_cancel / 副作用回调；函数体在调用期解析
    # （expand_bar 等定义在下方，闭包捕获变量名即可）。

    def _gesture_after(ms, fn):
        def _wrapped():
            try:
                fn()
            except Exception:
                try:
                    _log_err(data_dir, "handle gesture callback error:\n%s"
                             % traceback.format_exc())
                except Exception:
                    pass
        try:
            return root.after(ms, _wrapped)
        except Exception:
            return None

    def _gesture_click_expand():
        """单击确认展开（手势状态机 _fire_click -> on_expand）：source
        =="click"。**不受收起冷却窗拦截**——单击是用户主动明确意图，收起
        冷却窗内点一下立即展开（0.1.6 把点击来源一并拦掉导致「小把手点
        不开」）。防自恢复不改由手势机侧兜住：双击第二次抬起
        （in_double_press）不武装单击待定，残余 release 不再走 click 路径。"""
        try:
            expand_bar(source="click")
        except Exception:
            try:
                _log_err(data_dir, "handle gesture expand error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    def _gesture_persist(x, y):
        # 拖动结束 -> 持久化把手坐标（原子写），poll 不再吸回直到展开
        cfg["handle_x"] = int(x)
        cfg["handle_y"] = int(y)
        state["manual_handle"] = True
        save_config_keys(config_path, cfg, data_dir, ["handle_x", "handle_y"])
        _sync_cfg_mtime()

    hand_gesture = GestureState(
        after=_gesture_after,
        after_cancel=lambda i: root.after_cancel(i),
        now=time_ms,
        on_expand=_gesture_click_expand,
        on_persist=_gesture_persist,
    )

    # 记录配置文件初始 mtime（热加载基线；文件暂不存在为 None）
    try:
        state["cfg_mtime"] = os.path.getmtime(config_path)
    except Exception:
        state["cfg_mtime"] = None

    def _after(ms, fn):
        """root.after 包装：回调异常写 err 日志后继续，绝不断刷新/轮询循环。"""
        def _wrapped():
            try:
                fn()
            except Exception:
                try:
                    _log_err(data_dir, "statusbar after-callback error:\n%s"
                             % traceback.format_exc())
                except Exception:
                    pass
        try:
            root.after(ms, _wrapped)
        except Exception:
            pass

    # ---- tooltip（单一全局 toplevel 复用，跟随鼠标）----
    tip = None
    tip_after_id = None

    def _ensure_tip():
        nonlocal tip
        if tip is None:
            try:
                tip = tk.Toplevel(root)
                tip.withdraw()
                tip.overrideredirect(True)
                try:
                    tip.attributes("-topmost", True)
                except Exception:
                    pass
                tip.configure(bg=BG_SECOND)
                tip_lbl = tk.Label(
                    tip, text="", bg=BG_SECOND, fg=FG, font=FONT_DIM,
                    anchor="w", justify="left",
                    padx=8, pady=5, wraplength=520,
                )
                tip_lbl.pack()
                tip.tip_lbl = tip_lbl
            except Exception:
                tip = None
        return tip

    def _tip_visible():
        try:
            return tip is not None and tip.winfo_ismapped()
        except Exception:
            return False

    def show_tooltip(text, x, y):
        """显示 tooltip：落在小条**外侧**（优先上方），并按小条所在显示器的工作
        区夹紧；工作区取不到时退回主屏。摆放规则见纯函数 tooltip_placement。"""
        t = _ensure_tip()
        if t is None:
            return
        try:
            t.tip_lbl.config(text=text)
            t.update_idletasks()
            tw = t.winfo_reqwidth()
            th = t.winfo_reqheight()
            bar_rect = window_rect_of(hwnd) if hwnd else None
            wa = work_area_of_rect(bar_rect) if bar_rect else None
            if not wa:
                wa = (0, 0, t.winfo_screenwidth(), t.winfo_screenheight())
            tx, ty = tooltip_placement(tw, th, x, y, bar_rect, wa)
            t.geometry("+%d+%d" % (tx, ty))
            t.deiconify()
            t.lift()
        except Exception:
            pass

    def hide_tooltip():
        try:
            if tip is not None:
                tip.withdraw()
        except Exception:
            pass

    def tooltip_enter(text):
        """悬停进入：延时 ~400ms 后显示（避免乱闪）。"""
        nonlocal tip_after_id
        try:
            if tip_after_id is not None:
                root.after_cancel(tip_after_id)
        except Exception:
            pass
        try:
            tip_after_id = root.after(400, lambda: _show_tip_at_pointer(text))
        except Exception:
            tip_after_id = None

    def _show_tip_at_pointer(text):
        try:
            show_tooltip(text, root.winfo_pointerx(), root.winfo_pointery())
        except Exception:
            pass

    def tooltip_leave():
        """悬停离开：立即隐藏并取消延时任务。"""
        nonlocal tip_after_id
        try:
            if tip_after_id is not None:
                root.after_cancel(tip_after_id)
                tip_after_id = None
        except Exception:
            pass
        hide_tooltip()

    def bind_hover(tag, tip_text, rect_id=None, base_fill=None, hover_fill=None):
        """Canvas tag 级悬停：块背景高亮 + tooltip（延时显示/跟随/离开隐藏）。"""
        def enter(_e):
            if rect_id is not None and hover_fill is not None:
                try:
                    canvas.itemconfigure(rect_id, fill=hover_fill)
                except Exception:
                    pass
            if tip_text:
                tooltip_enter(tip_text)

        def leave(_e):
            if rect_id is not None and base_fill is not None:
                try:
                    canvas.itemconfigure(rect_id, fill=base_fill)
                except Exception:
                    pass
            tooltip_leave()

        def motion(e):
            if tip_text and _tip_visible():
                show_tooltip(tip_text, e.x_root, e.y_root)

        canvas.tag_bind(tag, "<Enter>", enter)
        canvas.tag_bind(tag, "<Leave>", leave)
        canvas.tag_bind(tag, "<Motion>", motion)

    def _draw_close(win_w):
        """右上「×」close 小块（圆角底 + hover 红 + 点击退出；每次重画时重建）。
        win_w 为本帧生效窗口宽（自适应）。"""
        cw, ch = 24, 16
        x1 = win_w - 10
        x0 = x1 - cw
        y0, y1 = 4, 4 + ch
        rect = round_rect(canvas, x0, y0, x1, y1, 5, fill=BG_SECOND, outline="")
        ctxt = canvas.create_text((x0 + x1) / 2.0, (y0 + y1) / 2.0,
                                  text=u"\u00d7", font=FONT_CLOSE,
                                  fill=FG_DIM, tags=("close",))
        state["close_box"] = (x0, y0, x1, y1)

        def enter(_e):
            try:
                canvas.itemconfigure(rect, fill=CLOSE_HOVER_BG)
                canvas.itemconfigure(ctxt, fill=CLOSE_HOVER_FG)
            except Exception:
                pass

        def leave(_e):
            try:
                canvas.itemconfigure(rect, fill=BG_SECOND)
                canvas.itemconfigure(ctxt, fill=FG_DIM)
            except Exception:
                pass

        canvas.tag_bind("close", "<Enter>", enter)
        canvas.tag_bind("close", "<Leave>", leave)

        def on_click(_e):
            quit_app()
            return "break"   # 阻断 Canvas 级拖动绑定

        canvas.tag_bind("close", "<Button-1>", on_click)

    def _avail_work_w():
        """窗口宽上限 = 所在显示器工作区宽 - 16px；取不到工作区退回屏幕宽。"""
        try:
            if hwnd:
                rect = window_rect_of(hwnd)
                if rect:
                    wa = work_area_of_rect(rect)
                    if wa and wa[2] > wa[0]:
                        return (wa[2] - wa[0]) - 16
        except Exception:
            pass
        try:
            return root.winfo_screenwidth() - 16
        except Exception:
            return WINDOW_W

    def _apply_width(new_w):
        """窗口宽自适应落地（带 ±WIDTH_HYSTERESIS 防抖）：
        - 变化小于阈值 -> 保持当前宽（数字位数跳动不引起窗口频繁缩放闪烁）；
        - cur_w 为 None（收起<->展开刚切换）-> 跳过防抖强制生效；
        - 拖动中 -> 不改尺寸（避免干扰拖动；释放后下一拍渲染补上）；
        - 生效时只改宽度、保持左上角不动（贴边模式 poll 下一拍按新宽重新居中，
          手动定位模式位置完全不受影响）。
        返回本帧实际生效宽度（防抖后可能与 new_w 不同，渲染以返回值为准）。"""
        cur = state.get("cur_w")
        if cur is not None and abs(new_w - cur) < WIDTH_HYSTERESIS:
            return cur
        if state.get("dragging"):
            return (cur if cur is not None else new_w)
        state["cur_w"] = new_w
        try:
            canvas.config(width=new_w)
        except Exception:
            pass
        try:
            if hwnd:
                xy = current_window_xy()
                if xy:
                    win().move_window(hwnd, xy[0], xy[1], new_w, WINDOW_H, True)
            else:
                root.geometry("%dx%d+0+0" % (new_w, WINDOW_H))
        except Exception:
            pass
        return new_w

    # ---- 靠边收起：小把手（collapsed 模式）----
    # 状态机：state["collapsed"] 单一事实源；收起入口 = 双击状态条任意区域 /
    # 右键「收起到边缘」；展开 = 把手悬停 ~0.5s（Move 刷新计时）或单击；
    # 切换即原子写回配置（save_config_keys），重启按 collapsed 字段恢复。

    def _cancel_hover_expand():
        """取消把手悬停自动展开的计时器（离开把手 / 已展开时调用）。"""
        nonlocal hover_expand_id
        try:
            if hover_expand_id is not None:
                root.after_cancel(hover_expand_id)
        except Exception:
            pass
        hover_expand_id = None

    def _schedule_hover_expand():
        """（重新）启动悬停 0.5s 自动展开计时（Move 事件刷新 = 重置计时）。

        守卫（互斥清晰，判定抽为模块级 hover_expand_should_schedule）：
          - 收起冷却期内（collapse_bar 后 COLLAPSE_HOVER_GRACE_MS 内）不排程
            —— 防双击收起后把手恰在指针下的立即自展开；
          - 未武装（handle_armed=False，收起瞬间被 disarm）不排程 ——
            悬停展开要求指针先离开把手再重新进入（leave->enter）才再次武装；
          - 手势按压/拖动进行中不排悬停展开（按下即取消悬停，避免悬停展开与
            单击/双击/拖动裁决冲突）。
        排程成功后 consume 武装（handle_armed=False），一次 leave->enter 只武装一次。"""
        nonlocal hover_expand_id
        if not hover_expand_should_schedule(state, time_ms(),
                                            hand_gesture.press_xy is not None,
                                            hand_gesture.dragging):
            return
        state["handle_armed"] = False   # 武装只消费一次
        _cancel_hover_expand()
        try:
            hover_expand_id = root.after(HANDLE_HOVER_MS,
                                         lambda: expand_bar(source=None))
        except Exception:
            hover_expand_id = None

    def _sync_cfg_mtime():
        """写回配置后同步热加载基线 mtime，避免下一拍重读同值文件。"""
        try:
            state["cfg_mtime"] = os.path.getmtime(config_path)
        except Exception:
            pass

    def collapse_bar():
        """收起：完整状态条 -> 小把手。清完整态手动定位/清理把手手势待定，
        持久化 collapsed=true，立即以把手尺寸重画。把手停靠位由 poll 决定：
        无记忆 -> 工作区底部居中；有记忆 -> 记忆位置（越界回退右下角）。"""
        if state.get("collapsed"):
            return
        state["collapsed"] = True
        cfg["collapsed"] = True
        save_config_keys(config_path, cfg, data_dir, ["collapsed"])
        _sync_cfg_mtime()
        hide_tooltip()
        _cancel_hover_expand()
        hand_gesture.cancel_click()          # 收起瞬间清理未决单击待定/拖动态
        # 双击尾巴遮蔽：双击最后一次抬起的残余释放事件若在收起后仍到达手势机
        # （此时 cancel_click 已把 in_double_press 清为 False，release 会误走
        # _arm_click -> 250ms 后 click 路径展开，把刚收起的条又拉回完整态），
        # 这里置遮蔽窗：窗口内下次 release 不再喂手势机，双击尾巴被吃掉。
        state["ignore_click_until"] = time_ms() + DOUBLE_CLICK_TAIL_MS
        # 防自恢复：收起后进入悬停展开冷却窗，并把把手 disarm —— 把手恰在
        # 指针下也不会 500ms 后自展开；需 leave->enter 且过冷却后才恢复悬停展开。
        state["hover_grace_until"] = time_ms() + COLLAPSE_HOVER_GRACE_MS
        state["handle_armed"] = False
        # 完整态默认贴边（manual_position 是完整态拖动记忆；收起态不沿用）
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["close_box"] = None
        try:
            render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(data_dir, "render after collapse error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    def expand_bar(source=None):
        """展开：把手 -> 完整状态条（取简单：重新贴边 ZCode 底部，不还原
        收起前的手动位置）。持久化 collapsed=false，清掉把手手动记忆（下次
        收起按记忆位置重新出现；本次展开后把手坐标记忆保留在配置文件）。
        恢复完整条高度并强制宽度生效（cur_w=None 绕过防抖）。

        source 区分展开路径（冷却守卫对点击来源豁免）：
          - 悬停计时（默认/None）与右键菜单（"menu"）：仍受收起冷却守卫——
            刚收起 COLLAPSE_HOVER_GRACE_MS 内不展开，避免双击收起的同桌
            手势（残留轻按把手）+ 把手恰在指针下 500ms 悬停自展开，把
            状态条又拉回完整态（防自恢复）。
          - 单击确认（_gesture_click_expand 经手势状态机 _fire_click ->
            on_expand 传入 "click"）：**不受冷却拦截**——单击是用户主动明确
            意图，收起冷却窗内点一下必须立即展开（0.1.6 把点击来源也一并
            拦掉导致「收起后小把手怎么点都没反应」）；防自恢复的双击尾巴
            由手势机侧守卫兜住：双击第二次抬起（in_double_press）不会
            武装单击待定，残余 release 落在把手上也不再走 click 路径展开。"""
        if not state.get("collapsed"):
            return
        if source != "click" and time_ms() < state.get("hover_grace_until", 0):
            return  # 非点击来源在收起冷却窗内不展开（悬停/菜单防自恢复）
        state["collapsed"] = False
        cfg["collapsed"] = False
        save_config_keys(config_path, cfg, data_dir, ["collapsed"])
        _sync_cfg_mtime()
        hide_tooltip()
        _cancel_hover_expand()
        hand_gesture.cancel_click()
        state["ignore_click_until"] = 0   # 展开后清双击尾巴遮蔽（防残留遮蔽正常点击）
        state["manual_handle"] = False   # 展开后清标志：下次收起重新按记忆位置
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["hover_grace_until"] = 0   # 展开后清冷却：本次会话下次收起重新计时
        state["handle_armed"] = False    # 展开后 disarm：下次收起仍需 leave->enter
        state["cur_w"] = None      # 绕过宽度防抖：72 -> ~620 必须立刻生效
        try:
            canvas.config(height=WINDOW_H)  # 恢复完整条高度（把手态是 HANDLE_H）
        except Exception:
            pass
        try:
            render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(data_dir, "render after expand error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    def _apply_handle_geometry(new_w):
        """收起把手尺寸落地：cur_w 直接生效（把手宽稳定，无需防抖），窗口
        高度切到 HANDLE_H；位置本函数不动（poll 下一拍按 collapsed 停靠位
        贴屏幕底部），仅保持当前左上角。"""
        state["cur_w"] = new_w
        try:
            canvas.config(width=new_w, height=HANDLE_H)
        except Exception:
            pass
        try:
            if hwnd:
                xy = current_window_xy()
                if xy:
                    win().move_window(hwnd, xy[0], xy[1], new_w, HANDLE_H, True)
            else:
                root.geometry("%dx%d+0+0" % (new_w, HANDLE_H))
        except Exception:
            pass

    def _render_handle(info):
        """收起态渲染（render_ui 的 collapsed 分支调用；canvas 已清空）：
        ~72x18 深底小把手 + 顶部 1px 分隔线 + 「◐ 84.9%」浓缩缓存命中率
        （绿字）；悬停 0.5s / 单击展开（单击经 drag_stop 位移判定走 expand）。"""
        stats = (info or {}).get("stats")
        hit_pct = _hit_rate(stats) if stats else 0.0
        hit_txt = (u"%.1f%%" % hit_pct) if stats else u"—"
        # ◐ 图标颜色跟随当前状态色（STATUS_COLORS）：收起态也能看出当前状态；
        # 命中率数字按命中率档位分色（cache_hit_color，与展开态口径一致）。
        icon_color = handle_status_color(info)
        w = BLOCK_PAD * 2 + f_icon.measure(ICON_HIT) + 5 + f_num.measure(hit_txt)
        _apply_handle_geometry(w)
        canvas.create_rectangle(0, 0, w, 1, fill=EDGE_LINE, outline="")
        tx = BLOCK_PAD
        cy = HANDLE_H / 2.0
        canvas.create_text(tx, cy, text=ICON_HIT, font=ICON_FONT,
                           fill=icon_color, anchor="w", tags=("hdl",))
        tx += f_icon.measure(ICON_HIT) + 5
        canvas.create_text(tx, cy, text=hit_txt, font=FONT_NUM,
                           fill=cache_hit_color(hit_pct), anchor="w",
                           tags=("hdl",))

        def _enter(_e):
            # 仅当已武装（leave->enter 后）才排悬停展开；arm 事件会立刻 consume，
            # 冷却期内的 enter 因冷却守卫不排程，小幅度移动不重复武装。
            if state.get("handle_armed", False):
                _schedule_hover_expand()      # 悬停 0.5s 自动展开
            tooltip_enter(HANDLE_TIP)         # 「已收起——悬停或单击展开」

        def _motion(e):
            if state.get("handle_armed", False):
                _schedule_hover_expand()      # Move 刷新（重置）展开计时
            if _tip_visible():
                show_tooltip(HANDLE_TIP, e.x_root, e.y_root)

        def _leave(_e):
            _cancel_hover_expand()
            state["handle_armed"] = True      # 离开把手 -> 重新武装（下次进入可展开）
            tooltip_leave()

        canvas.tag_bind("hdl", "<Enter>", _enter)
        canvas.tag_bind("hdl", "<Motion>", _motion)
        canvas.tag_bind("hdl", "<Leave>", _leave)

    def render_ui(info):
        """整幅重画（0.4.0 对话级三区：同一回调内 delete+create，Tk 单次刷帧
        无闪烁）：顶部 1px 分隔线 + 第一行（状态徽标 + 对话名）+ 第二行
        （本轮统计）+ 右侧小字（会话累计）+ close 小块。
        窗口宽按内容自适应（plan_statusbar_layout_3zone）：状态徽标 / 本轮
        统计 / 累计小字**永不因宽度裁剪**，超上限时依次收对话名 -> 缩间距 ->
        省累计标注。"""
        canvas.delete("all")
        if state.get("collapsed"):
            # 收起态：只画底部小把手（◐ 命中率），几何走 HANDLE_H 分支
            _render_handle(info)
            return
        show_status = cfg.get("show_status", True)
        show_turn = cfg.get("show_recent_turn", True)
        show_cum = cfg.get("show_cumulative", True)
        status = info.get("status") or "idle"
        speed = info.get("status_speed")
        model = info.get("model")
        label = info.get("session_label")
        turn = info.get("turn_stats")
        cum = info.get("stats")

        _fmap = {FONT_MAIN: f_main, FONT_NUM: f_num, FONT_DIM: f_dim,
                 ICON_FONT: f_icon}

        def _turn_segments(ts):
            """本轮统计分段（(text, font, fg) 列表）；无数据返回单段占位。
            0.5.0：生成中时无论 speed 是否有值，尾部追加实时速度或占位文案
            「生成中…」，确保首 token 前用户也看到生成中状态。
            0.9.0：数字来源三态标注——live（本轮 model_usage 实时聚合）/
            stale_turn（本轮尚未落库，展示的是上一轮）/ 无标记（本轮权威行）。
            不标注就无法区分「本轮跑了 3s」和「上一轮 3s」，而这正是
            「生成中却显示上一轮数字」被误读为统计错误的地方。"""
            if not ts:
                segs = [(TURN_PENDING, FONT_MAIN, FG_DIM)]
            else:
                inp = int(ts.get("inputTokens") or 0)
                cache_rd = int(ts.get("cacheReadTokens") or 0)
                hit = (cache_rd / float(inp) * 100.0) if inp > 0 else 0.0
                segs = [
                    (ICON_DUR, ICON_FONT, FG_DIM),
                    (u"%.1fs" % ((ts.get("durationMs") or 0) / 1000.0),
                     FONT_NUM, FG),
                    (u" \u00b7 in ", FONT_MAIN, FG_DIM),
                    (format_tokens(inp), FONT_NUM, FG),
                    (u" \u00b7 out ", FONT_MAIN, FG_DIM),
                    (format_tokens(ts.get("outputTokens") or 0), FONT_NUM, FG),
                    (u" \u00b7 cache hit ", FONT_MAIN, FG_DIM),
                    (u"%.1f%%" % hit, FONT_NUM, FG),
                ]
                # 本轮工具调用数（0.9.2）
                segs.extend(turn_tool_segments(ts))
                if ts.get("live"):
                    segs.append((u" \u00b7 \u5b9e\u65f6", FONT_MAIN, ACCENT_GREEN))
                elif ts.get("stale_turn"):
                    segs.append((u" \u00b7 \u4e0a\u4e00\u8f6e", FONT_MAIN, FG_DIM))
            # 0.9.1：速度段扩到整个 busy 族（工具中也显示），并在轮次收尾后短时
            # 保留最后已知值——口径与各分支收在 speed_display 里，见其文档。
            segs.extend(speed_display(
                status, speed, info.get("status_speed_held"),
                cfg.get("show_live", True), cfg.get("show_speed", True)))
            # 数据异常标注（Stage 1）：info["error"] 有值（数据组装/刷新落错）时
            # 在第二行尾部追加「（数据异常）」——该字段此前无任何消费者。
            if info.get("error"):
                segs.append((DATA_ANOMALY_NOTE, FONT_MAIN, FG_DIM))
            return segs

        def _draw_turn_stats(x, y, ts):
            """第二行：本轮统计分段绘制（数字等宽防跳字）+ 整行悬停 tooltip。"""
            segs = _turn_segments(ts)
            for t, f, c in segs:
                canvas.create_text(x, y, text=t, font=f, fill=c,
                                   anchor="w", tags=("m_turn",))
                x += _fmap[f].measure(t)
            bind_hover("m_turn", turn_tooltip(ts))

        # ---- 文本拼装 ----
        badge_txt = status_badge_text(status, turn)
        # 0.4.3：tok/s 不再放第一行徽标旁（0.4.2 曾在此以小字灰字显示，用户
        # 反馈太弱找不到），改到第二行本轮统计尾部（见 _turn_segments）。
        turn_segs = _turn_segments(turn)
        turn_w = sum(_fmap[f].measure(t) for t, f, _ in turn_segs)
        cum_txt = cumulative_text(cum, cfg.get("show_cache_hit", True))
        has_note = bool(cum and info.get("recent_note"))

        # ---- 先实测、后布局（三区宽度全部 tkinter.font 实测）----
        badge_w = 0
        if show_status:
            badge_w = BADGE_PAD_X * 2 + f_main.measure(badge_txt)
        cum_w = f_dim.measure(cum_txt) if (show_cum and cum_txt) else 0
        note_w = f_dim.measure(SESSION_RECENT_NOTE) if has_note else 0
        # 模型名并入不可裁槽（与徽标同槽：徽标+模型名恒不裁，对话名按剩余宽截短）
        if cfg.get("show_model", True) and model:
            badge_w += f_dim.measure(_truncate(model, 20) or "model?") + 8

        def _title_w(lm):
            if not label:
                return 0
            return f_dim.measure(_truncate(label, lm) or u"")

        plan = plan_statusbar_layout_3zone(badge_w, _title_w, turn_w, cum_w,
                                           note_w, _avail_work_w())
        win_w = _apply_width(plan["win_w"])
        badge_gap = plan["badge_gap"]
        show_cum_now = bool(plan["show_cum"] and cum_txt)
        show_note_now = bool(plan["show_note"] and has_note)

        # 顶部 1px 分隔线（提质感）
        canvas.create_rectangle(0, 0, win_w, 1, fill=EDGE_LINE, outline="")

        # ---- 第一行：状态徽标（色块 + 深色粗体字）+ 模型名（蓝）+ 对话名 ----
        x = 12
        if show_status:
            color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
            badge_only_w = BADGE_PAD_X * 2 + f_main.measure(badge_txt)
            rect = round_rect(canvas, x, ROW1_CY - BADGE_H / 2.0,
                              x + badge_only_w, ROW1_CY + BADGE_H / 2.0, 9,
                              fill=color, outline="")
            canvas.create_text(x + badge_only_w / 2.0, ROW1_CY, text=badge_txt,
                               font=BADGE_FONT, fill=BADGE_FG,
                               anchor="center", tags=("m_badge",))
            bind_hover("m_badge",
                       status_badge_tip(status, turn),
                       rect, color, color)
            x += badge_only_w
            x += badge_gap
        if cfg.get("show_model", True) and model:
            mtxt = _truncate(model, 20) or "model?"
            canvas.create_text(x, ROW1_CY, text=mtxt, font=FONT_DIM,
                               fill=ACCENT_BLUE, anchor="w", tags=("m_model",))
            x += f_dim.measure(mtxt) + 8
            bind_hover("m_model", u"模型：%s" % model)
        if label:
            canvas.create_text(x, ROW1_CY,
                               text=_truncate(label, plan["label_max"])
                               or u"\uff08\u672a\u547d\u540d\u4f1a\u8bdd\uff09",
                               font=FONT_DIM, fill=FG_DIM, anchor="w",
                               tags=("m_sess",))
            tip = u"\u4f1a\u8bdd\uff1a%s" % label
            if model:
                tip = tip + u"\n\u6a21\u578b\uff1a%s" % model
            bind_hover("m_sess", tip)
        elif not show_status:
            # fail-closed：徽标也关掉且无对话名 -> 会话未识别占位
            canvas.create_text(12, ROW1_CY, text=SESSION_UNKNOWN,
                               font=FONT_DIM, fill=FG_DIM, anchor="w")

        # ---- 右上 close 小块 ----
        _draw_close(win_w)

        # ---- 第二行：本轮统计（左侧）+ 会话累计小字（右侧）----
        if show_turn:
            _draw_turn_stats(10, ROW2_TURN_Y, turn)
        if show_cum_now:
            # 累计小字右对齐（close 小块左侧留白），jsonl 兜底判定时左侧
            # 附「（最近会话累计）」标注（超宽时布局先省略标注，不裁统计）。
            cx = win_w - CLOSE_RESERVE_W - 6
            canvas.create_text(cx, ROW2_TURN_Y, text=cum_txt, font=FONT_DIM,
                               fill=FG_DIM, anchor="e", tags=("m_cum",))
            bind_hover("m_cum", cum_tooltip(cum))
            if show_note_now:
                canvas.create_text(cx - f_dim.measure(cum_txt) - 6,
                                   ROW2_TURN_Y, text=SESSION_RECENT_NOTE,
                                   font=FONT_DIM, fill=FG_DIM, anchor="e",
                                   tags=("m_cum_note",))

    def current_window_xy():
        """取小条当前屏幕坐标 (x, y)；失败返回 None。"""
        try:
            if not hwnd:
                return None
            wid = root.winfo_id()
            child_hwnd = win().get_parent(wid) or wid
            if not child_hwnd:
                return None
            rect = wintypes.RECT()
            if win().get_window_rect(child_hwnd, ctypes.byref(rect)):
                return rect.left, rect.top
        except Exception:
            pass
        return None

    def _default_dock_xy(bar_w):
        """坐标/工作区瞬时不可得时的回退停靠（0.2.2）：优先小条自身所在
        显示器工作区（get_window_rect 直取顶层句柄——与 current_window_xy
        的 child 解析失败场景互补），再退主显示器（_primary_work_area）；
        仍失败返回 None。定位用生产 default_handle_xy（工作区底部居中）。"""
        wa = None
        try:
            rect = wintypes.RECT()
            if hwnd and win().get_window_rect(hwnd, ctypes.byref(rect)):
                wa = work_area_of_rect((rect.left, rect.top,
                                        rect.right, rect.bottom))
        except Exception:
            wa = None
        if wa is None:
            wa = _primary_work_area()
        if wa is None:
            return None
        return default_handle_xy(wa, bar_w)

    def drag_start(event):
        """按下：完整态（未收起）延用偏移拖动；收起态走手势状态机 press
        （记录坐标+时间，无动作；250ms 内第二次按下到达会取消单击待定）。
        close 小块上的按下走退出逻辑，不进入任何拖动。"""
        if state.get("dragging") or hand_gesture.dragging:
            return
        # close 小块上的按下走退出逻辑（把手态无 close，恒 None）
        cb = state.get("close_box")
        if cb and cb[0] <= event.x <= cb[2] and cb[1] <= event.y <= cb[3]:
            return
        if state.get("collapsed"):
            _cancel_hover_expand()   # 按下即取消悬停展开，交手势裁决
            # 新的一次按下 = 新的一次抓取：作废上次拖动的偏移，避免复用陈旧
            # 抓取量（release 不按时到达时尤其要紧）。
            state["handle_drag_offset"] = None
            hand_gesture.press(event.x_root, event.y_root)
            return
        xy = current_window_xy()
        if xy is None:
            return
        state["dragging"] = True
        state["press_xy"] = (event.x_root, event.y_root)
        state["drag_offset"] = (event.x_root - xy[0], event.y_root - xy[1])

    def drag_move(event):
        """按住左键移动：
        - 收起态 -> 手势状态机裁决：位移 >3px 才进入拖动态跟随移动
          （仍 clamp_to_work_area；未达阈值不触发，保持单击/双击待判）；
        - 完整态 -> 原偏移拖动跟随（不受手势状态机约束）。"""
        if state.get("collapsed"):
            act = hand_gesture.motion({"x_root": event.x_root,
                                       "y_root": event.y_root})
            if act != E_ACTION_DRAG:
                return
            # 拖动态：目标坐标 clamp_to_work_area（以目标位置构造伪矩形
            # 定显示器），防止拖出屏幕无法自救，移动后记录把手位置。
            bar_w = state.get("cur_w") or HANDLE_W_DEFAULT
            bar_h = HANDLE_H
            # 抓取偏移在一次拖拽内冻结（0.9.6 前每帧现算 -> new == 当前位置，
            # 恒等式，把手拖不动）。
            off = drag_grab_offset((event.x_root, event.y_root),
                                   current_window_xy(),
                                   state.get("handle_drag_offset"))
            if off is None:
                return            # 窗口坐标瞬时取不到：本帧不移动
            state["handle_drag_offset"] = off
            new_x, new_y = drag_target_xy((event.x_root, event.y_root), off,
                                          bar_w, bar_h, clamp_to_work_area)
            try:
                if hwnd:
                    win().move_window(hwnd, new_x, new_y, bar_w, bar_h, True)
            except Exception:
                return
            state["last_drag_xy"] = (new_x, new_y)
            return
        if not state.get("dragging"):
            return
        off = state.get("drag_offset")
        if not off:
            return
        bar_w = state.get("cur_w") or WINDOW_W
        bar_h = WINDOW_H
        new_x, new_y = drag_target_xy((event.x_root, event.y_root), off,
                                      bar_w, bar_h, clamp_to_work_area)
        try:
            if hwnd:
                win().move_window(hwnd, new_x, new_y, bar_w, bar_h, True)
        except Exception:
            return
        state["manual_xy"] = (new_x, new_y)

    def drag_stop(event):
        """释放：
        - 收起态 -> 手势状态机裁决：拖动态 -> on_persist 持久化把手坐标
          （handle_x/handle_y，原子写，poll 不再吸回）；双击第二次 -> 不动作；
          单击 -> 排 250ms 待定展开（第二次按下到来前），时间到才展开一次。
        - 完整态 -> 结束拖动、标记手动定位（poll 不再吸回），记录稳定位置。"""
        if state.get("collapsed"):
            cur_xy = state.get("last_drag_xy")
            # 双击尾巴遮蔽：收起后 DOUBLE_CLICK_TAIL_MS 内（collapse_bar 置的
            # ignore_click_until）到达的释放事件——双击收起的残余 Release#2——
            # 直接取消待定并丢弃，不喂手势机（否则 in_double_press 已被
            # cancel_click 清掉，release 误走 _arm_click，250ms 后 click 路径
            # 展开，收起被弹回）。
            if time_ms() < state.get("ignore_click_until", 0):
                hand_gesture.cancel_click()
                state["last_drag_xy"] = None
                return
            hand_gesture.release({"x_root": event.x_root,
                                  "y_root": event.y_root}, cur_xy)
            state["last_drag_xy"] = None
            # 拖动裁决（E_ACTION_PERSIST）不必在这里再处理：release() 内部已按
            # cur_xy 回调 on_persist 写过 handle_x/handle_y 并置 manual_handle
            # =True，poll 下一拍起不再吸回；拖动的落点本身就是真实位置。
            return
        if not state.get("dragging"):
            return
        state["dragging"] = False
        state["drag_offset"] = None
        state["press_xy"] = None
        state["manual_position"] = True
        try:
            xy = current_window_xy()
            if xy is not None:
                state["manual_xy"] = xy
        except Exception:
            pass

    def re_dock():
        """重新贴边：清除手动定位标志（完整态 manual_position 与收起态
        manual_handle），让 poll 下一拍恢复停靠/记忆位置。"""
        state["manual_position"] = False
        state["manual_xy"] = None
        state["last_xy"] = None
        state["manual_handle"] = False

    def toggle_show(key):
        """右键「显示项」开关：更新内存配置 -> 立即重画 -> 原子写回配置文件。
        写回失败不崩（save_config_show_keys 内已记 err 日志）；重画失败也只记日志。"""
        try:
            val = bool(show_vars[key].get())
        except Exception:
            return
        cfg[key] = val
        save_config_show_keys(config_path, cfg, data_dir)
        _sync_cfg_mtime()   # 写回后 mtime 已变：同步热加载基线，避免下一拍重读同值文件
        # 立即重画（不等下一拍刷新）；启动早期 last_info 可能仍是全 None dict
        try:
            render_ui(state.get("last_info") or {
                "text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None})
        except Exception:
            try:
                _log_err(data_dir, "render after toggle_show error:\n%s"
                         % traceback.format_exc())
            except Exception:
                pass

    # 拖动绑到 Canvas 全区域（含两行文字与各指标块；close 小块在 drag_start 内排除）
    canvas.bind("<ButtonPress-1>", drag_start)
    canvas.bind("<B1-Motion>", drag_move)
    canvas.bind("<ButtonRelease-1>", drag_stop)
    # 双击状态条任意区域 -> 收起到边缘（collapsed 把手）；收起态双击由手势
    # 状态机裁决（第二次抬起不作任何展开/收起，双击收起态无意义）
    canvas.bind("<Double-Button-1>",
                lambda _e: collapse_bar() if not state.get("collapsed") else None)

    def refresh_once():
        """单次数据组装 + 渲染（不排任何定时器）。异常契约：内部自吞
        （err 日志 + error 标记），绝不向上抛——供 1 秒兜底链
        （refresh_tick）与文件事件链（poll_fs_events）共同复用。"""
        info = {"text": None, "line1": None, "session_id": None,
                "model": None, "session_label": None, "stats": None}
        try:
            # 配置热加载：mtime 变化则重读（坏 JSON 保留当前配置 + err 日志一行）
            state["cfg_mtime"] = hot_reload_config(
                cfg, config_path, data_dir, state.get("cfg_mtime"))
            if not interval_fixed:
                try:
                    state["refresh_ms"] = max(
                        int(cfg.get("refresh_ms", REFRESH_MS_DEFAULT)), 200)
                except Exception:
                    pass
        except Exception:
            pass
        # 完全不可见（SW_HIDE）：配置热加载照走，数据组装（jsonl/DB 共 6-8
        # 条查询/拍）整段跳过。收起把手不在此列——把手 shown 恒为 True。
        if hidden_skip_refresh(state):
            return
        try:
            # N6 性能：逐秒全量解析改走指纹缓存，文件未变时复用上次结果
            rows, _err = read_jsonl_cached(os.path.join(data_dir, JSONL_NAME))
            # 会话判定改为信号驱动 + 粘滞（resolve_session_sticky，经
            # resolve_gui_info(sess_state=state)）：不再每拍读 mark 判变化。
            # model/title 缓存复用由 resolve_gui_info 内建 reuse 判定
            # （cur.session_id 与判定 sid 不同即重读）；此处仅保留周期性强刷
            # （db_read_count == 0 时每 DB_READ_INTERVAL 拍重读一次）。
            prev_info = state["last_info"]
            cur_cache = (None if (prev_info is None
                                  or state["db_read_count"] == 0)
                         else prev_info)
            # 0.6.0：状态徽标来源改为 ZCode 钩子时序 status-state.json
            # （status_event.py 写），不再读 live_stream / 不依赖代理；
            # 会话判定走 sticky 信号链。
            # 0.9.0：文档按会话分片，一拍只读一次原始文档；sid 要等
            # resolve_gui_info 判定出来，故取记录推迟到其后的 status_state_for。
            status_doc = None
            if cfg.get("show_live", True):
                status_doc = _read_status_state(data_dir)
            # R4 拍级连接复用：一拍数据组装（会话判定 + 统计 + 状态）共用
            # 同一只读连接，组装完毕 finally 关闭（链路内零次新开连接）。
            tick_conn = _db_connect(db_path)
            try:
                info = resolve_gui_info(rows, data_dir, db_path, cfg=cfg,
                                        cur=cur_cache,
                                        sess_state=state,
                                        db_conn=tick_conn)
                session_changed = (prev_info is not None
                                   and info.get("session_id") != prev_info.get("session_id"))
                if session_changed:
                    # 换会话 = 新的徽标上下文：清掉去抖驻留，新会话状态立即生效
                    state["badge_shown"] = None
                force_db = (prev_info is None
                            or session_changed
                            or state["db_read_count"] == 0)
                state["force_db"] = force_db  # 诊断用：model/title 强刷已由 cur_cache/reuse 覆盖
                # 钩子时序 -> 状态徽标；生成中时 speed 取 db_latest_speed 精确速度。
                # 只采信**本会话**的事件记录（status_state_for），别的会话的分片
                # 记录对本会话恒为 None -> 状态回落 idle。
                status, spd, turn_stats = resolve_turn_status(
                    status_state_for(status_doc, info.get("session_id")),
                    db_path, info.get("session_id"),
                    conn=tick_conn)
            finally:
                if tick_conn is not None:
                    try:
                        tick_conn.close()
                    except Exception:
                        pass
            info["status"] = status_debounce(state, status)
            # 0.9.1：busy 族取本轮速度；收尾后短时保留最后已知值（缓存随会话
            # 变化作废），held 标记让渲染层加「（上次）」。
            spd, held = speed_hold(state, info["status"], spd,
                                   info.get("session_id"))
            info["status_speed"] = spd
            info["status_speed_held"] = held
            info["turn_stats"] = turn_stats
        except Exception:
            # 不静默吞错（Stage 1）：落 err 日志 + 置 error 标记，绝不向上抛
            # （刷新循环不得中断；_log_err 内部已自吞异常）
            _log_err(data_dir, "refresh_once error:\n%s" % traceback.format_exc())
            info["error"] = "refresh"
        state["last_info"] = info
        state["db_read_count"] += 1
        if state["db_read_count"] >= DB_READ_INTERVAL:
            state["db_read_count"] = 0
        try:
            render_ui(info)
        except Exception:
            # 渲染层异常落 err 日志（pythonw 无 console，静默空白无法诊断）
            try:
                _log_err(data_dir, "render_ui error:\n%s" % traceback.format_exc())
            except Exception:
                pass

    def refresh_tick():
        # 全程序唯一的 1 秒排程点：兜底轮询链独占（事件驱动刷新缺失/
        # 队列消费链断裂时，数据最迟 1 秒后仍会更新）。refresh_once
        # 自吞异常不会断链；_after 包装再兜一层，绝不让链意外终止。
        refresh_once()
        _after(1000, refresh_tick)

    def poll_fs_events():
        """文件事件队列消费者：每 FS_POLL_MS 排空一次 event_queue，
        取到事件就合并刷新一次（一次 refresh_once，不逐事件刷新、
        不排 1 秒定时器）。自排队常驻；队列空时零开销轮转。"""
        drained = False
        while True:
            try:
                event_queue.get_nowait()
            except queue.Empty:
                break
            drained = True
        if drained:
            # watcher 不可用时队列恒空（drained 恒 False），本循环退化为
            # 每 FS_POLL_MS 一次的空转，纯轮询路径不受影响
            refresh_once()
        _after(FS_POLL_MS, poll_fs_events)

    def _apply_noactivate():
        """持续保障 WS_EX_NOACTIVATE（0.2.1）：创建后仅在 1960 一次设置过，
        拖动/点击激活会重入清除扩展样式（点击 pythonw 后窗口可被激活，导致
        前台判定/焦点行为异常）。每次显示前重新 Apply + SWP_NOACTIVATE。"""
        if not hwnd:
            return
        try:
            style = win().get_window_long(hwnd, GWL_EXSTYLE)
            if not (style & WS_EX_NOACTIVATE):
                win().set_window_long(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
            win().set_window_pos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )
        except Exception:
            pass

    def set_visible(show):
        if not hwnd:
            return
        if show == state["shown"]:
            return
        state["shown"] = show
        if show:
            # 恢复可见：强制下一拍全量重读（隐藏期间跳过数据组装，model/title
            # 等缓存可能陈旧），自然补读出新数据，不残留旧值
            state["db_read_count"] = 0
        try:
            if show:
                win().show_window(hwnd, SW_SHOWNOACTIVATE)
            else:
                win().show_window(hwnd, SW_HIDE)
        except Exception:
            pass

    def poll():
        # 生命周期绑定（0.9.3）：宿主 ZCode 进程真退出 -> 小条一起退出。
        # 放在最前面（连 hwnd 都没建成的那一拍也要判），且刻意只看进程表
        # 不看窗口——ZCode 最小化/收进托盘时窗口判定同样失败，那时只该由
        # 下面的显隐逻辑隐藏，不该把小条退出。
        quit_now, state["zcode_gone_since"] = zcode_gone_exit_due(
            zcode_app_running(), state.get("zcode_gone_since"), time_ms())
        if quit_now:
            _log_err(data_dir, "zcode 进程缺席满 %dms，小条随宿主退出"
                     % ZCODE_GONE_QUIT_MS)
            set_visible(False)
            try:
                # mainloop 返回 -> run_gui 的 finally 清 statusbar.pid，
                # 下次 ZCode 的 SessionStart 钩子据此判断该重开一条。
                root.quit()
            except Exception:
                pass
            return
        if not hwnd:
            _after(state.get("refresh_ms", refresh_ms), poll)
            return
        try:
            # 正在拖动：跳过前台/最小化/找窗等全部隐藏判定（拖动中绝不
            # withdraw，否则拖动会被自隐藏打断），也不贴边 MoveWindow；
            # 释放后恢复完整显隐判定 + manual_position 接管停靠位置。
            # 0.9.6：条件补上手势机的 hand_gesture.dragging（收起态拖动）。
            # 收起分支原先只判 state["dragging"]（完整态专用，收起态恒 False），
            # 于是 poll 每拍（~1 秒）都按记忆/默认位 MoveWindow 把手拽回去
            # ——拖动刚改好就会变成「拖一下、弹回一下」。
            if state.get("dragging") or hand_gesture.dragging:
                _apply_noactivate()   # 拖动中显示同样持续保障 NOACTIVATE
                set_visible(True)
                return
            collapsed = bool(state.get("collapsed"))
            if collapsed:
                # 收起态分支（0.2.0 与完整态彻底解耦）：
                # 收起把手**不依赖 ZCode 窗口查找**（用户 ZCode 标题是终端风格
                # 时精确标题/精确 exe 兜底都可能找不到，下一拍 poll 用 SW_HIDE
                # 会把双击收起后的把手藏掉 ->「能缩不能显示」）：
                #   - ZCode 进程真退出（_zcode_process_alive False）-> 藏；
                #   - 真窗口句柄可用且最小化（IsIconic）-> 藏；句柄拿不到
                #     （标题匹配不到/权限不足）则跳过该检查，绝不因此隐藏；
                #   - 否则走现有收起停靠（贴屏幕底部中心），set_visible(True)。
                # 0.2.2 根因修复：坐标/工作区判定**瞬时失败**（current_window_xy
                # 或 work_area_of_rect 返回 None）不再 SW_HIDE（曾导致「双击收起
                # 后把手消失」），回退默认停靠（工作区底部居中，default_handle_xy）
                # 并保持可见；判定抽为模块级 collapsed_poll_decision（依赖显式
                # 注入，可独立重放测试），本闭包只做副作用与诊断落盘。
                bar_w = state.get("cur_w") or HANDLE_W_DEFAULT

                def _collapsed_err(name):
                    # 同因连续失败只记一行（瞬时失败通常下一拍自愈；持续失败
                    # 也不刷屏），判定恢复后由成功路径清 last_collapse_err。
                    _throttled_collapsed_err(state, data_dir, name)

                visible, hxy = collapsed_poll_decision(
                    state, cfg, bar_w,
                    alive=_zcode_process_alive(),
                    zcode_hwnd=find_zcode_window(),
                    is_iconic=win().is_iconic,
                    current_xy=current_window_xy,
                    work_area=work_area_of_rect,
                    valid_and_clamped=lambda xy, wa: valid_and_clamped_handle_xy(
                        xy, wa, bar_w),
                    default_dock=_default_dock_xy,
                    on_err=_collapsed_err,
                )
                if not visible:
                    set_visible(False)
                    return
                if state.get("last_collapse_err") is not None:
                    state["last_collapse_err"] = None   # 判定恢复 -> 允许再记
                if hxy is not None and hxy != state["last_xy"]:
                    win().move_window(hwnd, hxy[0], hxy[1], bar_w, HANDLE_H, True)
                    state["last_xy"] = hxy
                set_visible(True)
                return
            # 完整态（未收起）：贴 ZCode 底部外沿（水平居中），钳制工作区。
            # 0.2.1 判活改判：可见性只由「ZCode 窗口存在 + 未最小化 + 矩形
            # 可得」决定，前台判定仅用于「贴边锚点退一级」（ZCode 非前台但
            # 窗口存在时用当前矩形贴边，仍保持可见），不作为隐藏条件。
            # 判定抽为模块级 expanded_poll_decision（依赖显式注入，可独立
            # 重放测试），本闭包只做副作用：_apply_noactivate 持续保障不抢
            # 焦点（SWP_NOMOVE|SWP_NOSIZE，与 move_window 先后无涉）、
            # move_window 吸边、set_visible。
            bar_w = state.get("cur_w") or WINDOW_W
            visible, xy = expanded_poll_decision(
                state, bar_w,
                zcode_hwnd=find_zcode_window(),
                is_iconic=win().is_iconic,
                rect_of=window_rect_of,
                foreground=is_foreground_zcode,
                dock=dock_rect,
                clamp=clamp_to_work_area,
            )
            if not visible:
                set_visible(False)
                return
            _apply_noactivate()
            # xy is None：manual_position，停在用户放下的位置，不动坐标也不记
            # last_xy（与外提前同一支路）。
            if xy is not None and xy != state["last_xy"]:
                win().move_window(hwnd, xy[0], xy[1], bar_w, WINDOW_H, True)
                state["last_xy"] = xy
            set_visible(True)
        except Exception:
            # 任何异常只隐藏或保持现状，绝不让小条抢焦点、绝不弹错。
            # 0.2.1：仅隐藏本拍，下一拍 poll 仍会按「目标存在/最小化/矩形可得」
            # 重新判定显示（不永久藏死）——异常多为瞬时（如窗口重入瞬间矩形
            # 取不到），fail-closed 防抢焦点但不可把条永远藏没。
            try:
                set_visible(False)
            except Exception:
                pass
        finally:
            _after(state.get("refresh_ms", refresh_ms), poll)

    try:
        refresh_tick()       # 启动即有一行内容（refresh_once 同步执行），
                             # 并排上 1 秒兜底链
    except Exception:
        pass
    try:
        # 事件驱动链首拍：FS_POLL_MS 后开始排空 event_queue
        _after(FS_POLL_MS, poll_fs_events)
    except Exception:
        pass
    try:
        _after(state.get("refresh_ms", refresh_ms), poll)
    except Exception:
        pass
    try:
        root.mainloop()
    finally:
        _cleanup_pid(data_dir)
    return 0


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
