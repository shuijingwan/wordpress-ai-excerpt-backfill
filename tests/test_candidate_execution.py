import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.candidate_execution import (ExcerptValidationError, SafetyError, authorize_live_selection,
    backup_record, dry_run, guarded_pipeline, select_inventory_rows, validate_generated_excerpt,
    validate_manifest, write_backup)

POST_9452_EXCERPTS = (
    "在 VS Code 中按下 Ctrl + S 时文件内容意外被还原，检查设置发现 Editor: Format On Save 已开启，且提示配置已在其他位置修改。经排查，这并非撤销操作，而是 Go 语言的自动格式化工具将代码强制改回了合法状态。将 math.pi 修正为 math.Pi 后，Ctrl + S 即可正常保存文件。",
    "针对在 VS Code 中按下 Ctrl + S 后文件内容意外还原至上一步而非保存的问题，检查发现 Editor: Format On Save 设置已开启。经排查，该现象并非保存功能异常，而是 Go 语言自动格式化工具在保存时将代码修正为合法状态所致，修正具体写法后即可正常保存。",
    "在 VS Code 中按下 Ctrl + S 后文件意外还原至修改前状态，经检查发现 Editor: Format On Save 设置已开启。实际操作并非撤销，而是 Go 的自动格式化工具在保存时将代码修正为合法状态。排查发现源码中将 math.pi 改为 math.Pi 后，文件即可正常保存，解决了因格式化规则导致的保存还原问题。",
)

POST_8669_REJECTED_EXCERPT = (
    "在 Laravel 9 中执行数据库迁移时遇到 SQLSTATE[HY000] 错误，提示 Incorrect DECIMAL value。"
    "将报错的 SQL 在 Navicat for MySQL 中执行后依然报错。经检查迁移文件和表结构，发现表中字段 "
    "stock_out_sort 存在空字符串值。将该空字符串手动修改为 99801708945158 后再次执行数据库迁移，"
    "操作成功且不再报错。"
)


def digest(value="x"):
    return hashlib.sha256(value.encode()).hexdigest()


def manifest(count=42):
    return [{
        "chinese_post_id": str(index), "chinese_title": f"标题 {index}",
        "chinese_content_sha256": digest(f"zh-{index}"), "chinese_excerpt_empty": "True",
        "english_post_id": str(1000 + index), "english_post_status": "publish",
        "english_title_sha256": digest(f"t-{index}"),
        "english_excerpt_sha256": digest(f"e-{index}"),
        "english_content_sha256": digest(f"c-{index}"),
        "candidate_reason": "fixed", "execution_status": "pending",
    } for index in range(1, count + 1)]


def live(row):
    return {
        "chinese_exists": True, "chinese_status": "publish", "chinese_language": "zh",
        "chinese_excerpt_empty": True, "chinese_content_sha256": row["chinese_content_sha256"],
        "is_gutenberg": True, "has_code_block_pro": True, "phase1_eligible": True,
        "linked_english_post_id": int(row["english_post_id"]), "english_status": "publish",
        "english_title_sha256": row["english_title_sha256"],
        "english_excerpt_sha256": row["english_excerpt_sha256"],
        "english_content_sha256": row["english_content_sha256"],
        "chinese_title": "标题", "chinese_content": "正文",
    }


class CandidateSelectionTest(unittest.TestCase):
    def eligible(self, **changes):
        row = {"post_id": "1", "category": "gutenberg-code-block-pro", "excerpt_empty": "True",
               "phase1_eligible": "True", "has_english_translation": "True",
               "english_post_status": "publish"}
        row.update(changes)
        return row

    def test_only_empty_gutenberg_cbp_is_selected(self):
        selected = select_inventory_rows([self.eligible(), self.eligible(post_id="2", excerpt_empty="False")])
        self.assertEqual(["1"], [row["post_id"] for row in selected])

    def test_non_cbp_and_phase1_ineligible_are_excluded(self):
        rows = [self.eligible(category="gutenberg-without-code-block-pro"),
                self.eligible(post_id="2", phase1_eligible="False")]
        self.assertEqual([], select_inventory_rows(rows))

    def test_missing_or_abnormal_english_relation_is_excluded(self):
        rows = [self.eligible(has_english_translation="False"),
                self.eligible(post_id="2", english_post_status="draft")]
        self.assertEqual([], select_inventory_rows(rows))


