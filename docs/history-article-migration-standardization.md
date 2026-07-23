# 历史文章迁移标准化日常流程设计

## 1. 文档目的与边界

本文基于 2026-07-23 的仓库只读审计，设计 Gutenberg、SyntaxHighlighter、Code Block Pro
历史文章迁移的标准化日常流程。目标是让规则、批次和进度由仓库保存，用户以后通过固定命令继续工作，
不再依赖聊天上下文。

本文只做设计，不实现控制脚本，不创建或修改状态文件，不调用生产写接口、GLM 或 SlyTranslate。
无法由当前仓库确认的事实明确标为“待确认”。

## 2. 当前仓库能力盘点

### 2.1 候选扫描与固定清单

| 能力 | 当前实现 | 可复用性与限制 |
|---|---|---|
| 全格式盘点 | `bin/build-full-inventory.py`、`data/analysis/full-format-inventory.*` | 可复用；产出 Gutenberg、代码格式、摘要、英文关联等盘点字段 |
| Code Block Pro 候选固定 | `bin/build-candidates.py` | 可复用旧 42 篇清单的构建规则；实现硬编码为 42，不适合作为每日 20 篇协调器 |
| SyntaxHighlighter 预览 | `bin/build-syntaxhighlighter-preview.py` | 可复用；从本地原始导出和翻译关联生成 `ready`、`mixed`、`abnormal` |
| SyntaxHighlighter 固定批次 | `bin/build-syntaxhighlighter-batch.py` | 可复用；拒绝覆盖输出，排除试点、旧清单和已有固定批次 ID，按 `expected-count` 校验 |
| 固定清单授权边界 | `src/candidate_execution.py` | 可复用；校验数量、唯一 ID、固定清单成员和单篇执行授权 |

审计开始时，`bin/build-syntaxhighlighter-batch.py` 和
`tests/test_syntaxhighlighter_batch.py` 已有未提交修改；本文不修改、恢复或评价这些改动的归属。

### 2.2 生产只读检查

| 能力 | 当前实现 | 可复用性与限制 |
|---|---|---|
| 旧 42 篇只读快照 | `bin/candidate-snapshot-readonly.php` | 可复用旧清单；数量硬编码为 42 |
| SyntaxHighlighter 批次验收 | `bin/validate-syntaxhighlighter-batch.py` | 可复用；读取固定批次，单次 SSH 只读获取中英文文章和 Polylang 双向关系 |
| 批量只读来源 | `src/batch_readonly_ssh.py` | 可复用；只读取固定 ID，最多 100 对，不构造写客户端 |
| 转换后结构验收 | `src/syntaxhighlighter_batch_validation.py` | 可复用；检查发布状态、标题、空摘要、双向关系、Gutenberg 平衡、SH/CBP 数量、CBP 可解析性和未知格式 |
| 单篇生产预检 | `bin/execute-single-candidate.py --preflight-live` | 可复用；两次 REST GET 加一次只读 Polylang SSH 检查，无本地写入、AI 或翻译调用 |

SyntaxHighlighter 验收会输出 `ready`、`pending`、`abnormal`。它能读取 CBP 的 `language`
属性并列出语言，但当前代码只能确认语言字段可解析，不能判断人工选择的语言是否符合原代码语义。
因此“明确语言沿用原语言、无声明使用 Plaintext”仍必须人工核对并留下确认记录。

### 2.3 摘要、翻译、状态和恢复

唯一应复用的写执行入口是 `bin/execute-single-candidate.py`，核心编排位于
`src/single_candidate_flow.py`：

- 默认模式是本地快照 dry-run。
- `--preflight-live` 是生产只读预检。
- 只有显式 `--execute` 才构造写客户端、GLM 和 SlyTranslate 客户端。
- 每次只授权一个固定中文 ID。
- 写入前按文章原子保存 `*.pre-write.json`。
- 执行状态原子写入 `chinese-<id>.execution.json`。
- 内部状态包括 `prepared`、`excerpt_rejected`、`excerpt_generated`、
  `chinese_excerpt_saved`、`translation_started`、`translation_failed`、`completed`。
