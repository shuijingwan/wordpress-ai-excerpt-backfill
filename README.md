# wordpress-ai-excerpt-backfill

[English](README.en.md)

## 项目简介

`wordpress-ai-excerpt-backfill` 是一个面向 WordPress 中文历史文章的确定性审计、迁移协调与摘要回填工具。它通过固定批次、生产只读验收、执行证据和恢复机制，在严格安全边界内完成历史文章迁移。

## 当前范围

当前审计范围包括 Gutenberg、Classic Editor 和 mixed 内容，以及 Code Block Pro、SyntaxHighlighter、Gutenberg core code、经典 `pre`/`code`、已知和未知短代码。工具还会记录部分媒体与结构信号、损坏或不平衡的标记、确定性的风险原因，以及第一阶段资格结果。

第一阶段的自动处理边界有意保持严格。只有 Polylang 明确归属中文、已经发布，同时具备完整 Gutenberg 和 Code Block Pro 结构，且不包含 SyntaxHighlighter、不属于 mixed 或 unknown、无需 manual-review 的文章，才可能进入候选范围。`gutenberg/plain` 和所有更早的历史格式目前只用于盘点或迁移，不属于自动摘要候选。

完整分类规则和输出字段见 [docs/classification-rules.md](docs/classification-rules.md) 与 [docs/audit-schema.md](docs/audit-schema.md)。

## 项目状态

### 已完成

- 本地确定性检测器、编辑器/代码格式分类、风险评估和第一阶段资格判断。
- 支持 Polylang 中文文章过滤的生产只读 WordPress 导出器。
- 要求显式导出数量，并在发布远程结果前校验 JSONL 的远程运行脚本。
- 本地 JSONL 合约校验和不包含正文等敏感字段的脱敏分析输出。
- 屏蔽 SyntaxHighlighter、Code Block Pro、`pre` 和 `code` 区域内的短代码外观，避免误判。
- 按结构语义识别 Gutenberg 区块外的空残留，同时保留对真实经典内容的检测。
- 防止分析器输入与输出指向同一文件，包括符号链接和硬链接。
- 覆盖格式夹具、资格判断、导出合约和本地分析的自动化测试。
- 已完成 3 条、20 条、100 条及 SyntaxHighlighter retirement audit 的 1,438 条中文已发布文章受控生产导出；下载后已核对 SHA-256，并完成本地分析。
- 历史迁移固定范围已完成：67 个固定批次、1,281 篇文章，协调状态 `count=1281`、`remaining=0`、`integrity=ok`，所有批次均已完成。执行证据汇总为 `completed=1279`、`failed=2`、`pending=0`、`translation_started=0`；失败记录均已由最终协调状态收口，不存在待处理工作。
- 所有纳入历史迁移范围的文章均已完成最终协调收口；无 pending、translation_started 或其他未完成执行状态。
- 普通 Mixed 候选已耗尽。最后 5 篇显式异常文章通过 `mixed-syntaxhighlighter-special-20260812-01` 固定批次完成，`total=5`、`completed=5`、`remaining=0`、`integrity=ok`。
- 当前没有未完成批次。

### 历史迁移收尾结论

1. 异常候选只需要通过显式、固定的特殊批次进入；进入后继续复用既有 history-migration 状态机、只读验收、执行证据和恢复流程。
2. Code Block Pro 的短代码保护范围必须覆盖完整 Gutenberg 区块，包括开始/结束注释及开始注释中的 JSON 属性。
3. CBP JSON `code` 属性内的 `[code]` 等字面量不是区块外 SyntaxHighlighter 或 shortcode 结构损坏。
4. 已完成且确定性的生产 preflight 拒绝不应对同一候选重复重试。

### SyntaxHighlighter retirement audit

卸载准备使用独立的只读人工检查审计，不改变 migration detector 或状态机。生产
`SyntaxHighlighter Evolved 3.7.2` 的运行时 shortcode 清单固化在
`config/syntaxhighlighter-retirement.json`；插件版本或生产 filter 变化后必须重新
只读取证，不能凭历史命中类型扩展。

输入必须是重新导出的 `post_type=post`、`post_status=publish`、Polylang `zh` JSONL。
`bin/export-readonly.php` 可按 `ID ASC`、每页最多 100 条继续复用；每次以上一页最后
一条 JSON 的 `post_id` 作为下一页 `--after-id`，最后一页少于 100 条时结束。不要复用
历史 raw snapshot。下载各页并核对运行脚本报告的 SHA-256 后运行：

```bash
python3 bin/build-syntaxhighlighter-retirement-audit.py \
  --input data/raw/wordpress-zh-posts-PAGE-01.jsonl \
  --input data/raw/wordpress-zh-posts-PAGE-02.jsonl
```

