# BUGFIX：状态栏无声卡死（after 链断裂，v0.12.1 后仍发）

**状态**：Open
**日期**：2026-09-25

## 症状

用户反馈状态栏"卡住、移动不了、展开不了"。冻结现场（杀进程前抓取）：
- 进程活着但以 ~89% 单核 CPU 空转（8 秒墙钟烧 7.09 秒 CPU）；
- stats.log 自 2026-09-25 06:30:41（rowid<=108494）后 2.5 小时零写入；
- docked-statusbar-err.log 最后一条停在 2026-09-23 23:46（旧 NameError），09-25 整天零记录；
- 即"无声停摆"：进程没崩、没抛错，poll 循环 06:30 后再没成功执行一拍。

## 根因分析（症状 → 直接原因 → 根因）

- **直接原因**：tkinter after 链断裂后进程僵死空转。已排除三个候选：alpha 动画 160ms 帧计数必停（statusbar_gui.py:889-957）；poll/poll_fs_events 间隔 1000/200ms 正常且有下钳（statusbar_gui.py:1564, docked_statusbar.py:443-444）；hover 自动展开是有限动作（statusbar_gui.py:793-811）。
- **根因（两个韧性缺陷，均有 file:line 实锤）**：
  1. **poll 的 except 静默吞异常、不写日志**（statusbar_gui.py:1816-1825）：except 里只做 `set_visible(False)`，finally 仍 reschedule，于是 poll 链永远转下去、每拍都异常、每拍都不写日志——这是"无声"的直接来源。
  2. **`_after._wrapped` 吞异常后不 reschedule**（statusbar_gui.py:509-524）：回调抛一次异常，该 after 链就永久断裂——这是"停摆"的直接来源。
- **放大/诱因（推测，待验证）**：9-23 的 `zcode_gone_exit_due` NameError 是 v0.11.4→v0.12 升级期两文件错位所致，当前 0.12.1 两文件已对齐（docked_statusbar.py:1902 定义、statusbar_gui.py:1725 引用），该 NameError 本身已消失；但"after 链断裂"的韧性缺陷仍在，任何一拍异常都能让整条链无声死亡。06:30 的具体触发异常因缺陷 #1（无日志）已不可考。

## 修复方案（最小改动，3 处 + 1 处兜底）

改 `scripts/statusbar_gui.py`（开发副本，随后同步部署副本）：

1. **poll 的 except 补错误日志**：在 1816-1825 的 except 分支里调 `_log_err`（statusbar_config.py:83-91 已有），加节流（同一异常签名 60 秒内只写一次，防刷屏）。让"无声"变"有声"，下次再犯能抓到现行。
2. **`_after._wrapped` 吞异常后 reschedule 自身**：509-524 的 except 分支末尾补 `self.root.after(ms, _wrapped)`，把"断链"改为"下一拍重试"；并为防"每拍都异常"的高频重抛，重排间隔取 `max(ms, 1000)`（至少 1 秒一拍，给异常路径降温）。
3. **看门狗加挂死检测**：`scripts/statusbar_alive.py` 在"pid 活着"分支里补一刀——连续两次采样（间隔 3 秒）CPU 增量都 >2.5 秒（即 >80% 单核），视为假死，走杀+拉起流程（复用现有 `_kill_residual_statusbars` + 启动逻辑）。让"无声卡死"5 分钟内自愈，不用用户手动叫。

不改：alpha 动画（已证 160ms 必停）、轮询间隔（已证有下钳）、zcode_gone_exit_due 定义（已对齐）。

## 验证方案

1. `python -m py_compile` 两个改动文件通过；
2. 项目测试套件（tests/）回归通过；
3. 部署后重启，实测：PID 起来、boot.log 新行、stats.log 持续 append、CPU 基线 <5%；
4. 注入验证：临时在 poll 里抛一次异常，确认 err.log 出现记录且链不断（验证后移除注入）。

## 风险与边界

- reschedule 自身可能让"每拍都异常"的回调变成 1 秒一拍的循环（有日志、有降温，可接受）；
- 看门狗 CPU 判定有误判风险（编译/杀软扫描时 pythonw 瞬时高 CPU）——用"连续两次都高"降误判；
- 不追 06:30 的具体异常是什么（因无日志已不可考），本方案目标是"让它不再无声死、死了能自愈"。

---

## 验证结论（2026-09-25）

**状态：Verified**

- 计划门：attempt-5 PASS（含 DISPOSITION_REVIEW 处置复核，驳回 carryover 的误报 finding、加固两条新 P1 设计）。
- 实现（v0.12.2）：
  - `scripts/statusbar_gui.py`：`_after._wrapped` 加断路器（连续 10 次异常熔断）+ 指数退避（封顶 30s）+ 异常重排；poll 的 except 补 `_log_throttled` 节流日志（同 key 60s 一次、dict 上限 100）。
  - `scripts/statusbar_alive.py`：新增 `_is_hung` 挂死检测——连续两段 3 秒采样 CPU 增量均 >2.5s（≈80% 单核）判假死；3 次进程创建时间复核防 PID 复用误杀；全程在 msvcrt 文件锁内串行。
- 验证：两个文件 py_compile 通过；注入测试 GUI 11/11 + 看门狗 6/6 全过；项目测试套件 385 通过无新增失败。
- 部署：4 处版本号同步 0.12.1→0.12.2（plugin.json 实际在 `.zcode-plugin\` 下）；install.cmd 4 步全过；手动触发看门狗换版，boot.log 写入 `2026-09-25 10:04:36 STATUSBAR_VERSION=0.12.2 pid=11644`；新进程 CPU 基线 2.9% 单核（健康）；计划任务指向 0.12.2。
- 遗留：install.cmd 不停旧实例（需手动触发看门狗换版），非本次范围；06:30 的具体触发异常因旧代码无日志已不可考，本次靠"补日志+断路器+看门狗自愈"让同类问题不再无声死。

> 归档时间：2026-09-25T10:09:37+08:00 | 归档人：AI 代行（用户长期授权代行 UAT） | 测试结果：通过