- 仅摘要内容校验失败会在一次普通执行内最多尝试 3 次；其他错误不自动重试。
- `--execute --resume` 只接受 `chinese_excerpt_saved`、`translation_started` 或
  `translation_failed`，不会再次生成中文摘要。
- `translation_started` 恢复先读取生产现状；若英文摘要已存在且关系正常，则收敛为
  `completed`，避免重复调用覆盖翻译。
- Polylang 双向关系在初始读取、翻译前和翻译后检查；异常会停止当前文章。

当前本地存在 63 份主执行状态和 63 份写前备份；63 份主执行状态均为 `completed`。
未发现主状态为 `failed` 或 `pending` 的文件，也未发现 rejected 摘要文件。另有恢复目录和
人工保留的历史副本，它们不应被当作当前状态来源。

### 2.4 当前实际存在的批次与清单

| 类型 | 文件 | 行数 | 审计时状态 |
|---|---|---:|---|
| 旧 Gutenberg + Code Block Pro 固定候选 | `data/analysis/gutenberg-cbp-empty-excerpt-candidates.csv` | 42 | 清单字段仍为 `pending`；对应 42 篇执行状态实际已完成 |
| SyntaxHighlighter 试点 | `data/analysis/gutenberg-syntaxhighlighter-migration-pilot-candidates.csv` | 1 | 文章 17586；对应执行状态已完成 |
| SyntaxHighlighter 固定批次 | `data/analysis/syntaxhighlighter-migration-batch-20260722-01.csv` | 20 | 固定清单字段仍为 `pending` |
| 上批转换后验收 | `data/analysis/syntaxhighlighter-migration-batch-20260722-01-validation.csv` | 20 | `ready=20` |
| 上批执行适配清单 | `data/analysis/syntaxhighlighter-migration-batch-20260722-01-execution-candidates.csv` | 20 | 清单字段为 `pending`；对应执行状态已完成 |
| 今日 SyntaxHighlighter 固定批次 | `data/analysis/syntaxhighlighter-migration-batch-20260723-01.csv` | 20 | 已存在，`migration_status=pending`、`validation_status=not-checked` |

严格按 `syntaxhighlighter-migration-batch-*.csv` 文件名会匹配固定批次及其 validation、
execution-candidates 衍生文件。因此“固定批次数量”必须按字段/文件角色判定，而不能只按 glob：
当前有 2 个 SyntaxHighlighter 固定批次；另有 1 个旧 CBP 固定候选清单和 1 篇试点。

今日批次 `syntaxhighlighter-20260723-01` 是有效历史事实：不得重新生成、排序、补选或替换。

## 3. 当前流程的主要风险与缺失能力

1. 固定清单中的 `migration_status`、旧候选清单中的 `execution_status` 与逐篇 execution JSON
   已发生分歧，缺少一个明确且可重建的统一状态视图。
2. 没有统一命令回答“当前活动批次、下一篇、各状态数量、是否允许新建批次”。
3. 没有把“人工转换完成”和“人工语言核对完成”保存为结构化、可审计状态。
4. SyntaxHighlighter 批量验收一次生成整批新文件；没有标准化的单篇验收/重复验收记录策略。
5. 没有通用批次 runner 对 20 篇逐篇隔离异常并继续，也没有跨运行的有限重试计数。
6. 现有 `--resume` 只恢复摘要已保存后的翻译阶段，不等同于批次级 failed/pending resume。
7. 没有全局检查“中文 ID 曾进入任意固定批次即永久排除”的统一索引。
8. 没有文件锁；两个进程可同时针对同一文章启动。现有原子 rename 防止半文件，但不能防止重复 API 调用。
9. `execution.json` 是可变快照，能恢复但不能完整表达人工标记、每次失败和重试历史。
10. GLM 调用成功但状态落盘前进程中断时，无法确认该次调用；SlyTranslate 已有
    `translation_started` 生产收敛保护，但缺少外层统一运行记录。