class SafetyBoundaryTest(unittest.TestCase):
    def test_custom_expected_count_allows_isolated_single_manifest(self):
        self.assertEqual(
            [1], authorize_live_selection(manifest(1), [1], expected_count=1)
        )

    def test_count_other_than_42_is_rejected(self):
        with self.assertRaisesRegex(SafetyError, "exactly 42"):
            validate_manifest(manifest(41))

    def test_id_outside_manifest_is_rejected(self):
        with self.assertRaisesRegex(SafetyError, "outside"):
            authorize_live_selection(manifest(), [999])

    def test_default_live_limit_is_one(self):
        with self.assertRaisesRegex(SafetyError, "defaults to one"):
            authorize_live_selection(manifest(), [1, 2])
        self.assertEqual([1, 2], authorize_live_selection(manifest(), [1, 2], batch_authorized=True))

    def test_dry_run_calls_no_ai_and_writes_nothing(self):
        rows = manifest(); snapshots = {int(r["chinese_post_id"]): live(r) for r in rows}
        result = dry_run(rows, snapshots, range(2001, 2047))
        self.assertEqual((42, 0, 0, 0), (result["passed"], result["skipped"],
                                            result["ai_api_calls"], result["wordpress_writes"]))
        self.assertEqual(0, result["ssh_readonly_calls"])
        self.assertEqual(0, result["translation_calls"])
        self.assertTrue(result["protected_46_excluded"])

    def test_nonempty_excerpt_changed_content_and_relation_are_each_rejected(self):
        for field, value, reason in (
            ("chinese_excerpt_empty", False, "chinese_excerpt_not_empty"),
            ("chinese_content_sha256", digest("changed"), "chinese_content_changed"),
            ("linked_english_post_id", 9999, "english_relation_changed"),
        ):
            with self.subTest(field=field):
                rows = manifest(); snapshots = {int(r["chinese_post_id"]): live(r) for r in rows}
                snapshots[1][field] = value
                result = dry_run(rows, snapshots)
                self.assertEqual(1, result["skipped"])
                self.assertEqual(1, result["skip_reasons"][reason])

    def test_protected_posts_cannot_overlap_manifest(self):
        rows = manifest(); snapshots = {int(r["chinese_post_id"]): live(r) for r in rows}
        with self.assertRaisesRegex(SafetyError, "protected"):
            dry_run(rows, snapshots, [1] + list(range(2001, 2046)))

    def test_excerpt_failure_stops_translation_and_writes(self):
        calls = []
        row = manifest()[0]
        with self.assertRaises(SafetyError):
            guarded_pipeline(row, live(row), lambda *_: "", lambda *_: calls.append("translate"),
                             lambda _: True, lambda *_: calls.append("write-zh"),
                             lambda *_: calls.append("write-en"))
        self.assertEqual([], calls)

    def test_translation_validation_failure_writes_neither_post(self):
        calls = []
        row = manifest()[0]
        with self.assertRaisesRegex(SafetyError, "translation validation"):
            guarded_pipeline(row, live(row), lambda *_: "这篇文章说明一个具体技术问题的背景、排查思路和完整操作过程，并根据实际执行结果总结最终结论，同时保留关键技术名称，避免加入原文没有提及的效果、判断或营销表达，可直接作为博客文章的中文摘要使用。",
                             lambda *_: {"title": "T"}, lambda _: False,
                             lambda *_: calls.append("write-zh"), lambda *_: calls.append("write-en"))
        self.assertEqual([], calls)

    def test_excerpt_rejects_markup_url_and_code_fence(self):
        for suffix in ("<code>x</code>", "https://example.com", "```x```"):
            with self.assertRaises(SafetyError):
                validate_generated_excerpt("这篇文章说明一个具体技术问题的背景、排查思路和完整操作过程，并根据实际执行结果总结最终结论，同时保留关键技术名称，避免加入原文没有提及的效果、判断或营销表达，可直接作为博客文章的中文摘要使用。" + suffix)

    def test_markdown_rejection_preserves_raw_excerpt(self):
        raw = "- " + "这是一段包含列表标记的原始模型摘要文本。" * 8
        with self.assertRaises(ExcerptValidationError) as raised:
            validate_generated_excerpt(raw)
        self.assertEqual(raw, raised.exception.raw_excerpt)
        self.assertEqual("generated Chinese excerpt contains Markdown or a list", str(raised.exception))

    def test_plain_single_paragraph_excerpt_is_accepted(self):
        raw = "这篇文章介绍一个具体技术问题的背景和排查过程，说明关键操作步骤、实施条件及其原因，并依据实际执行结果总结解决方案和注意事项，同时准确保留相关产品与技术名称，避免加入原文没有提及的判断，可直接作为中文摘要保存。"
        self.assertEqual(raw, validate_generated_excerpt(raw))

    def test_real_php_magic_method_excerpt_is_plain_text(self):
        raw = (
            "本文记录了在 Lighthouse 中排查 resolver 类方法 __invoke 未执行的过程。"
            "通过打印日志、调整 GraphQL 架构定义和拆分文件，确认问题与解析类无关。"
            "分析调用栈发现构造方法正常执行，但 __invoke 未调用。对比正常工作的 "
            "resolver 类并删除该类 __invoke 方法中的 yield 循环后，方法成功执行，"
            "最终确认问题由 yield 循环导致。")
        self.assertEqual(raw, validate_generated_excerpt(raw))

    def test_php_magic_method_identifiers_are_not_markdown(self):
        raw = (
            "文章比较 __construct、__invoke、__toString、__callStatic 和 "
            "__debugInfo 等 PHP 魔术方法的调用时机，并说明如何通过日志确认对象构造、"
            "动态调用和字符串转换流程，最终根据实际调用栈定位实现中的控制流问题。")
        self.assertEqual(raw, validate_generated_excerpt(raw))

    def test_paired_markdown_emphasis_remains_rejected(self):
        body = "这是一段足够长的普通中文技术摘要，用于说明问题背景、排查过程、修改方法和最终验证结果。"
        for emphasized in ("**加粗文本**", "__加粗文本__"):
            with self.subTest(emphasized=emphasized), self.assertRaisesRegex(
                    ExcerptValidationError, "Markdown or a list"):
                validate_generated_excerpt(body + emphasized, minimum=1)

    def test_sqlstate_codes_are_not_treated_as_shortcodes(self):
        cases = (
            "在 Laravel 9 中执行迁移时遇到 SQLSTATE[HY000] 错误。",
            "查询失败并返回 SQLSTATE[23000]: Integrity constraint violation。",
            "MySQL 返回 SQLSTATE[42S02]，表示数据表不存在。",
            "错误代码为 SQLSTATE [HY000]。",
            "错误代码为 SQLSTATE [23000]。",
            "错误代码为 SQLSTATE [42S02]。",
            "错误代码为 sqlstate[hy000]。",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(raw, validate_generated_excerpt(raw, minimum=1))

    def test_real_post_8669_rejected_excerpt_is_now_accepted(self):
        self.assertEqual(
            POST_8669_REJECTED_EXCERPT,
            validate_generated_excerpt(POST_8669_REJECTED_EXCERPT),
        )

    def test_real_shortcodes_and_html_remain_rejected(self):
        cases = (
            '[caption id="attachment_123"]图片[/caption]',
            '[gallery ids="1,2,3"]',
            '正文包含 [custom_shortcode]。',
            '错误代码为 <code>SQLSTATE[HY000]</code>。',
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ExcerptValidationError):
                validate_generated_excerpt(raw, minimum=1)

    def test_invalid_or_context_free_sqlstate_brackets_are_not_exempt(self):
        for raw in ("[HY000]", "SQLSTATE[HY00]", "SQLSTATE[HY0000]", "SQLSTATE[HY-00]"):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    ExcerptValidationError, "HTML or shortcode"):
                validate_generated_excerpt(raw, minimum=1)

    def test_post_9452_real_excerpts_are_accepted(self):
        for raw in POST_9452_EXCERPTS:
            with self.subTest(raw=raw):
                self.assertEqual(raw, validate_generated_excerpt(raw))

    def test_inline_technical_operators_are_not_lists(self):
        cases = (
            "文章说明在编辑器中使用 Ctrl + S 保存文件时触发自动格式化的原因，并介绍如何检查配置和修正源码，使文件能够按预期正常保存，同时避免将格式化行为误认为撤销操作。",
            "文章使用 A + B 表示两个输入值相加，通过完整示例介绍表达式的计算过程、输入条件和输出结果，并说明排查异常结果时需要核对的数据类型与运算顺序，最后根据实际运行结果总结验证方法和相关注意事项。",
            "文章介绍 C++ 项目的构建配置和故障排查过程，说明编译器选项、依赖关系及源码调整方法，并根据实际构建结果总结解决方案和注意事项，同时给出验证配置是否正确的操作思路和判断依据。",
            "文章分析 x - y 一类普通减法表达式及带有连字符的技术名称，说明输入数据、计算步骤和验证结果，并总结避免常见配置错误的方法和相关注意事项，帮助读者根据实际输出确认调整是否生效。",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(raw, validate_generated_excerpt(raw))

    def test_markdown_headings_and_lists_are_rejected(self):
        body = "这篇文章介绍一个具体技术问题的背景和排查过程，说明关键操作步骤及其原因，并依据实际结果总结解决方案，同时准确保留相关产品与技术名称，可直接作为中文摘要保存。"
        for prefix in ("# ", "- ", "* ", "+ ", "1. ", "1) "):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(
                    ExcerptValidationError,
                    "generated Chinese excerpt contains Markdown or a list"):
                validate_generated_excerpt(prefix + body)

    def test_list_after_newline_and_multiple_paragraphs_are_rejected(self):
        body = "这篇文章介绍一个具体技术问题的背景和排查过程，说明关键操作步骤及其原因，并依据实际结果总结解决方案，同时准确保留相关产品与技术名称，可直接作为中文摘要保存。"
        for raw in (body + "\n- 第一项", body + "\n\n另一个段落"):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    ExcerptValidationError,
                    "generated Chinese excerpt must be one paragraph"):
                validate_generated_excerpt(raw)

    def test_per_post_backup_is_private_atomic_and_not_overwritten(self):
        row = manifest()[0]
        before = live(row) | {"chinese_excerpt": "", "english_title": "Old title",
                              "english_excerpt": "Old excerpt", "english_content": "Old content"}
        record = backup_record(row, before, executed_at="2026-07-21T00:00:00Z",
                               model="glm-4.7", request_id="request-1")
        with tempfile.TemporaryDirectory() as directory:
            path = write_backup(Path(directory) / "backups", record)
            self.assertEqual(record, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            with self.assertRaisesRegex(SafetyError, "overwrite"):
                write_backup(path.parent, record)


if __name__ == "__main__":
    unittest.main()