`--input` 可重复。scanner 会验证 schema、正文 SHA-256、重复 post ID 和严格数据范围，
并在所有正文区域查找生产插件实际注册的小写 shortcode opening/closing 标记；不会保护
Code Block Pro、Gutenberg code block、`pre`、`code`、HTML 或 freeform 区域。
`[[php]...[/php]]` 这类 WordPress escaped literal 不会命中。

输出是被 `.gitignore` 覆盖的本地产物：

- `data/analysis/syntaxhighlighter-retirement-audit.csv`
- `data/analysis/syntaxhighlighter-retirement-audit.txt`

它们只是人工检查候选清单；命中不代表错误、必须修改或卸载前必须清零。

## 安全边界

- PHP 导出器只读取 WordPress 和数据库，不更新文章、元数据、分类、标签、选项或缓存。
- 正式导出入口要求显式提供 `--limit N`，每次允许导出 1～100 条记录。该上限由正式运行脚本实施；PHP 导出器本身要求显式有限数量，但目前没有独立实施相同的 100 条硬上限。
- 部署必须显式使用 `--deploy`。`--dry-run` 只显示计划，不连接生产环境；未指定模式时不会部署。
- 部署和导出是两个独立命令：部署不会启动导出，导出也不会部署代码。
- 分析器要求显式提供 1～100 范围内的 `--expected-count N`。
- 输入校验覆盖 JSONL schema、准确记录数、文章类型、发布状态、Polylang 语言、重复 post ID 和正文 SHA-256。
- 如果输入与输出解析到同一文件，包括通过符号链接或硬链接指向同一文件，分析器会拒绝执行。
- 正式结果先写入临时文件，执行 `flush` 和 `fsync` 后再原子替换到目标路径。
- 生产导出与本地分析结果通过 `.gitignore` 排除，不进入 Git。
- 写入仅通过固定清单和显式 `--execute` 的单篇执行流程发生；执行前后保留 SHA-256、Polylang、发布状态、正文结构和执行证据校验。

## 生产环境布局

当前生产部署使用 SSH 别名 `aliyun`，工具目录位于 Web 根目录之外：

```text
工具目录：      /root/tools/wordpress-ai-excerpt-backfill
WordPress 目录：/data/wwwroot/www.shuijingwanwq.com
站点 URL：      https://www.shuijingwanwq.com
```

导出器以固定文件部署到独立工具目录，远程 JSONL 首先保存在该目录的 `data/raw/`。部署过程不会将项目文件放入 WordPress 根目录、插件目录、主题目录或其他 Web 可访问位置。

## 目录结构

```text
bin/            部署、只读导出和本地分析的命令行入口
config/         纳入版本控制的确定性分类配置
docs/           分类规则和审计数据结构
src/            检测器、分类器、风险评估、分析器和资格判断
tests/          人工夹具和标准库自动化测试
data/raw/       从生产环境只读导出的原始 JSONL
data/analysis/  本地生成的脱敏分析 JSONL
```

`data/raw/` 和 `data/analysis/` 包含本地生成的潜在敏感数据，均已被 `.gitignore` 排除，不得提交。

## 已验证流程

1. 运行完整本地测试。
2. 使用 `--dry-run` 检查部署计划。
3. 显式授权部署只读导出器。
4. 使用 `--limit` 和需要时的 `--after-id` 分批导出。
5. 通过独立受控操作下载 JSONL，并核对 SHA-256。
6. 使用准确的预期记录数运行本地分析器。
7. 审查脱敏分析结果，再决定是否扩大样本或进入后续设计。

生产部署和导出保持为两个独立命令：

```bash
bin/deploy-to-production.sh --deploy
```

```bash
bin/run-readonly-export.sh --limit 5 --after-id 0
```

以上命令都不会生成摘要，也不会写回 WordPress。

## 本地验证

运行完整测试，并避免在项目中生成 Python 字节码：

```bash
PYTHONDONTWRITEBYTECODE=1 \
python3 -m unittest discover -s tests -v
```

## 失败执行后中文源被人工修改

`translation_failed` 后应先区分两种情况：

- 中文标题和正文未修改，只是临时翻译失败：继续使用
  `python3 bin/history-migration.py resume --post-id ID --execute`。
- 为修复问题人工修改了中文标题或正文：禁止直接 resume。先运行
  `restart-from-current --post-id ID` 预览；确认所有生产只读安全检查通过后，加
  `--apply` 归档旧 execution/pre-write、建立当前生产版本的新基线；然后仅对该篇运行
  `run-ready --batch-id BATCH --post-id ID --execute`，最后用 `status` 和 `summary` 验证收敛。

