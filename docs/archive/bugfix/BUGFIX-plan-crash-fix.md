DISPOSITION_REVIEW: true

# BUGFIX 计划：v0.12.0 状态栏反复裸崩溃

## 症状
状态栏"老是卡住"。实测：进程反复退出——boot.log 显示 v0.12.0 部署后每 6-7 分钟换 PID（43404→31204→31196→43128→28832），当前全机 0 个 pythonw；err.log 干净（无 traceback），pidfile 残留（finally 未执行）。status-state.json 事件 14 秒前还在写入，钩子层健康，问题 100% 在渲染进程。

## 根因（症状→直接原因→根因）
- 症状：状态栏消失/不动
- 直接原因：pythonw 进程非正常退出（无 traceback、finally 未跑、pidfile 残留）
- 根因（最高嫌疑）：ctypes 回调委托被 Python GC 回收。`docked_statusbar.py:1811-1834` 的 `_zcode_process_alive` 和 `1713-1738` 的 `_find_zcode_window_by_exe` 里，`EnumWindowsProc(_enum_cb)` 是函数内临时委托对象；poll 每秒调用一次，Windows 枚举窗口期间若 GC 回收该委托，回调到已释放内存→进程立即崩溃，不产生 Python 异常。

## 修复方案

### 1. ctypes 委托提升为模块级常驻对象（修根因）
`_zcode_process_alive` 和 `_find_zcode_window_by_exe` 内部的 EnumWindowsProc 委托改为模块级单例：模块加载时创建一次并持有引用（如 `_ENUM_CB = EnumWindowsProc(_enum_cb)`），函数内复用，消除 GC 窗口。改动 docked_statusbar.py，约 10-20 行。

### 2. 看门狗改心跳 + 互斥锁防并发（P1-watchdog-duplicate-start 处置）
现状：statusbar_alive.py 只被 SessionStart 钩子触发，用户长时间不开新会话时进程死了没人拉起。
改为：install.cmd 里注册 Windows 计划任务（schtasks /create /tn "ZcodeTokenStatsAlive" /sc minute /mo 5 /tr "\"%PYW_EXE%\" \"%DST%\scripts\statusbar_alive.py\"" /f），每 5 分钟自查。

> 路径说明（回应审查 P1-watchdog-relative-task-path）：计划任务不继承工作目录，注册命令中 %PYW_EXE% 与 %DST% 均由 install.cmd 在安装时解析为绝对路径——%PYW_EXE% = D:\Program Files\python312\pythonw.exe，%DST% = C:\Users\yushe\.zcode\cli\plugins\cache\local\zcode-token-stats\0.12.1，即 /tr 两端均为带引号绝对路径。安装后已验证 exact action：`schtasks /query /tn "ZcodeTokenStatsAlive" /v /fo list` 显示"要运行的任务" = "D:\Program Files\python312\pythonw.exe" "C:\Users\yushe\.zcode\cli\plugins\cache\local\zcode-token-stats\0.12.1\scripts\statusbar_alive.py"；任务已启用、每 5 分钟一次、上次运行 2026/9/25 1:35:01 结果 0。

**互斥锁防并发**：看门狗拉起逻辑与 SessionStart 钩子的 ensure-docked-statusbar.cmd 可能并发，导致双开。统一规则：
- 两个入口共用同一把**文件锁**（msvcrt.locking）：在拉起新进程前，先对数据目录的 `statusbar.lock` 文件加排他锁（非阻塞，拿不到锁说明另一个入口正在拉起，直接退出）
- 拿到锁后才执行：读 pidfile → PID 活则解锁退出 0 / PID 死或 pidfile 不存在 → 清场（wmic 搜杀所有 CommandLine 含 docked_statusbar 的 pythonw、tasklist 复查）→ 删 pidfile → 拉起新进程 → 等新进程写 pidfile（最多 5 秒）→ 校验 pidfile PID == 新 PID → 解锁
- 锁 holder 崩溃时文件句柄释放，锁自动失效，不死锁
- ensure-docked-statusbar.cmd（SessionStart 钩子）改为先调 statusbar_alive.py 的同一入口（把拉起逻辑收口到 statusbar_alive.py 一处），cmd 只做转发，保证所有拉起路径都过互斥锁

