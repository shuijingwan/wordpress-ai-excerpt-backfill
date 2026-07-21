# 历史文章格式盘点分类规则

## 1. 原则与适用范围

第一版分析只使用确定性规则。检测器先产生相互独立的事实和规则命中，再计算 `editor_format`、`code_format`、`primary_format` 与翻译风险；不得通过模型推测格式或风险。

规则匹配正文原始字符串。HTML 标签名、属性名、CSS class、Gutenberg 区块名和已知短代码名按 ASCII 大小写不敏感处理；证据保留原文。匹配必须尊重标签、注释或短代码边界，不能只因普通文字包含关键词而命中。

正式数据范围必须同时满足 `post_type=post`、`post_status=publish`、`language_source=polylang`、`language=zh`。Polylang 无结果时标记 `LANGUAGE_UNKNOWN` 并进入人工或兜底检查；`LANG_HAN_TEXT_PRESENT`、`LANG_HAN_TEXT_ABSENT` 仅是异常证据，不改变明确的 Polylang 归属。

## 2. 信号强度

- **强信号**：结构具有明确命名空间、插件标识或成对语法，可直接证明格式族存在。
- **弱信号**：常见 HTML 结构或不完整片段，可支持判断，但单独不能证明具体插件。
- 同一插件正常生成的多个内部结构只属于一个格式族，不构成 `mixed`。
- 规则命中保存规则 ID、次数及有限长度证据；不得保存“只命中但无法说明位置”的黑箱结论。

## 3. 规则 ID

### 3.1 语言与范围

| 规则 ID | 强度 | 定义 |
|---|---|---|
| `LANG_POLYLANG_ZH` | 强 | Polylang 明确返回规范化语言 `zh`（实现可将 `zh_CN` 等站点已确认别名映射为 `zh`） |
| `LANGUAGE_UNKNOWN` | 强 | 无法读取或无法规范化 Polylang 语言 |
| `LANG_HAN_TEXT_PRESENT` | 弱 | 标题或可见正文包含汉字，仅用于异常检查 |
| `LANG_HAN_TEXT_ABSENT` | 弱 | Polylang 为中文但标题和可见正文均未发现汉字 |

### 3.2 Gutenberg 与编辑器结构

| 规则 ID | 强度 | 定义 |
|---|---|---|
| `GB_BLOCK_COMMENT` | 强 | 存在语法有效的 `<!-- wp:name ... -->`、自闭合区块注释或对应结束注释 |
| `GB_BLOCK_BALANCED` | 强 | 按嵌套顺序解析后所有非自闭合区块正确配对 |
| `GB_BLOCK_DAMAGED` | 强 | 开始/结束注释残缺、名称不匹配、逆序或未配对 |
| `GB_UNKNOWN_BLOCK` | 强 | 区块名不在本次已知区块注册表/配置中 |
| `GB_THIRD_PARTY_BLOCK` | 强 | 有效区块命名空间不是 `core` |
| `GB_INACTIVE_BLOCK` | 强 | 区块在已知历史清单中，但对应插件在导出时登记为未启用 |
| `GB_RAW_HTML_BLOCK` | 强 | 存在 `core/html` 区块注释 |
| `CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS` | 强 | 有效 Gutenberg 顶层区块覆盖范围之外存在非空、非纯空白/注释的实质 HTML 或文本 |
| `EDITOR_SIGNAL_AMBIGUOUS` | 弱 | 仅存在疑似但无法解析的 Gutenberg 片段，且不足以确认 classic 或 Gutenberg |

`CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS` 不因 Gutenberg 正常生成的区块内部 HTML 命中。普通 HTML 本身也不证明 classic 与 Gutenberg 混用。

### 3.3 代码格式

