# 盘点数据 Schema

## 1. 通用约定

- 文件编码为 UTF-8；原始数据与逐篇分析结果使用 JSONL，每行一个完整 JSON 对象。
- 时间使用 ISO 8601；同时保存 WordPress 本地时间和 UTC 时间。
- ID 为整数；计数均为非负整数；枚举值使用本文固定的小写字符串。
- 原始导出与分析结果通过 `schema_version` 和 `ruleset_version` 演进。
- 原始记录必须保持正文原样；分析器不得覆盖原始文件。
- 证据必须限长。建议每条 `excerpt` 最多 240 个 Unicode 字符，每个规则每篇最多 5 条；额外命中只保留总次数。

## 2. 原始 JSONL

每行表示一篇只读导出的 WordPress 文章：

| 字段 | 类型 | 要求 |
|---|---|---|
| `schema_version` | integer | 必填，第一版为 `1` |
| `export_id` | string | 必填，本批导出的稳定标识 |
| `exported_at` | string | 必填，UTC ISO 8601 |
| `site_url` | string | 必填，用于本站链接/媒体主机判断 |
| `post_id` | integer | 必填 |
| `post_type` | string | 必填，正式范围应为 `post` |
| `post_status` | string | 必填，正式范围应为 `publish` |
| `title` | string | 必填，原始标题 |
| `slug` | string | 必填 |
| `published_at` | string | 必填，WordPress 本地时间 |
| `published_at_gmt` | string | 必填，UTC |
| `modified_at_gmt` | string/null | 可选 |
| `permalink` | string | 必填，由 WordPress 解析的永久链接，不使用 `guid` 代替 |
| `language_source` | string | 必填；正式中文数据预期为 `polylang`，无法读取为 `unknown` |
| `language` | string/null | 规范化语言；正式中文为 `zh`，无法确认时为 null |
| `language_raw` | string/null | Polylang 原始语言值，便于复核映射 |
| `categories` | array | `{term_id,slug,name}` 对象数组 |
| `tags` | array | `{term_id,slug,name}` 对象数组 |
| `content` | string | 必填，未经分析器修改的 `post_content` |
| `content_sha256` | string | 必填，原始 UTF-8 内容字节的十六进制 SHA-256 |

批次 manifest 以后应另行记录导出条件、WP/WP-CLI 版本、记录数、文件字节数和文件 SHA-256；本阶段不定义或实现生产导出命令。

## 3. 分析结果 JSONL

### 3.1 身份、版本和基本信息

```json
{
  "schema_version": 1,
  "ruleset_version": "1.0.0",
  "analysis_run_id": "run-id",
  "analyzed_at": "2026-07-20T00:00:00Z",
  "source_export_id": "export-id",
  "post_id": 123,
  "title": "示例",
  "published_at": "2020-01-02T03:04:05+08:00",
  "permalink": "https://example.invalid/example/",
  "content_sha256": "..."
}
```

分析结果必须复制足够的源身份字段，并在运行前复核 `content_sha256`。

### 3.2 语言

```json
"language": {
  "language_source": "polylang",
  "language": "zh",
  "language_raw": "zh_CN",
  "in_scope": true,
  "fallback_status": "not-needed",
  "rule_ids": ["LANG_POLYLANG_ZH"]
}
```

`fallback_status` 枚举：`not-needed`、`language-unknown`、`fallback-check-required`、`manually-resolved`。字符检测结果可作为规则命中保存，但不能覆盖明确的 Polylang 值。

### 3.3 分类

```json
"classification": {
  "editor_format": "gutenberg",
  "code_format": "core-code",
  "primary_format": "gutenberg/plain"
}
```

- `editor_format`：`classic`、`gutenberg`、`mixed`、`unknown`。
- `code_format`：`none`、`syntaxhighlighter`、`code-block-pro`、`core-code`、`classic-pre-code`、`mixed`、`unknown`。
- `primary_format`：`classic/plain`、`classic+syntaxhighlighter`、`gutenberg/plain`、`gutenberg+syntaxhighlighter`、`gutenberg+code-block-pro`、`mixed`、`unknown`。

### 3.4 Gutenberg 区块统计

```json
"blocks": {
  "total_count": 2,
  "distinct_count": 2,
  "balanced": true,
  "items": [
    {"name": "core/paragraph", "count": 1, "status": "known-core"},
    {"name": "vendor/widget", "count": 1, "status": "unknown"}
  ],
  "invalid_comments": []
}
```