11. 旧清单的固定字段长期保持 `pending`，说明不可变清单不应再兼任实时状态文件。

## 4. 推荐的最小架构

只增加一个轻量控制入口 `bin/history-migration.py`，负责读取、校验、锁定和协调；它不得复制候选分析、
生产验收、摘要生成或翻译实现。

```text
不可变事实：固定批次 CSV
        │
        ├── 轻量协调器：history-migration.py
        │       ├── 读取/导入已有批次
        │       ├── 维护逐篇协调状态与事件
        │       ├── 文件锁、状态转换保护、汇总
        │       └── 逐篇调用已有脚本
        │
        ├── 转换后验收：validate-syntaxhighlighter-batch.py / 现有验证模块
        └── 摘要与翻译：execute-single-candidate.py
```

协调器只负责编排，不直接访问 GLM、SlyTranslate 或 WordPress 写接口。首期不要实现常驻服务、工作流引擎、
消息队列或 SQLite。

## 5. 唯一状态来源与数据文件设计

### 5.1 推荐方案

- **固定批次 CSV：不可变分配事实。** 保存 batch ID、顺序、固定中英文 ID、标题、基线哈希和基线区块数；
  创建后永不写回状态。
- **逐篇协调状态 JSON：当前状态唯一来源。** 建议路径
  `data/state/history-migration/<batch_id>/chinese-<id>.json`。每篇独立文件，使用临时文件、
  `fsync`、`os.replace` 原子更新。
- **追加式 JSONL 事件：审计来源，不参与当前状态决策。** 建议路径
  `data/state/history-migration/<batch_id>/events.jsonl`，记录人工确认、转换、验收、尝试、错误和恢复。
  当前状态必须能由事件重放校验；若两者不一致，停止写操作并要求修复，不能猜测。
- **现有 execution JSON：摘要/翻译执行器的阶段事实。** 协调状态引用其路径、状态和 SHA-256，
  不复制 `generated_excerpt` 等敏感或大字段。
- **只读验收产物：证据。** 保留 validation CSV/JSONL 或未来的逐篇验收 JSON；协调状态只保存证据路径、
  哈希、验收时间和结果。
- **summary：纯派生输出。** `status`/`summary` 每次从固定清单和逐篇状态计算，不保存第二份可变汇总。

这里的“唯一”按事实类型划分：成员资格只看固定 CSV；协调当前状态只看逐篇协调 JSON；摘要/翻译内部阶段只看
现有 execution JSON；生产验收结论必须引用不可变验收证据。固定 CSV 中遗留的 `pending` 字段只作为历史原值，
导入后不再作为实时状态。

### 5.2 为什么暂不选择 SQLite

CSV + 独立 JSON + JSONL 易人工查看、易做 Git diff、支持原子替换和逐篇恢复，也符合现有仓库习惯。
SQLite 的事务和唯一约束更强，但当前每天 20 篇、单机串行执行，不足以抵消迁移、人工审计和维护成本。
若以后出现多主机写入、高并发或事件量显著增长，再单独评估 SQLite；当前为“待确认的未来需求”。

JSONL 追加本身不是跨进程安全事务，因此写事件和状态时必须持有同一批次锁。事件写入后先 `flush + fsync`，
再原子更新状态；中断时可由事件重建。不得直接手工编辑 current JSON。

## 6. 状态名称与生命周期

协调层使用有限状态，不照搬执行器内部细粒度状态：

