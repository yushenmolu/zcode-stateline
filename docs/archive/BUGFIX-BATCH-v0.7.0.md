# v0.7.0 全量修复批记录（2026-08-27）

> 归属会话修复批：两轮审查确认的 13 个问题一次修完。
> 提交：da86efa（主体，13 files, +773/-270）、0aba76c（install.cmd 预览行收尾）。均未推送远程。

## Critical

### N1 inject_context.py 编码崩溃致统计行静默失效
- 症状：中文系统（GBK/cp936 管道）下对话内统计行 ⏱… 注入完全无声失效（stdout 空、exit 0）。
- 根因：inject_context.py 输出含 U+23F1（⏱），sys.stdout.write 走 ANSI 代码页编码抛 UnicodeEncodeError，被 except pass 吞掉。
- 修复：main 输出改 sys.stdout.buffer.write(UTF-8 字节)，无 buffer 时回退 reconfigure；异常改 stderr 留痕不再静默。
- 验证：PYTHONIOENCODING=cp936 实测修复前 stdout 0 字节 → 修复后 103 字节且 json.load 合法。

## High

### H1 docked_statusbar 非前台贴边坐标笔误
- 症状：ZCode 非最大化且失焦时状态条飞到屏幕边缘错误位置。
- 根因：非前台分支 y 误用 zrect[2]（right）应为 zrect[3]（bottom），对照 dock_rect 的 y=bottom+margin 证实语义错位。
- 修复：clamp_to_work_area((zrect[0], zrect[3] + MARGIN), ...)。
- 验证：py_compile 通过；分支可达性复核成立（GUI 落位待人工目视）。

### H2 statusbar_alive 版本守卫整体失效
- 症状：SessionStart 每次白拉起一个 pythonw 再自杀；"旧版强杀换新"核心功能永不生效。
- 根因：读 ZCODE_PLUGIN_DATA——钩子环境该变量指向另一套空目录，pid 文件恒找不到恒 return 1。
- 修复：恒用 DATA_DIR_DEFAULT（与 mark_session 同一定义），补注释说明该坑。
- 验证：模拟钩子环境注入空目录变量，脚本正确落到真实数据目录。

### H3 status_event 子代理事件污染全局徽标
- 症状：子代理 Stop 把主会话徽标提前打成"空闲"。
- 根因：status-state.json 全局单份，status_event 是兄弟脚本中唯一不过滤 sess_subagent_* 的。
- 修复：新增 is_subagent_session 过滤，命中即忽略写入。
- 验证：沙箱实测三种子代理 id 变体均不写入、正常 id 写入正常。

### M4（随 H2 连带）强杀误杀防护
- 症状/风险：pid 复用时 TerminateProcess 可能杀无关进程。
- 修复：_terminate 前 QueryFullProcessImageNameW 取映像名，须 python(w).exe 且目录含 python 特征；取不到一律放弃杀。
- 验证：无关进程（cmd.exe）命中伪造旧版本记录仍被拒绝强杀（负向路径实测）。

## 数据准确性

### N3 记账游标漏洞（失败行重复计入）
- 症状：纯失败轮后，下一份记录请求数/错误数虚高。
- 根因：游标仅按 completed 行 MAX(started_at) 推进，error 行永留窗口被重扫重复计入。
- 修复：游标改 rowid 闭合窗口 (after_rowid, boundary]。选型依据真实库只读诊断：model_usage 落库即终态（无 running 存量）、会话内时间基本单调（迟到率 0.22%，纯时间游标方案会让迟到行永久漏记故弃用）、无回填迹象。含 v1 时间游标→v2 rowid 游标一次性兼容迁移。
- 验证：四阶段造数实测 error 恰计一次、迟到行不漏；迁移幂等。

### N2 多窗口记账竞态
- 症状：双窗口同时 Stop 时 state.json 互踩、WinError 5 崩溃、账目缺口。
- 根因：固定 .tmp 名截断互踩 + 读改写无跨进程锁。
- 修复：tmp 名掺 pid+time_ns；<state>.lock msvcrt 排他锁包裹临界区；os.replace 带界重试。
- 验证：双进程各 12 轮并发，token 总额守恒校验分毫不差，游标采样单调。

## 代理侧

### N4 双实例并存
- 根因：ThreadingHTTPServer 默认 allow_reuse_address=1，Windows 下第二次 bind 同端口也成功。
- 修复：ProxyHTTPServer 子类置 False，bind 失败自愈路径真实可达。
- 验证：第二实例立即退出码 1（OSError 10048）。

### N5 旧版代理永驻
- 根因：ensure-proxy 仅端口探活，升级后旧实例短路放行新代码永不生效。
- 修复：--alive-check 三重正证据门（映像名+GetExtendedTcpTable 端口归属+日志版本比对），全过才杀；ensure-proxy 按退出码分流。
- 验证：三种身份未知变体全部拒绝杀进程（RC=3）；正向正确清除带旧版痕迹实例（RC=2）。

### N6 文件无限增长
- 问题：JSONL 每秒全量解析越用越卡；live-proxy.log 无轮转。
- 修复：refresh_stats 加 (mtime_ns,size) 指纹缓存，文件未变复用上次解析；log 超 5MB rename .old。
- 验证：缓存命中/失效双态实测；5MB 日志轮转实测旧内容进 .old。

## 杂项
- 版本统一 0.7.0：plugin.json / install.cmd / README / STATUSBAR_VERSION / proxy __version__ 五处 grep 一致。
- status-event.cmd 去硬编码：%USERPROFILE%/%~dp0 推导根目录、Python 三层回退、CRLF 行尾修复。
- 缓存命中率口径统一 cache_read/input（input 已含）：commands/stats.md:41 与 docked_statusbar tooltip 两处错误文案修正。
- db_recent_session_id 补 session_id LIKE 'sess_%' 过滤，对齐 inject_context 口径。
- 死代码清理约 170 行（read_live_stream/live_to_stats/live_elapsed_s/_build_live_parts/_build_live_blocks、turn_stats_text 及 TIP_LIVE* 常量），删前引用盘点零依赖、删后反查零残留。
- docstring ${ZCODE_SESSION_ID}→${CLAUDE_SESSION_ID} 三处加 cmd 注释一处。
- 7 个 .bak-* 移入 docs/archive/bak/（gitignore 忽略不入库），install.cmd 两处 robocopy 均 /XF *.bak-*。

## 批次验证汇总
py_compile scripts/*.py EXIT=0；replay_cure ALL 5 SCENARIOS PASS；verify_against_sqlite RESULT: MATCH ALL（14 项口径）；安装 exit=0 且缓存抽查（plugin.json/version、STATUSBAR_VERSION、.bak 计数 0）通过。