| 规则 ID | 强度 | 定义 |
|---|---|---|
| `SH_SHORTCODE` | 强 | 已知 SyntaxHighlighter Evolved 短代码，如 `[sourcecode]...[/sourcecode]`、`[code]...[/code]`，名称列表由规则版本固定 |
| `SH_BRUSH_MARKER` | 强 | 已知 `brush: language` 等 SyntaxHighlighter 配置标记出现在代码包装上下文中 |
| `SH_HTML_CLASS` | 强 | 已确认属于 SyntaxHighlighter 的包装 class/历史标记 |
| `SH_DAMAGED` | 弱 | 已知 SyntaxHighlighter 开始短代码无闭合、孤立闭合或结构残缺 |
| `CBP_BLOCK_COMMENT` | 强 | 区块名为 `kevinbatdorf/code-block-pro` |
| `CBP_BLOCK_CLASS` | 强 | class 包含 `wp-block-kevinbatdorf-code-block-pro` |
| `CBP_SHIKI_STRUCTURE` | 强 | 在 Code Block Pro 上下文中出现其 textarea、`pre.shiki`、`code`、`span.line` 结构组合 |
| `CBP_PARTIAL_STRUCTURE` | 弱 | 仅出现上述通用内部片段，缺少区块名或插件 class 上下文 |
| `CORE_CODE_BLOCK` | 强 | 有效 `core/code` Gutenberg 区块 |
| `CLASSIC_PRE_CODE` | 强 | 不属于已识别 Gutenberg/插件包装范围的 `<pre>`、`<code>` 或 `<pre><code>` |
| `CODE_STRUCTURE_DAMAGED` | 强 | 已识别代码包装明显截断、错误嵌套或不配对 |

`pre.shiki`、`code`、`span.line` 单独出现可能来自其他工具，因此没有 CBP 区块名/class/结构组合时只能命中弱信号，不能单独归类为 Code Block Pro。

### 3.4 短代码

| 规则 ID | 强度 | 定义 |
|---|---|---|
| `SC_KNOWN` | 强 | 名称存在于版本化的已知短代码清单 |
| `SC_UNKNOWN` | 强 | 语法完整但名称不在已知清单 |
| `SC_UNCLOSED` | 强 | 非自闭合短代码存在开始标记但缺少相应闭合标记 |
| `SC_ORPHAN_CLOSE` | 强 | 存在没有对应开始标记的闭合短代码 |
| `SC_MALFORMED` | 弱 | 疑似短代码但括号、引号或属性结构不完整 |

转义形式 `[[name]]` 按 WordPress 转义短代码处理，不计为有效短代码。自闭合短代码由已知清单或明确自闭合语法判断。短代码解析忽略 HTML 标签属性中的方括号文本，并记录逐名称次数。

### 3.5 媒体、正文与链接

| 规则 ID | 强度 | 定义 |
|---|---|---|
| `MEDIA_IMAGE` | 强 | HTML `img`；按元素计数，不因 `srcset` 重复计数 |
| `MEDIA_EXTERNAL_IMAGE` | 强 | 图片绝对 URL 的主机不属于配置的本站主机集合 |
| `MEDIA_LOCAL_IMAGE` | 强 | 相对 URL或绝对 URL 主机属于本站主机集合 |
| `MEDIA_IMAGE_HOST_UNKNOWN` | 弱 | 图片源为空、data/blob URL 或无法可靠判断主机 |
| `MEDIA_FIGURE` | 强 | `figure` 元素 |
| `MEDIA_GALLERY` | 强 | `core/gallery` 区块或已知 gallery 短代码/结构；同一结构去重后计数 |
| `MEDIA_VIDEO` | 强 | `video` 元素或 `core/video` 区块；元素与包装表示同一实体时去重 |
| `MEDIA_AUDIO` | 强 | `audio` 元素或 `core/audio` 区块；同上去重 |
| `MEDIA_EMBED` | 强 | `core/embed`、已知 embed 短代码或独立嵌入结构 |
| `MEDIA_IFRAME` | 强 | `iframe` 元素；iframe 同时可计入 embed，但分别保留指标 |
| `STRUCT_TABLE` | 强 | `table` 元素；嵌套异常时仍记录并触发损坏规则 |
| `STRUCT_LARGE_TABLE` | 强 | 任一表格数据行数至少 20，或最大列数至少 8，或单表单元格至少 100 |
| `LINK_INTERNAL` | 强 | `a[href]` 指向相对地址或本站主机；页内 `#`、`mailto:`、`tel:`、`javascript:` 不计 |
| `LINK_EXTERNAL` | 强 | `a[href]` 的 HTTP(S) 主机不属于本站主机集合 |
| `LINK_HOST_UNKNOWN` | 弱 | 链接无法可靠解析主机 |
| `HTML_COMMENT` | 强 | 普通 HTML 注释；Gutenberg 区块注释单独统计，不重复计入普通注释 |
| `RAW_HTML_PRESENT` | 强 | `core/html`，或解析后处于 Gutenberg 区块外且属于配置的高风险原始 HTML 元素/结构 |
| `CONTENT_VERY_LONG` | 强 | `content_text_length >= 30000` Unicode 字符 |