| 状态 | 含义 |
|---|---|
| `awaiting_manual_conversion` | 已固定，等待后台人工转换 |
| `awaiting_manual_review` | 已标记转换完成，等待人工核对语言和数量 |
| `awaiting_validation` | 人工核对完成，等待生产只读验收 |
| `validation_failed` | 只读验收失败，需人工修复后重验 |
| `ready_for_excerpt` | 只读验收通过，可调用现有单篇执行器 |
| `excerpt_failed` | 摘要阶段失败，仍在有限重试策略内或等待人工处理 |
| `ready_for_translation_resume` | 中文摘要已保存，翻译未完成；只能走现有 `--resume` |
| `translation_failed` | 覆盖翻译失败，可在有限重试策略内 resume |
| `completed` | 中文摘要和英文覆盖翻译均确认完成 |
| `blocked` | 安全条件异常或重试耗尽，必须人工解除 |
| `paused` | 用户显式暂停；不自动执行 |

`pending` 和 `failed` 是汇总分类，不作为额外持久状态：

- `pending`：所有等待类状态及 `paused`。
- `failed`：`validation_failed`、`excerpt_failed`、`translation_failed`。
- `blocked`：单独统计。
- `completed`：仅 `completed`。

### 6.1 状态转换表

| 当前状态 | 允许的下一状态 | 触发者 | 保护条件 |
|---|---|---|---|
| 未导入 | `awaiting_manual_conversion` | 自动导入 | 固定 CSV 有效；`batch_id + chinese_post_id` 全局唯一；未出现于其他固定批次 |
| `awaiting_manual_conversion` | `awaiting_manual_review` | 人工 | 明确指定 post ID；记录操作者、时间、转换后正文哈希和观察到的 SH/CBP 数量 |
| `awaiting_manual_review` | `awaiting_validation` | 人工 | 每个代码块语言已核对；有声明则对应语言，无声明则 Plaintext；记录确认 |
| `awaiting_validation` | `ready_for_excerpt` | 自动只读验收 | 验收为 ready；SH=0；CBP 数等于转换前 SH+原 CBP；无未知格式；关系正常；摘要仍空 |
| `awaiting_validation` | `validation_failed` | 自动只读验收 | 任一验收项失败，记录结构化原因和证据 |
| `validation_failed` | `awaiting_manual_review` | 人工 | 人工修复后显式请求重验；不得自动修改文章 |
| `ready_for_excerpt` | `ready_for_translation_resume` | 现有执行器结果映射 | execution 状态为 `chinese_excerpt_saved`、`translation_started` 或 `translation_failed` |
| `ready_for_excerpt` | `completed` | 现有执行器结果映射 | execution 状态为 `completed` |
| `ready_for_excerpt` | `excerpt_failed` / `blocked` | 自动映射 | 记录错误；安全错误直接 blocked，允许重试错误受次数限制 |
| `excerpt_failed` | `ready_for_excerpt` | 自动或人工 resume | 未超过摘要阶段运行上限，且重新只读检查仍通过 |
| `ready_for_translation_resume` | `completed` | 现有 `--execute --resume` | 生产收敛或覆盖翻译成功，最终关系正常 |
| `ready_for_translation_resume` | `translation_failed` / `blocked` | 自动映射 | 记录错误；达到上限转 blocked |
| `translation_failed` | `ready_for_translation_resume` | 自动或人工 resume | 未超过翻译阶段运行上限 |
| 任意非 `completed` | `paused` | 人工 | 保存 `paused_from` 和原因 |
| `paused` | 原状态 | 人工 | 显式恢复 |
| `blocked` | 安全的前序状态 | 人工 | 必须记录解除原因；协调器重新检查保护条件 |
| `completed` | 无 | 无 | 终态，任何 run/resume 都必须拒绝 |

不能用普通 `failed` 自动回退人工转换，也不能从只读验收失败直接进入摘要。

## 7. 状态转换保护和批次不可变规则

