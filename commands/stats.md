---
description: 查看本会话/历史/按模型的 token 使用量、缓存命中率与速度（耗时/TTFT）统计
argument-hint: [会话|历史|模型|缓存|速度]
---

# zcode-token-stats：token 用量 / 缓存 / 速度统计

运行工具脚本查询 ZCode 本地 SQLite 用量库，输出统计报告。脚本只读访问数据库，绝不写库。

## 脚本调用方式

统一用 Bash 执行。脚本路径从 `ZCODE_PLUGIN_ROOT` 环境变量推导（插件根目录，指向本插件版本目录），**不要硬编码版本号**——插件升级换版本目录后旧路径会失效。Python 解释器直接用 `python`（钩子脚本内部有 `PYTHON_BIN > PATH > py -3` 三层解析；命令里也可用 `py -3`）：

```
python "$ZCODE_PLUGIN_ROOT/scripts/db_stats.py" --mode <模式> [--session-id <id>] [--days N] [--json|--table] [--db <db路径>]
```

若 `ZCODE_PLUGIN_ROOT` 未设置（如手动在终端跑），可回退用实际存在的插件缓存路径（默认 `%USERPROFILE%\.zcode\cli\plugins\cache\local\zcode-token-stats\<版本>`），但优先使用环境变量写法。db 默认路径由脚本内 `~/.zcode/cli/db/db.sqlite` 推导，也可用 `--db` 显式指定。

## 模式与参数

1. 本会话汇总：
   `--mode session --session-id <session_id>`
   若拿不到当前 session id，可省略 `--session-id`（脚本自动取最近更新的会话），并在报告中注明"最近一次会话"。

2. 历史汇总（默认最近 7 天）：
   `--mode history --days 7`
   或指定起始毫秒：`--mode history --since <epoch_ms>`

3. 按模型分组：
   `--mode model`

4. 输出格式：加 `--json` 得 JSON；不加则为人类可读表格。

## 报告组织要点

汇总四类指标并清楚标注口径与时窗：

- **用量**：model_request_count（请求数）、input_tokens / output_tokens / reasoning_tokens、error_count / cancelled_count。
- **缓存**：cacheReadTokens（cache_read_input_tokens）、cacheCreationTokens（cache_creation_input_tokens）、cache_hit_rate。
  **口径：cache_hit_rate = cache_read_input_tokens / (input_tokens + cache_read_input_tokens)**（仅输入侧，排除 cache_creation）。
- **速度**：total_duration_ms、avg_duration_ms（平均总耗时）、avg_ttft_ms（TTFT = time_to_first_token_ms，首 token 耗时均值）。
- **统计口径说明**：请求数/错误数对全部状态行计数；token 与耗时聚合仅对 status='completed' 的行。

## 长期累计记录

插件数据目录 `%USERPROFILE%\.zcode\cli\plugins\data\local\zcode-token-stats\token-stats.jsonl`
每行一条会话统计记录（由 Stop 钩子自动追加）。报告末尾可加一句：
"统计记录文件累计 N 行（长期趋势见 token-stats.jsonl）"。行数可用 Bash：
`find "$USERPROFILE/.zcode/cli/plugins/data/local/zcode-token-stats" -name "token-stats.jsonl" -exec wc -l {} \;`
（或 Python：`open(...).read().count(chr(10))`）。
