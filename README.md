# wordpress-ai-excerpt-backfill

[English](README.en.md)

## 项目简介

`wordpress-ai-excerpt-backfill` 是一个面向 WordPress 中文历史文章的确定性审计工具。当前重点不是立即批量生成摘要，而是盘点编辑器与代码格式、识别结构和翻译风险，并为未来的摘要补全建立可解释、可复核的资格筛选管线。

项目当前仍是只读审计管线，不是可以执行摘要回填的工具。所有生产读取都必须只读、显式限量，并且能够独立校验。

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
- 已完成 3 条、20 条和 100 条中文已发布文章的受控生产导出；下载后已核对 SHA-256，并完成本地分析。

### 正在进行

- 扩大受控历史样本。
- 验证历史文章的格式边界和确定性风险规则。
- 确定未来可以安全进入摘要生成阶段的低风险文章。

### 尚未实现

- AI 摘要生成。
- WordPress 摘要写回。
- 批量修改文章。
- 自动部署任何 WordPress 写入工具。
- 翻译生成或替换。

本项目至今没有修改任何 WordPress 文章、摘要、分类、标签、数据库记录或缓存，也没有调用任何 AI API。

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
- 当前代码不包含数据库写入、WordPress 更新、摘要生成、翻译或 AI API 集成。

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

- 继续扩大只读样本并验证历史格式分布。
- 在迁移前审查 mixed、unknown、损坏结构和 SyntaxHighlighter 内容。
- 确认未来摘要候选的低风险资格边界。
- 将摘要生成与 WordPress 写回设计为相互独立的后续流程。
- 在任何写入阶段开始前，增加明确的备份、dry-run、幂等性、冲突检测和回滚能力。

在这些保护措施完成设计、实现并独立验证之前，不应添加或运行任何写回命令。