1. 固定批次创建后，文件内容、文章顺序和数量不可变；异常、失败或删除文章均不补选。
2. 建立所有固定 CSV 的全局中文 ID 索引。文章一旦出现过，就永久禁止进入新批次，不以当前状态为条件。
3. 创建新批次必须使用确定性候选排序；`expected_count` 只校验结果，不能放宽资格。
4. 导入和执行均校验 `batch_id + chinese_post_id` 唯一；同时校验中文 ID 全局只能属于一个固定批次。
5. `before_content_sha256` 在人工转换前是基线。标记转换完成时：
   - 生产正文哈希若仍等于基线，拒绝标记，说明转换尚未发生；
   - 若在用户未标记转换期间发生非预期变化，进入 `blocked`，不能猜测变化来源；
   - 保存转换后哈希，后续验收证据必须与该哈希一致。若人工修复导致再次变化，产生新事件，不覆盖旧证据。
6. 转换后必须满足 `after_syntaxhighlighter_count=0` 和
   `after_code_block_pro_count=before_code_block_pro_count+before_syntaxhighlighter_count`。
   数量不符、Gutenberg 不平衡、CBP 不可解析、代码为空或出现未知格式均验收失败。
7. 人工语言核对是硬门槛，不能从“language 字段存在”推断语义正确。未确认不得进入只读验收通过后的执行阶段。
8. Polylang 中英文状态或双向关系异常，停止当前文章并置 `blocked`；不得影响后续文章。
9. 只读验收不是永久通行证。调用 `execute-single-candidate.py` 前仍使用其 live validation/preflight
   检查当前摘要、正文哈希、状态、结构和关系。
10. 中文摘要未成功保存，不得调用英文覆盖翻译；此顺序继续由现有执行器保证。
11. `completed` 是不可逆终态；即使用户重复运行命令，也不得再次调用 GLM 或 SlyTranslate。

## 8. 单篇失败隔离、重试和 resume

### 8.1 隔离规则

- 批次 runner 始终按固定 CSV 顺序逐篇处理，每篇外层单独捕获异常。
- 一篇失败写入事件和协调状态后立即处理下一篇，进程级配置错误除外。
- 配置错误包括清单损坏、状态目录不可写、锁机制失效或凭证整体缺失。这类错误允许停止本次运行，
  但不得改变尚未处理文章的状态。
- 最终始终输出 `completed`、`failed`、`pending`、`blocked` 数量及 ID。

### 8.2 有限重试

- 保留现有摘要内容校验“一次执行最多 3 次”的内部规则，不在协调器里复制。
- 协调器的**运行级自动重试建议上限为每阶段 2 次**（首次运行加 1 次自动重试）。
  这是推荐设计值，实施前待用户确认。
- 安全类错误不自动重试：哈希变化、Polylang 异常、发布状态异常、清单不一致、结构/数量验收失败、
  completed 再执行请求。
- 可自动重试仅限明确的瞬时错误，例如超时、连接中断、HTTP 429/5xx；错误分类映射需在实施阶段用测试固定。
- 每次尝试记录 `stage`、`attempt`、开始/结束时间、错误类型、脱敏原因和结果；达到上限转 `blocked`。
- 禁止递归和无限循环；批次 resume 也不能重置累计次数。

### 8.3 Resume 语义

- `resume` 只选择协调状态为等待类、`excerpt_failed`、`ready_for_translation_resume` 或
  `translation_failed` 的文章；默认跳过 `paused`，拒绝 `completed` 和 `blocked`。
- 对未开始摘要的 pending 文章，执行普通单篇入口。
- 对 execution JSON 已进入 `chinese_excerpt_saved`、`translation_started` 或
  `translation_failed` 的文章，只调用现有 `--execute --resume`。
- `excerpt_rejected` 当前不能用现有 `--resume`；在运行级次数未耗尽时可显式重新普通执行，
  复用现有写前备份和安全检查。耗尽后 blocked。
- 恢复前先协调生产现状与 execution JSON。无法判断“API 是否成功”时不得盲目重发：
  SlyTranslate 使用现有 `translation_started` 收敛；其他无法收敛情形进入 blocked。

## 9. 固定命令入口设计

推荐唯一入口：

```bash
python3 bin/history-migration.py <command> [options]
```