`restart-from-current` 默认只读。它要求旧 execution 为 `translation_failed` 且 pre-write
同时存在；当前中文摘要可以为空，也可以逐字等于旧 execution 的非空 `generated_excerpt`，其他
非空摘要一律拒绝。文章结构和 Polylang 关系仍须合格，并要求英文 title/excerpt/content
与旧写前备份完全一致。旧摘要、失败、文件 SHA-256 和重试计数会进入按时间戳保存的 recovery
审计目录，其中记录新旧摘要 SHA-256、匹配结果和摘要状态。新 generation 的运行计数从零开始，
旧计数累计保留在 `lifetime_retry_counts`。只有该 recovery generation 的正式 run 可以覆盖已知旧
摘要；它仍会用当前标题和正文重新调用 GLM，绝不复用旧 `generated_excerpt`。run 前摘要若发生变化
会 fail closed。普通 fresh run 仍要求空摘要。

同一入口也处理 `workflow_status=excerpt_failed` 且 execution 为 `excerpt_rejected` 的已知拒绝场景。
这不是网络失败，也不能用普通 `resume`：先 preview，再 `--apply`，最后只执行目标篇。

```bash
python3 bin/history-migration.py restart-from-current --post-id ID --json
python3 bin/history-migration.py restart-from-current --post-id ID --apply
python3 bin/history-migration.py run-ready --batch-id BATCH --post-id ID --execute
```

该路径要求中文摘要为空、中文 title/content 与 pre-write 完全一致、英文 title/excerpt/content 未变化，
且所有 execution 记录的 rejected 摘要文件存在于受控 evidence 目录。apply 会按 SHA-256 归档旧
execution、pre-write 和 rejected 文本，创建新的 recovery generation；正式 run 会重新调用 GLM，
不会复用旧 rejected 文本。合法的五位 `SQLSTATE[HY000]`、`SQLSTATE [23000]` 等数据库状态码不是
WordPress shortcode；只有明确 SQLSTATE 上下文会被豁免，真正的 shortcode 和 HTML 仍会被拒绝。
禁止删除 backup 或手工修改 execution、coordination JSON。

Fresh run 将中文 title/content 作为严格锁定的生成 source；中文 excerpt 与英文
title/excerpt/content 是覆盖 target。英文 target 的冻结 SHA-256 会继续作为审计基线报告，
但不会仅因 drift 阻断完整覆盖：执行器会在最后一次读取 target 后、任何写入前创建私有原子
pre-write backup，记录当前值、候选基线、drift 字段和完整覆盖范围。当前 WordPress/SlyTranslate
接口没有 target revision 的原子 compare-and-overwrite；因此备份与翻译调用保持相邻，但不能消除该
外部接口固有的并发窗口。

## Mixed SyntaxHighlighter 固定批次构建

`bin/build-mixed-syntaxhighlighter-batch.py` 用于从本地 Mixed preview、只读 WordPress raw JSONL
快照和 Polylang translation JSONL 构建不可覆盖的固定 CSV。它不会访问或写入 WordPress。构建器会
重新分析 raw 正文，并要求 preview 与 raw 的文章 ID、正文 SHA-256、标题、发布时间、永久链接和结构
计数完全一致；任何漂移都会 fail closed。

输出文件和 batch ID 必须遵循：

```text
batch ID: mixed-syntaxhighlighter-YYYYMMDD-NN
文件名:   mixed-syntaxhighlighter-migration-batch-YYYYMMDD-NN.csv
```

候选按 `published_at DESC`、`chinese_post_id DESC` 排序，每批最多 20 篇；最后不足 20 篇时允许生成
final partial batch。输出固定记录 `source_type=mixed_syntaxhighlighter_daily`，并排除已进入历史批次、
已完成的中英文文章。文章 `2710`、`4984`、`5152`、`5520`、`12389` 始终排除在普通 Mixed 每日批次之外；
它们已通过显式固定特殊批次 `mixed-syntaxhighlighter-special-20260812-01` 完成，普通 builder 的异常 ID 排除规则不因此放宽。

命令需要显式提供 preview、translation snapshot、输出、batch ID 和一个或多个 raw JSONL：

```bash
python3 bin/build-mixed-syntaxhighlighter-batch.py \
  --preview data/analysis/MIXED_PREVIEW.csv \
  --translations data/raw/TRANSLATIONS.jsonl \
  --output data/analysis/mixed-syntaxhighlighter-migration-batch-YYYYMMDD-NN.csv \
  --batch-id mixed-syntaxhighlighter-YYYYMMDD-NN \
  data/raw/WORDPRESS_PART_1.jsonl [data/raw/WORDPRESS_PART_2.jsonl ...]
```

在不连接生产环境的情况下检查部署计划：

```bash
bin/deploy-to-production.sh --dry-run
```

分析已经下载并完成校验的本地批次：

```bash
bin/analyze-export.py \
  --expected-count 100 \
  data/raw/example.jsonl \
  data/analysis/example.analysis.jsonl
```

预期记录数必须与受控导出数量一致，输入和输出必须是不同文件。

## 后续计划

历史迁移已收敛。后续任何新增文章或新的迁移范围必须创建新的、显式固定的审计和执行计划；不得修改已完成的 857 篇历史迁移状态。