正文同时记录：原始 UTF-8 字节数、Unicode 字符数、去除标签/区块注释/短代码后的可见文本字符数。第一版 `many_links` 阈值为内部与外部链接合计至少 50；`many_images` 阈值为图片至少 20。本站主机集合必须来自运行配置，不能写死在检测器中。

## 4. 多维分类

### 4.1 `editor_format`

- `classic`：没有有效 Gutenberg 区块，且正文可作为经典内容可靠解析。
- `gutenberg`：至少一个有效 Gutenberg 区块，区块结构可解析，且没有区块外经典实质内容。
- `mixed`：既有有效 Gutenberg 区块，又有 `CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS`。
- `unknown`：只有损坏或歧义信号，无法可靠确认编辑器结构。

少量空白、普通 HTML 注释以及 Gutenberg 自身区块内部 HTML 不构成 mixed。

### 4.2 `code_format`

- `none`：没有代码格式强信号。
- `syntaxhighlighter`：只有 SyntaxHighlighter 格式族强信号。
- `code-block-pro`：只有 Code Block Pro 格式族强信号。
- `core-code`：只有 Gutenberg `core/code` 格式族强信号。
- `classic-pre-code`：只有插件/Gutenberg 包装之外普通 `pre/code` 强信号。
- `mixed`：两个或更多独立代码格式族有强信号。
- `unknown`：只有代码弱信号或代码结构损坏，不能可靠归入上述格式。

Code Block Pro 的区块注释、插件 class、textarea 和 Shiki 内部结构共同出现仍只算一个 `code-block-pro` 格式族。

### 4.3 `primary_format`

按以下顺序决定：

1. `editor_format=mixed` 或 `code_format=mixed` → `mixed`。
2. `editor_format=unknown` 或 `code_format=unknown` → `unknown`。
3. `editor_format=classic` 且 `code_format=syntaxhighlighter` → `classic+syntaxhighlighter`。
4. `editor_format=classic` 且 `code_format` 为 `none` 或 `classic-pre-code` → `classic/plain`。
5. `editor_format=gutenberg` 且 `code_format=syntaxhighlighter` → `gutenberg+syntaxhighlighter`。
6. `editor_format=gutenberg` 且 `code_format=code-block-pro` → `gutenberg+code-block-pro`。
7. `editor_format=gutenberg` 且 `code_format` 为 `none`、`core-code` 或 `classic-pre-code` → `gutenberg/plain`。
8. 其他未定义组合 → `unknown`。

`gutenberg/plain` 中的 “plain” 表示未使用两个目标历史插件，并不表示没有 `core/code`、普通 HTML 或其他 Gutenberg 核心区块。

## 5. mixed 与 unknown

`mixed` 是肯定结论：至少两个独立格式族均有充分强证据。`unknown` 是证据不足、结构损坏或组合未被规则覆盖。弱信号之间冲突不能自动判为 mixed；应归 unknown。未知区块或未知短代码本身不改变编辑器主分类，只增加风险；若它同时使结构不可解析，才可导致 unknown。

## 6. 区块与短代码统计

每篇文章按规范化完整区块名统计出现次数和去重名称集合；省略命名空间的核心区块规范化为 `core/name`。全站报告为每个区块统计“出现文章数”和“总出现次数”。另存 `known`、`third_party`、`inactive`、`unknown` 状态及区块注释错误。

短代码按规范化名称保存开始/自闭合实例次数；成对短代码只按开始标记计一个实例。全站同样统计出现文章数和总次数，并保存 known/unknown、未闭合、孤立闭合和疑似损坏状态。