建议的最小命令集：

| 命令 | 作用 | 是否写生产 |
|---|---|---|
| `status [--batch BATCH_ID]` | 显示活动批次、逐篇状态、锁和阻塞原因 | 否 |
| `summary [--batch BATCH_ID]` | 输出 completed/failed/pending/blocked 汇总 | 否 |
| `prepare --batch-id ID --count 20` | 调用现有候选/固定批次实现；若目标或活动批次冲突则拒绝 | 否 |
| `import-batch --batch PATH` | 幂等接管已有固定批次，不改 CSV | 否 |
| `show-current` | 显示唯一活动批次和下一批人工任务 | 否 |
| `mark-converted --batch ID --post-id ID` | 标记人工转换完成并记录只读观察值 | 否 |
| `confirm-languages --batch ID --post-id ID` | 记录人工语言核对完成 | 否 |
| `validate --batch ID [--post-id ID]` | 复用现有生产只读验收模块，保存证据 | 否 |
| `run --batch ID --post-id ID` | 通过保护后调用现有单篇执行器 | 是，必须明确 post ID |
| `resume --batch ID [--post-id ID]` | 仅处理允许恢复的 pending/failed；逐篇隔离 | 可能 |

`run` 和 `resume` 不实现摘要或翻译，只组装并调用
`bin/execute-single-candidate.py` 所需的固定 manifest、expected count、backup dir 和模式。
第一版不提供“无参数写完整批次”的快捷命令；批量 resume 如需启用，必须打印固定 ID 集并要求显式
`--execute` 授权，具体交互方式待确认。

## 10. 每日标准操作流程

1. `status`：先查看唯一活动批次、锁、失败和 blocked。
2. 若有未完成活动批次，默认继续该批次，不新建批次。
3. `show-current`：领取下一篇 `awaiting_manual_conversion` 文章。
4. 用户在 WordPress 后台人工把每个 SyntaxHighlighter 区块转换为 Code Block Pro。
5. `mark-converted`：只记录转换事实和只读观察，不写 WordPress。
6. 人工逐块核对语言；明确语言沿用对应语言，无声明使用 Plaintext；运行 `confirm-languages`。
7. `validate --post-id`：执行生产只读验收。失败则人工修复并重新确认，不生成摘要。
8. `run --post-id`：只对 `ready_for_excerpt` 调用现有单篇执行器。
9. 单篇失败记录后继续下一篇；可用 `resume --post-id` 恢复允许的阶段。
10. `summary`：结束时输出 completed、failed、pending、blocked。

### 10.1 是否允许同时创建下一批

默认**不允许**存在两个含义不清的活动批次。只要当前批次还有
`awaiting_manual_conversion`、`awaiting_manual_review`、`awaiting_validation`、
`ready_for_excerpt`、可重试 failed 或 `ready_for_translation_resume`，就拒绝 `prepare`。

仅当当前批次满足以下之一时才允许下一批：

- 全部 `completed`；或
- 所有未完成文章均由用户显式置为 `paused`/`blocked`，并给出原因，同时批次被显式标记为
  `closed_with_exceptions`。

`closed_with_exceptions` 是批次级派生结论，不是文章状态。创建下一批不会补选旧批次异常文章；
旧文章以后仍按原 batch ID 恢复。是否允许在存在 `closed_with_exceptions` 时长期并行恢复多个旧批次，
实施前待用户确认；第一版建议每次写操作仍必须显式指定 batch ID。

## 11. 已有脚本复用关系

