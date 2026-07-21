# 固定 42 篇候选的执行边界

候选唯一来源是 `data/analysis/full-format-inventory.csv`。只有同时满足正式盘点中的
`category=gutenberg-code-block-pro`、`excerpt_empty=True`、`phase1_eligible=True`、已有英文关联且英文状态为
`publish` 的行才能进入固定清单。清单数量不是 42 时，任何后续阶段都拒绝运行。

Dry Run 只读取逐篇现状快照，不调用 AI，也不写 WordPress。每篇必须同时通过：明确中文 ID、中文发布状态与
Polylang 语言、摘要仍为空、中文正文 SHA-256、Gutenberg 与 Code Block Pro 结构、第一阶段资格、固定英文 ID、
当前 Polylang 关联、英文发布状态，以及英文标题、摘要、正文 SHA-256。失败只跳过该篇，不寻找替代文章。

真实执行固定为 1 篇，必须同时提供 `--post-id ID --execute`。不提供 `--execute` 时始终使用本地只读快照 Dry Run，
不会读取凭证、调用 GLM、写 WordPress 或调用覆盖翻译接口。`--resume` 还必须同时提供 `--execute`，并且只接受已保存
中文摘要后的状态，不会再次生成摘要。

真实单篇流程不读取 REST 的 `lang`、`translations` 或 `pll_translations`。它在初次/恢复读取后、调用覆盖翻译之前、
以及覆盖成功后分别通过 `PolylangSshChecker` 验证固定中英文 ID、`zh`/`en` 语言和关联。任何一次变化都会停止后续阶段；
最后一次检查不重复读取文章正文。

`translation_started` 的恢复会先进行只读收敛：核对固定 ID、发布状态、已保存中文摘要、中文标题/正文不变、英文
标题/正文非空及双向 Polylang 关系。若英文摘要已经非空，则不再次调用覆盖接口，直接原子写入 `completed`、
`completed_at` 和固定 `translated_post_id`；只有英文摘要仍为空时才按原恢复流程调用一次覆盖翻译。

`--preflight-live` 是独立只读模式，只读取 `WP_ADMIN_COOKIE` 和 `WP_REST_NONCE`，对清单中的固定中英文 ID 各执行一次
WordPress REST GET，并通过固定 SSH 别名执行一次只读 Polylang 查询。它不读取 GLM Key、不发送 POST、不调用覆盖翻译，
也不写备份或状态。输出仅包含响应字段名、字段存在性、状态、结构、哈希比对和 Polylang 的语言/关联结果；不会输出
标题、摘要或正文。REST 缺少 `lang` 或 `translations` 时不会猜测；语言和关联资格只由服务器端 Polylang 函数确认。

未来逐篇写入前必须先原子保存一份独立备份，包含中英文 ID、原中文摘要、原英文标题/摘要/正文、各字段
SHA-256、执行时间、模型、请求 ID 和状态。恢复按该文章备份执行，不依赖全库恢复。

摘要只使用当前中文标题以及移除代码区块和标记后的自然语言正文，目标长度为 160～240 字，硬范围为 80～300 字。
中文 REST 更新体只包含 `excerpt`。重新 GET 并确认摘要、状态、标题和正文后，才调用 SlyTranslate 覆盖接口；该接口
内部负责英文内容保护及写入。覆盖失败保留中文摘要和失败状态，可通过 `--resume` 从覆盖阶段继续。

若 GLM 已返回 `choices[0].message.content` 但摘要校验失败，原始文本会原子保存到
`data/backups/single-candidate/rejected/`，目录权限为 `0700`、文件权限为 `0600`。状态记为 `excerpt_rejected`，只保存
错误和文件路径，不复制摘要正文。该状态禁止 `--resume`；普通 `--execute` 会复用已验证的写入前备份、重新执行安全
检查并重新调用 GLM。拒绝发生后不会发送 WordPress 更新或覆盖翻译请求。

单次普通执行只对 `ExcerptValidationError` 最多尝试 3 次（首次加两次重试），不使用递归。每次拒绝均以包含尝试序号
和 UTC 时间的独立文件保留。其他安全、网络、WordPress、Polylang 或翻译错误一律不自动重试。只有摘要通过后才记录
`excerpt_generated` 并开始写入；成功状态中的 `excerpt_attempts` 记录实际使用次数。