## 7. 翻译风险

### 7.1 风险规则

| 风险原因 ID | 触发条件 | 严重度 |
|---|---|---|
| `RISK_DAMAGED_STRUCTURE` | `GB_BLOCK_DAMAGED`、`CODE_STRUCTURE_DAMAGED`、`SC_UNCLOSED`、`SC_ORPHAN_CLOSE` 任一命中 | manual |
| `RISK_MIXED_EDITOR_FORMAT` | `editor_format=mixed` | high |
| `RISK_MIXED_CODE_FORMAT` | `code_format=mixed` | high |
| `RISK_UNKNOWN_BLOCK` | 存在 `GB_UNKNOWN_BLOCK` 或 `GB_INACTIVE_BLOCK` | high |
| `RISK_UNKNOWN_SHORTCODE` | 存在 `SC_UNKNOWN` | high |
| `RISK_RAW_HTML` | `RAW_HTML_PRESENT` | medium |
| `RISK_IFRAME_OR_EMBED` | iframe 或 embed 数量大于 0 | medium |
| `RISK_LARGE_TABLE` | `STRUCT_LARGE_TABLE` | high |
| `RISK_MANY_LINKS` | 内部与外部链接合计至少 50 | medium |
| `RISK_MANY_IMAGES` | 图片至少 20 | medium |
| `RISK_VERY_LONG_CONTENT` | `content_text_length >= 30000` | medium |
| `RISK_PROTECTED_STRUCTURE_HEAVY` | 受保护实例总数至少 20，或受保护内容字符占可见文本加受保护内容字符至少 30% | high |
| `RISK_FORMAT_UNKNOWN` | 任一格式维度为 `unknown` | manual |
| `RISK_LANGUAGE_UNKNOWN` | `LANGUAGE_UNKNOWN` | manual |

“受保护实例”包括代码块/代码短代码、pre/code、表格、图片、gallery、video、audio、iframe/embed、原始 HTML 区块和未知短代码；嵌套表示同一实体时去重。占比的分母为可见文本字符与受保护内容字符之和，空分母按 0% 处理。

### 7.2 风险等级

- `manual-review`：命中任一 manual 规则，优先级最高。
- `high`：未命中 manual，但命中任一 high 规则；或命中至少三个不同 medium 规则。
- `medium`：未达到更高等级且命中至少一个 medium 规则。
- `low`：没有风险原因。

`risk_reasons` 保存所有命中原因，不因最终等级较高而丢弃较低严重度原因。相同原因只记录一次并按规则 ID 字典序输出。

## 8. 代表性样本

分别为每个 `primary_format` 和每个风险原因 ID 建立样本集合。每个集合按以下槽位选择，文章可占多个槽位，但集合内按文章 ID 去重：

1. 发布时间最早；
2. 发布时间最晚；
3. `content_text_length` 最大；
4. 不同 Gutenberg 区块名最多；
5. 短代码实例总数最多；
6. 独立代码格式族数最多，其次代码实例总数最多；
7. 不同 `risk_reasons` 数量最多。

每个槽位同分时按数值最小的文章 ID 选择。缺少候选或指标全为零时仍可选择，但必须记录 `selection_reason` 和指标值。样本只引用文章 ID、永久链接、分类、风险和有限证据，不复制完整生产正文。

## 9. 第一阶段处理资格

### 9.1 资格状态

格式分类与第一阶段处理资格是两个独立结论。检测器继续对全部历史格式分类；资格判断只从分类、语言、文章范围、结构完整性和风险事实中产生。

`eligibility_status` 第一版只有：

- `eligible-gutenberg-code-block-pro`：满足本节全部硬条件；
- `excluded`：至少一个硬条件不满足，必须保存全部适用的 `exclusion_reasons`。

结构合格不等于立即需要生成摘要。未来摘要命令还必须检查 `excerpt_status`，只处理 `missing` 或 `empty`，不得覆盖 `valid` 摘要。未来英文重译使用独立命令和工作流，不得与摘要生成在一次运行中同时执行。

### 9.2 eligible 硬条件

一篇文章只有同时满足下列全部条件，才可标记为 `eligible-gutenberg-code-block-pro`：