| 新入口职责 | 直接复用 | 不应重复实现 |
|---|---|---|
| SyntaxHighlighter 扫描 | `build-syntaxhighlighter-preview.py` | 内容分析、mixed/abnormal 分类 |
| 固定每日清单 | `build-syntaxhighlighter-batch.py` | 排序、排除、原子 CSV 写入 |
| 旧 CBP 清单读取 | `build-candidates.py` 产物和 `candidate_execution.py` 校验 | 另一套 CBP 生成器 |
| 转换后只读验收 | `syntaxhighlighter_batch_validation.py`、`batch_readonly_ssh.py` | WordPress/Polylang 读取器 |
| 单篇生产预检 | `execute-single-candidate.py --preflight-live` | 新 preflight 客户端 |
| 摘要与英文覆盖 | `execute-single-candidate.py --execute` | 新摘要器、翻译器、完整执行管线 |
| 翻译恢复 | `execute-single-candidate.py --execute --resume` | 新翻译 resume 实现 |
| 原子备份/状态 | `candidate_execution.py` 的写入函数或同等模式 | 非原子覆盖写 |

当前 SyntaxHighlighter 固定 CSV 不能直接作为 `execute-single-candidate.py` manifest；
上批存在 execution-candidates 适配文件。协调器应把“从验收通过的固定批次生成执行器所需视图”
设计为确定性、幂等的派生步骤，不能成为新的候选选择器，也不能改变固定成员。该适配视图的确切字段来源、
是否持久化以及英文基线哈希采集方式需要结合上批构建过程进一步确认，当前仓库未发现对应的显式构建脚本，
故标记为**待确认**。

## 12. 已有批次接管方案

`import-batch` 必须只读固定 CSV，并满足：

1. 计算文件 SHA-256，验证 batch ID、序号、行数、中文/英文 ID 唯一和必需字段。
2. 扫描所有固定批次建立全局中文 ID 索引；衍生 validation/execution CSV 不计作新批次。
3. 若状态不存在，按固定行顺序创建逐篇初始状态。
4. 若状态已存在且 `batch_id`、post ID、行序号和清单哈希一致，返回 already-imported，不写新事件。
5. 若同一 ID 指向不同批次或清单哈希变化，停止并 blocked，绝不重建或覆盖。
6. 已有 execution JSON 为 `completed` 时，可在校验固定 ID、英文 ID、状态文件结构及生产只读现状后，
   导入为 `completed`；未做生产确认前只能标记为“待核对”，不能仅凭文件名推断。
7. 已有 validation `ready` 可作为验收证据，但必须校验它的 batch ID、20 个固定 ID、基线字段和文件哈希。

### 12.1 `syntaxhighlighter-20260723-01`

该文件已存在且包含 20 篇，导入时：

- 保持原顺序和全部字段；
- 不重新扫描、选择或生成；
- 不自动补足或替换；
- 因目前没有对应 validation 和 execution-candidates 文件，初始化为
  `awaiting_manual_conversion`；
- 多次导入返回相同结果，不重复事件；
- 如果文章实际已被人工转换但仓库无证据，应显示“待确认”，由用户按标准人工确认和只读验收接管，
  不根据正文差异自动宣称转换完成。

旧 42 篇、试点和 20260722 批次的接管也必须幂等。当前 63 份 completed execution 状态与
42+1+20 的数量吻合，但 ID 对应关系和生产最终状态仍应由导入审计逐项验证，不能只凭数量确认。

## 13. 幂等、并发与重复执行保护

1. **全局准备锁**：`prepare`/`import-batch` 持有全局文件锁，防止两个批次同时分配同一 ID。
2. **批次锁**：状态/事件更新和批次级 resume 持有 batch 锁。
3. **文章锁**：任何 validate/run/resume 对
   `batch_id + chinese_post_id` 加非阻塞独占锁；已锁定就明确失败，不等待并重复执行。
4. **唯一键**：`batch_id + chinese_post_id` 是状态键；中文 ID 另有跨批次全局唯一约束。
5. **原子写入**：current JSON、验收证据和派生 manifest 使用同目录临时文件、`flush`、`fsync`、
   `os.replace`；存在的不可变文件拒绝覆盖。
6. **幂等键**：每次阶段尝试使用
   `<batch_id>:<chinese_post_id>:<stage>:<attempt>`，事件中唯一。重复命令先读 current 和 execution
   状态，不直接调用外部 API。