`status` 枚举：`known-core`、`known-third-party`、`inactive-third-party`、`unknown`。`invalid_comments` 每项包含 `kind`、可选 `block_name`、`offset` 和限长 `excerpt`。

全站聚合对每个区块输出：`name`、`article_count`、`total_count`、`status`。

### 3.5 短代码统计

```json
"shortcodes": {
  "total_count": 1,
  "distinct_count": 1,
  "items": [
    {"name": "sourcecode", "count": 1, "status": "known", "balanced": true}
  ],
  "invalid_items": []
}
```

`status` 为 `known` 或 `unknown`。`invalid_items` 保存 `unclosed`、`orphan-close`、`malformed` 等类型、位置和限长证据。全站聚合字段与区块相同。

### 3.6 代码统计

```json
"code": {
  "format_families": ["core-code"],
  "syntaxhighlighter_count": 0,
  "code_block_pro_count": 0,
  "core_code_count": 1,
  "classic_pre_code_count": 0,
  "damaged_count": 0
}
```

同一实体的内部标签不得重复增加格式实例数；内部结构规则仍可分别记录在检测命中中。

### 3.7 媒体、正文和结构统计

```json
"metrics": {
  "content_bytes": 120,
  "content_character_count": 100,
  "content_text_length": 42,
  "image_count": 2,
  "local_image_count": 1,
  "external_image_count": 1,
  "unknown_host_image_count": 0,
  "figure_count": 1,
  "gallery_count": 0,
  "video_count": 0,
  "audio_count": 0,
  "embed_count": 0,
  "iframe_count": 0,
  "table_count": 0,
  "largest_table_rows": 0,
  "largest_table_columns": 0,
  "largest_table_cells": 0,
  "internal_link_count": 1,
  "external_link_count": 1,
  "unknown_host_link_count": 0,
  "html_comment_count": 0,
  "raw_html_block_count": 0,
  "protected_structure_count": 2,
  "protected_content_character_count": 20,
  "protected_content_ratio": 0.3226
}
```

Gutenberg 区块注释不重复计入 `html_comment_count`。同一媒体实体由区块包装和 HTML 元素共同表达时，实体指标去重；检测规则仍可全部保留。

### 3.8 规则命中与证据

```json
"detections": [
  {
    "rule_id": "CORE_CODE_BLOCK",
    "strength": "strong",
    "count": 1,
    "evidence": [
      {"offset": 0, "excerpt": "<!-- wp:code -->", "truncated": false}
    ]
  }
]
```

`strength` 为 `strong` 或 `weak`。`offset` 是原始 `content` 的 Unicode 字符偏移。证据不能包含整篇正文；超限必须设置 `truncated=true`。结果顶层另存去重、排序后的 `matched_rule_ids`，便于 CSV 输出。

### 3.9 风险与人工复核

```json
"risk": {
  "risk_level": "medium",
  "risk_reasons": ["RISK_RAW_HTML"],
  "score": null
},
"review": {
  "required": false,
  "status": "not-required",
  "reviewer": null,
  "reviewed_at": null,
  "resolution": null,
  "notes": null
}
```

第一版不使用数值风险模型，故 `score` 固定为 null。`review.status` 枚举：`not-required`、`pending`、`in-review`、`resolved`。自动分析只可设置前两项；人工字段不得由重复分析覆盖。`resolution` 可记录确认分类、规则误报、需修复源内容或接受风险。

### 3.10 代表性样本引用

样本报告的每项至少包含：

```json
{
  "sample_group_type": "primary_format",
  "sample_group": "gutenberg/plain",
  "selection_reason": "longest-content",
  "selection_metric": 30123,
  "post_id": 123,
  "permalink": "https://example.invalid/example/",
  "classification": {},
  "risk_level": "medium",
  "risk_reasons": [],
  "evidence_refs": ["CORE_CODE_BLOCK"]
}
```

### 3.11 第一阶段处理资格

资格与格式分类分别保存。符合全部硬条件时：

```json
"eligibility": {
  "phase": "phase-1",
  "status": "eligible-gutenberg-code-block-pro",
  "eligible": true,
  "exclusion_reasons": []
}
```

不符合时：

```json
"eligibility": {
  "phase": "phase-1",
  "status": "excluded",
  "eligible": false,
  "exclusion_reasons": [
    "EXCLUDE_SYNTAXHIGHLIGHTER"
  ]
}
```

`phase` 第一版固定为 `phase-1`。`status` 为 `eligible-gutenberg-code-block-pro` 或 `excluded`；`eligible` 必须与 status 一致；`exclusion_reasons` 使用分类规则文档中的 ID，去重后按字典序排列。资格计算必须可从原始范围字段、语言、classification、detections、code、risk 中复核。