1. `post_type=post` 且 `post_status=publish`；
2. `language_source=polylang` 且规范化 `language=zh`；
3. `editor_format=gutenberg`；
4. `code_format=code-block-pro`；
5. `primary_format=gutenberg+code-block-pro`；
6. Gutenberg 区块完整配对，`GB_BLOCK_DAMAGED` 未命中；
7. 至少一个确认的 Code Block Pro 实例，且插件区块及内部结构没有残缺或冲突；
8. 未命中任何 SyntaxHighlighter 完整或损坏信号；
9. 不属于任何 editor/code mixed 或 format unknown；
10. `risk_level` 不是 `manual-review`。

第一阶段只确认内容结构是否可进入候选池。Code Block Pro 内部代码及其包装结构以后不得发送给 AI；具体的正文提取、保护、摘要与翻译规则不在当前实现范围。

### 9.3 排除原因

排除原因是可叠加的确定性 ID，去重后按字典序保存：

| 排除原因 ID | 触发条件 |
|---|---|
| `EXCLUDE_NOT_PUBLISHED_POST` | `post_type` 不是 `post` 或 `post_status` 不是 `publish` |
| `EXCLUDE_NOT_POLYLANG_ZH` | 语言可以确认，但不是 `language_source=polylang` 且 `language=zh` |
| `EXCLUDE_LANGUAGE_UNKNOWN` | Polylang 语言无法读取或规范化 |
| `EXCLUDE_NOT_GUTENBERG` | `editor_format` 不是 `gutenberg` |
| `EXCLUDE_NO_CODE_BLOCK_PRO` | 没有确认的 Code Block Pro 实例，或 `code_format` 不是 `code-block-pro` |
| `EXCLUDE_SYNTAXHIGHLIGHTER` | 命中完整或损坏的 SyntaxHighlighter 信号，包括 `SH_SHORTCODE`、`SH_BRUSH_MARKER`、`SH_HTML_CLASS`、`SH_DAMAGED` |
| `EXCLUDE_MIXED_EDITOR_FORMAT` | `editor_format=mixed` |
| `EXCLUDE_MIXED_CODE_FORMAT` | `code_format=mixed`，包括 SyntaxHighlighter 与 Code Block Pro 并存 |
| `EXCLUDE_FORMAT_UNKNOWN` | 任一格式维度为 `unknown` |
| `EXCLUDE_DAMAGED_STRUCTURE` | Gutenberg、短代码或其他受保护结构损坏 |
| `EXCLUDE_CODE_BLOCK_PRO_DAMAGED` | Code Block Pro 区块或其必要内部结构残缺、冲突或只能由弱信号推断 |
| `EXCLUDE_MANUAL_REVIEW` | `risk_level=manual-review` |

语言未知时使用 `EXCLUDE_LANGUAGE_UNKNOWN`，不必再用 `EXCLUDE_NOT_POLYLANG_ZH` 重复表达同一事实。其他原因应尽量全部保留，例如 SyntaxHighlighter 与 Code Block Pro 混用可同时产生 `EXCLUDE_SYNTAXHIGHLIGHTER` 和 `EXCLUDE_MIXED_CODE_FORMAT`。

### 9.4 暂不自动处理的格式

- classic 内容必须先迁移为 Gutenberg；其中 SyntaxHighlighter 代码迁移为 Code Block Pro。
- `gutenberg+syntaxhighlighter` 必须先迁移代码格式。
- `gutenberg/plain` 当前不处理，即使没有代码块也不进入第一阶段候选。
- mixed 表明多个已确认格式族共存，自动处理可能遗漏保护边界。
- unknown 表明证据不足、结构损坏或组合未覆盖，必须先复核。
- SyntaxHighlighter 完整或损坏内容全部进入迁移/复核队列；待残留数量确认归零后，才具备停用并删除插件的依据。

### 9.5 日期的作用

发布日期可以用于统计各格式的起止区间、选择候选批次并查找某一时期的例外，但日期不能推断或覆盖 `editor_format`、`code_format`、`primary_format`。无论文章发布日期处于哪个历史阶段，最终格式与处理资格均以正文结构检测、Polylang 归属和风险事实为准。