7. **API 前后记录**：
   - 调用前持锁写 `attempt_started`，包括执行器状态哈希；
   - 调用后写结果事件，再更新 current；
   - SlyTranslate 中断依赖现有 `translation_started` 生产收敛；
   - GLM 调用与本地状态不能形成跨系统原子事务，若结果未写入即中断，恢复时记录 uncertain；
     由于尚未写 WordPress，可在运行上限内重新生成，但必须计入新 attempt，不能假装 exactly-once。
8. **completed 短路**：协调状态或可信 execution 状态为 completed 时，所有写命令在构造 API 客户端前拒绝。
9. **锁的实现**：Linux 环境优先 `fcntl.flock`；锁文件只含 PID、主机、开始时间和命令元数据。
   进程退出自动释放锁，不能仅靠“锁文件存在”判断运行中。

第一版采用串行逐篇处理。并发执行 20 篇带来的速度收益不值得 API 重复和状态竞争风险。

## 14. 分阶段最小实施计划

每阶段独立提交、独立验证，不一次性重构：

1. **只读状态审计和设计确认**：确认本文状态名、运行级重试上限、活动批次规则和数据目录是否纳入 Git。
2. **实现批次与状态读取**：只读识别固定批次角色、全局 ID 唯一、execution/validation 证据；不写生产。
3. **实现 `status` 和 `summary`**：从事实来源派生输出；测试旧清单字段与 execution JSON 分歧。
4. **接管已有批次**：实现幂等 `import-batch`，优先接管
   `syntaxhighlighter-20260723-01`；测试重复导入、清单篡改和跨批次重复 ID。
5. **人工转换完成标记和只读验收入口**：实现 `mark-converted`、
   `confirm-languages`、`validate`；复用现有验证模块，不写 WordPress。
6. **复用单篇执行器**：只实现参数适配和状态映射；先单篇显式 ID，不实现整批自动写。
7. **有限重试和 resume**：实现错误分类、尝试计数、逐篇隔离和现有 `--resume` 路由。
8. **测试**：状态转换表、锁、原子写、中断恢复、completed 短路、API 未调用断言、20 篇一篇失败继续。
9. **更新 README**：仅在实现稳定后加入每日命令；本文阶段不修改。
10. **提交 Git**：审阅 diff 和数据隐私后由用户明确授权；不自动 commit/push。

每一阶段都必须保持现有 `execute-single-candidate.py` 可独立使用，避免协调层成为不可逆迁移。

## 15. 本方案不会做什么

- 不开发新的摘要生成器、翻译器或第二套完整执行管线。
- 不自动转换 SyntaxHighlighter/其他旧格式代码块。
- 不自动判断代码语言语义，不取消人工语言核对。
- 不在只读验收失败时生成摘要或翻译。
- 不替换、补选、重排或覆盖任何已固定批次。
- 不把 failed 文章移入新批次。
- 不自动重新执行 completed 文章。
- 不无限重试，不并行写 20 篇。
- 不引入服务、队列、Web UI、SQLite 或通用工作流引擎。
- 不把聊天记录作为状态来源。
- 不在协调状态中复制文章正文、生成摘要或凭证。
- 不自动关闭 blocked；人工解除必须有原因和重新检查。

## 16. 待确认事项

1. 运行级“首次加 1 次自动重试”（每阶段最多 2 次）是否符合用户预期。
2. `data/state/history-migration/` 是否纳入 Git；其中不得保存凭证或完整文章正文。
3. 上批 execution-candidates 的准确生成命令/脚本目前未在仓库中发现，字段适配来源待确认。
4. 人工语言确认需要逐块记录语言映射，还是只记录整篇确认及验收输出，待确认。
5. `closed_with_exceptions` 后是否允许恢复多个旧批次；第一版建议所有写命令显式 batch ID 且串行。
6. 今日批次文章是否已发生任何后台人工转换，仓库目前无对应证据，待确认。