`excerpt_status` 是独立字段，枚举建议为 `missing`、`empty`、`valid`、`unknown`。进入结构候选池不代表需要摘要：未来摘要命令只接受 `missing` 或 `empty`，并拒绝覆盖 `valid`。英文重译不复用摘要命令。

## 4. CSV 投影

逐篇 CSV 至少包含：`post_id`、`title`、`published_at`、`permalink`、`language_source`、`language`、`editor_format`、`code_format`、`primary_format`、区块/短代码/媒体/表格/链接主要计数、`matched_rule_ids`、`risk_level`、`risk_reasons`、`review_status`。数组字段使用固定分隔符并在 manifest 中声明；完整嵌套数据以 JSON 为准。

## 5. 运行级汇总

运行 manifest/summary 以后至少记录：源文件 SHA-256、schema 与规则版本、配置 SHA-256、分析时间、范围内文章数、排除及语言未知数、各格式数量与占比、各风险等级数量、区块和短代码聚合、输出文件 SHA-256。格式占比的分母为正式范围内的 Polylang 中文已发布文章数。

## 6. 未来盘点输出

本节只定义文件契约，不表示报告生成器已经实现。CSV 均使用 UTF-8，数组字段使用 manifest 声明的固定分隔符；嵌套证据仍以分析 JSON 为准。

### 6.1 `eligible-gutenberg-code-block-pro.csv`

仅包含 `eligibility.status=eligible-gutenberg-code-block-pro` 的记录，至少包含：

- `post_id`
- `title`
- `published_at`
- `modified_at`
- `permalink`
- `language`
- `editor_format`
- `code_format`
- `primary_format`
- `code_block_pro_count`
- `content_sha256`
- `excerpt_status`
- `eligibility_status`
- `exclusion_reasons`（合格记录应为空）

此文件是结构候选池。未来中文摘要命令还需按 `excerpt_status` 过滤；未来英文重译命令独立读取所需候选和语言配对数据。

### 6.2 `syntaxhighlighter-migration.csv`

包含任何完整或损坏 SyntaxHighlighter 信号的文章，至少包含：

- `post_id`、`title`、`published_at`、`modified_at`、`permalink`
- `language_source`、`language`
- `editor_format`、`code_format`、`primary_format`
- `syntaxhighlighter_count`、`code_block_pro_count`
- `syntaxhighlighter_status`：`classic+syntaxhighlighter`、`gutenberg+syntaxhighlighter`、`mixed-with-code-block-pro` 或 `damaged-syntaxhighlighter`
- `matched_rule_ids`、`risk_level`、`risk_reasons`
- `content_sha256`、`review_status`

同一文章可命中多个检测事实，但 CSV 每篇只保留一行，并通过 `syntaxhighlighter_status` 与规则 ID 明确迁移类型。该文件也用于迁移后验证残留数量是否为零。

### 6.3 `format-review.csv`

包含 mixed、unknown、任意损坏结构、语言未知或 Code Block Pro 结构异常的文章，至少包含：

- `post_id`、`title`、`published_at`、`modified_at`、`permalink`
- `language_source`、`language`
- `editor_format`、`code_format`、`primary_format`
- `code_block_pro_count`
- `review_reasons`、`matched_rule_ids`
- `risk_level`、`risk_reasons`
- `eligibility_status`、`exclusion_reasons`
- `content_sha256`、`review_status`、`review_notes`

### 6.4 `format-periods.json`

按 `published_at` 汇总格式时期，建议结构为：

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-20T00:00:00Z",
  "denominator": "published-polylang-zh-posts",
  "formats": {
    "gutenberg+code-block-pro": {
      "first_article": {"post_id": 101, "published_at": "2024-01-01T00:00:00+08:00"},
      "last_article": {"post_id": 202, "published_at": "2026-01-01T00:00:00+08:00"},
      "by_year": {"2024": 12},
      "by_month": {"2024-01": 3}
    }
  },
  "periods": [
    {
      "period": "2024-01",
      "dominant_format": "gutenberg+code-block-pro",
      "dominant_count": 10,
      "total_count": 12,
      "exception_count": 2,
      "exception_post_ids": [88, 89]
    }
  ]
}
```

每种格式必须提供最早、最晚文章以及按年、按月数量。`dominant_format` 仅是时期统计；任何例外文章仍以正文检测结果分类，日期不能改变格式或处理资格。