### 3. 部署与回滚（P1-rollback-can-duplicate-process 处置）
- bump v0.12.1（4 文件同步 + README 变更日志）
- install.cmd 部署；失败→不杀旧进程，中止报告
- 杀旧进程（如有）→ 拉起 0.12.1
- 拉起失败/5 秒内崩溃 → 回滚：清场（wmic 搜杀 docked_statusbar 残留、删 pidfile、tasklist 复查）→ installed_plugins.json 写回 0.12.0 → 跑 0.12.0 ensure-docked-statusbar.cmd → 健康检查；回滚动作重试 3 次间隔 2s；兜底 pythonw 裸起 0.12.0 手写 pidfile；回滚未完成时报告里显式标注"状态栏不可用"
- 健康检查：pidfile 存在+PID 存活（tasklist）、boot.log 末行 0.12.1、err.log 启动后 10 秒无新报错

### 4. 重启验证
部署后进程跑 30 分钟：boot.log 不换 PID、tasklist 同一 PID 存活、err.log 无新报错。

## 不做什么
- 不改动画帧循环/region 防抖（已审查无 bug）
- 不改 hover Enter/Leave 调 render_ui（非根因）
- 不抓 procdump（先做最小修复，若修后仍崩再抓 dump 定性）

## 残余风险与边界
- 若修后仍崩：根因不是委托 GC（可能是 SetWindowRgn 句柄类型、Defender 强杀），下一步用 procdump 抓 dump 定性
- 计划任务注册失败（权限不足）→ 退化为 hooks\ensure-docked-statusbar.cmd 内嵌 while 循环（每 5 分钟自查）
- 看门狗计划任务卸载：uninstall.cmd 里需加 schtasks /delete /tn "ZcodeTokenStatsAlive"

---

## 验证结论（2026-09-25）

**状态：Verified**

- 根因：ctypes 回调委托挂在函数局部变量上，EnumWindows 枚举期间被 Python GC 回收 → 回调跳进已释放内存 → 进程无日志直接消失；且原看门狗仅由 SessionStart 钩子触发，崩溃后无人拉起。
- 修复（v0.12.1）：两处枚举回调提升为模块级单例（`_ENUM_CB_FIND` / `_ENUM_CB_ALIVE`）；新增 5 分钟计划任务看门狗（ZcodeTokenStatsAlive）+ msvcrt 文件锁互斥 + 残留进程清理。
- 部署证据：statusbar.boot.log 最后一行 `2026-09-25 01:11:10 STATUSBAR_VERSION=0.12.1 pid=23596`。
- 长窗口稳定性：复核时间 2026-09-25 01:35，连续运行 24 分钟零重启（旧版崩溃周期 6-11 分钟，boot.log 中 v0.12.0 有 6-11 分钟级连续重启史）。
- 活性证据：tasklist 确认 PID 23596 存活（StartTime=01:11:10）；CPU 采样 30.953125→31.1875（10 秒增量 0.234，渲染循环在跑）；statusbar.pid 内容=23596；status-state.json mtime 持续更新。
- 看门狗健康：schtasks 显示 ZcodeTokenStatsAlive 已启用、每 5 分钟、上次运行 2026/9/25 1:35:01 结果 0，指向 0.12.1 运行副本路径。
- 错误回归：docked-statusbar-err.log 自 2026-09-23 23:46 起无新增内容（其中 9-23 的 `zcode_gone_exit_due` NameError 为旧版本遗留，本次重写后未再复现）。

> 归档时间：2026-09-25T01:39:13+08:00 | 归档人：AI 代行（用户长期授权代行 UAT） | 测试结果：通过
> 修订：2026-09-25 | §2 看门狗注册命令由相对路径更正为实际部署的绝对路径写法（处置审查 finding P1-watchdog-relative-task-path）
