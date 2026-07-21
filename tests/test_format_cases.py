import json
from pathlib import Path
import unittest

from src.analyzer import analyze_content
from src.fixture_expansion import expand_fixture_content


ROOT = Path(__file__).resolve().parents[1]


class FormatCasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "config/classification.json").open(encoding="utf-8") as handle:
            cls.config = json.load(handle)
        cls.cases = []
        with (ROOT / "tests/fixtures/format-cases.jsonl").open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    cls.cases.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise AssertionError(f"invalid fixture JSON on line {line_number}: {error}") from error

    def test_all_format_cases(self):
        self.assertGreaterEqual(len(self.cases), 24)
        required_case_ids = {
            "unclosed_syntaxhighlighter",
            "orphan_syntaxhighlighter_close",
            "escaped_syntaxhighlighter",
            "valid_block_with_malformed_open",
            "valid_block_with_malformed_close",
            "orphan_gutenberg_close",
            "mismatched_gutenberg_names",
        }
        self.assertTrue(required_case_ids <= {case["case_id"] for case in self.cases})
        required = {
            "case_id", "description", "content", "expected_editor_format",
            "expected_code_format", "expected_primary_format", "expected_rule_ids",
            "expected_risk_level", "expected_risk_reasons",
        }
        for case in self.cases:
            with self.subTest(case_id=case.get("case_id")):
                self.assertTrue(required <= case.keys())
                content = expand_fixture_content(case)
                result = analyze_content(content, self.config)
                self.assertEqual(case["expected_editor_format"], result["editor_format"])
                self.assertEqual(case["expected_code_format"], result["code_format"])
                self.assertEqual(case["expected_primary_format"], result["primary_format"])
                self.assertEqual(set(case["expected_rule_ids"]), set(result["matched_rule_ids"]))
                self.assertEqual(case["expected_risk_level"], result["risk_level"])
                self.assertEqual(set(case["expected_risk_reasons"]), set(result["risk_reasons"]))

    def test_unknown_expansion_type_raises(self):
        with self.assertRaises(ValueError):
            expand_fixture_content({"content": "x", "fixture_expansion": {"type": "unknown"}})

    def test_negative_expansion_count_raises(self):
        with self.assertRaises(ValueError):
            expand_fixture_content({
                "content": "x",
                "fixture_expansion": {"type": "repeat-text", "text": "x", "count": -1},
            })

    def test_code_block_pro_internal_html_is_not_mixed(self):
        case = next(item for item in self.cases if item["case_id"] == "gutenberg_code_block_pro")
        result = analyze_content(expand_fixture_content(case), self.config)
        self.assertEqual("code-block-pro", result["code_format"])
        self.assertNotIn("classic-pre-code", result["code_format_families"])

    def test_html_inside_gutenberg_block_is_not_mixed(self):
        content = "<!-- wp:paragraph --><p><strong>区块内部 HTML</strong></p><!-- /wp:paragraph -->"
        result = analyze_content(content, self.config)
        self.assertEqual("gutenberg", result["editor_format"])
        self.assertNotIn("CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS", result["matched_rule_ids"])

    def test_whitespace_around_gutenberg_blocks_is_not_mixed(self):
        content = "\n\t<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->\r\n"
        result = analyze_content(content, self.config)
        self.assertEqual("gutenberg", result["editor_format"])
        self.assertNotIn("CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS", result["matched_rule_ids"])

    def test_empty_paragraph_between_or_after_blocks_is_not_mixed(self):
        contents = [
            "<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph --><p></p>"
            "<!-- wp:paragraph --><p>B</p><!-- /wp:paragraph -->",
            "<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph --><p>   </p>",
        ]
        for content in contents:
            with self.subTest(content=content):
                result = analyze_content(content, self.config)
                self.assertEqual("gutenberg", result["editor_format"])
                self.assertNotIn("CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS", result["matched_rule_ids"])

    def test_empty_space_entities_outside_blocks_are_not_mixed(self):
        for entity in ("&nbsp;", "&#160;", "&#xA0;"):
            with self.subTest(entity=entity):
                content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph --><p>{entity}</p>"
                result = analyze_content(content, self.config)
                self.assertEqual("gutenberg", result["editor_format"])

    def test_br_outside_blocks_is_not_mixed(self):
        for remainder in ("<br>", "<br/>", "<p><br></p>"):
            with self.subTest(remainder=remainder):
                content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->{remainder}"
                result = analyze_content(content, self.config)
                self.assertEqual("gutenberg", result["editor_format"])

    def test_self_closing_non_void_wrappers_outside_blocks_are_mixed(self):
        for remainder in ("<p/>", "<div/>", "<span/>", "<section/>"):
            with self.subTest(remainder=remainder):
                content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->{remainder}"
                result = analyze_content(content, self.config)
                self.assertEqual("mixed", result["editor_format"])
                self.assertIn("CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS", result["matched_rule_ids"])

    def test_self_closing_unknown_tag_outside_blocks_is_mixed(self):
        content = "<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph --><custom-widget/>"
        result = analyze_content(content, self.config)
        self.assertEqual("mixed", result["editor_format"])
        self.assertIn("CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS", result["matched_rule_ids"])

    def test_paired_empty_wrappers_remain_non_substantial(self):
        for remainder in ("<p></p>", "<div><span></span></div>", "<p><br></p>"):
            with self.subTest(remainder=remainder):
                content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->{remainder}"
                result = analyze_content(content, self.config)
                self.assertEqual("gutenberg", result["editor_format"])
                self.assertNotIn("CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS", result["matched_rule_ids"])

    def test_nested_empty_wrappers_outside_blocks_are_not_mixed(self):
        remainder = "<div><p><span>&nbsp;</span><br></p></div>"
        content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->{remainder}"
        result = analyze_content(content, self.config)
        self.assertEqual("gutenberg", result["editor_format"])

    def test_zero_width_characters_outside_blocks_are_not_mixed(self):
        remainder = "<section><span>\u200b\u200c\u200d\u2060\ufeff</span></section>"
        content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->{remainder}"
        result = analyze_content(content, self.config)
        self.assertEqual("gutenberg", result["editor_format"])

    def test_short_visible_text_outside_blocks_is_mixed(self):
        for text in ("文", "A", "1", "!", "这是补充说明。"):
            with self.subTest(text=text):
                content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph --><p>{text}</p>"
                result = analyze_content(content, self.config)
                self.assertEqual("mixed", result["editor_format"])
                self.assertIn("CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS", result["matched_rule_ids"])

    def test_functional_elements_outside_blocks_are_mixed(self):
        for element in (
            '<img src="example.png">', "<iframe></iframe>", "<hr>",
            '<a href="https://example.com"></a>',
        ):
            with self.subTest(element=element):
                content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->{element}"
                result = analyze_content(content, self.config)
                self.assertEqual("mixed", result["editor_format"])

    def test_unknown_or_damaged_html_outside_blocks_is_mixed(self):
        for remainder in ("<custom-widget></custom-widget>", "<div><span></div>"):
            with self.subTest(remainder=remainder):
                content = f"<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->{remainder}"
                result = analyze_content(content, self.config)
                self.assertEqual("mixed", result["editor_format"])

    def test_large_classic_content_outside_blocks_remains_mixed(self):
        remainder = "<p>人工经典正文。</p>" * 100
        content = f"{remainder}<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->"
        result = analyze_content(content, self.config)
        self.assertEqual("mixed", result["editor_format"])

    def test_unclosed_sourcecode_requires_manual_review(self):
        result = analyze_content("[sourcecode]echo 1;", self.config)
        self.assertEqual("unknown", result["code_format"])
        self.assertEqual("unknown", result["primary_format"])
        self.assertEqual("manual-review", result["risk_level"])
        self.assertTrue({"SC_UNCLOSED", "SH_DAMAGED"} <= set(result["matched_rule_ids"]))

    def test_orphan_sourcecode_close_requires_manual_review(self):
        result = analyze_content("<p>遗留内容</p>[/sourcecode]", self.config)
        self.assertEqual("unknown", result["code_format"])
        self.assertEqual("manual-review", result["risk_level"])
        self.assertTrue({"SC_ORPHAN_CLOSE", "SH_DAMAGED"} <= set(result["matched_rule_ids"]))

    def test_escaped_sourcecode_is_plain_text(self):
        result = analyze_content("[[sourcecode]]echo 1;[[/sourcecode]]", self.config)
        shortcode_rules = {
            "SC_KNOWN", "SH_SHORTCODE", "SC_UNCLOSED", "SC_ORPHAN_CLOSE", "SH_DAMAGED",
        }
        self.assertFalse(shortcode_rules & set(result["matched_rule_ids"]))
        self.assertEqual("none", result["code_format"])

    def test_mismatched_shortcode_nesting_is_damaged(self):
        result = analyze_content("[sourcecode][code]x[/sourcecode][/code]", self.config)
        self.assertEqual("unknown", result["code_format"])
        self.assertEqual("manual-review", result["risk_level"])
        self.assertTrue({"SC_UNCLOSED", "SC_ORPHAN_CLOSE", "SH_DAMAGED"} <= set(result["matched_rule_ids"]))

    def test_paired_sourcecode_does_not_regress(self):
        result = analyze_content("[sourcecode]echo 1;[/sourcecode]", self.config)
        self.assertEqual("syntaxhighlighter", result["code_format"])
        self.assertEqual("classic+syntaxhighlighter", result["primary_format"])
        self.assertTrue({"SC_KNOWN", "SH_SHORTCODE"} <= set(result["matched_rule_ids"]))
        self.assertNotIn("SH_DAMAGED", result["matched_rule_ids"])

    def test_unknown_single_tag_is_not_unclosed_structure(self):
        result = analyze_content('[unknown-widget id="123"]', self.config)
        self.assertIn("SC_UNKNOWN", result["matched_rule_ids"])
        self.assertNotIn("SC_UNCLOSED", result["matched_rule_ids"])
        self.assertNotIn("RISK_DAMAGED_STRUCTURE", result["risk_reasons"])
        self.assertEqual("high", result["risk_level"])

    def test_unknown_paired_appearance_is_not_structural_damage(self):
        result = analyze_content("[unknown-widget]人工内容[/unknown-widget]", self.config)
        self.assertIn("SC_UNKNOWN", result["matched_rule_ids"])
        self.assertNotIn("SC_UNCLOSED", result["matched_rule_ids"])
        self.assertNotIn("SC_ORPHAN_CLOSE", result["matched_rule_ids"])
        self.assertNotIn("RISK_DAMAGED_STRUCTURE", result["risk_reasons"])
        self.assertEqual("high", result["risk_level"])

    def test_paired_caption_is_known_and_low_risk(self):
        result = analyze_content('[caption id="attachment_1"]人工图片说明[/caption]', self.config)
        self.assertIn("SC_KNOWN", result["matched_rule_ids"])
        self.assertFalse({"SC_UNKNOWN", "SC_UNCLOSED", "SC_ORPHAN_CLOSE"} & set(result["matched_rule_ids"]))
        self.assertEqual("none", result["code_format"])
        self.assertEqual("classic/plain", result["primary_format"])
        self.assertEqual("low", result["risk_level"])

    def test_unclosed_caption_is_manual_review(self):
        result = analyze_content('[caption id="attachment_1"]人工图片说明', self.config)
        self.assertTrue({"SC_KNOWN", "SC_UNCLOSED"} <= set(result["matched_rule_ids"]))
        self.assertIn("RISK_DAMAGED_STRUCTURE", result["risk_reasons"])
        self.assertEqual("manual-review", result["risk_level"])

    def test_orphan_caption_close_is_damaged(self):
        result = analyze_content("[/caption]", self.config)
        self.assertIn("SC_ORPHAN_CLOSE", result["matched_rule_ids"])
        self.assertIn("RISK_DAMAGED_STRUCTURE", result["risk_reasons"])
        self.assertEqual("manual-review", result["risk_level"])

    def test_bare_unknown_bracket_words_are_plain_text(self):
        result = analyze_content("[length] [method]", self.config)
        self.assertFalse({"SC_UNKNOWN", "SC_UNCLOSED", "SC_ORPHAN_CLOSE"} & set(result["matched_rule_ids"]))
        self.assertEqual([], result["risk_reasons"])
        self.assertEqual("low", result["risk_level"])

    def test_unknown_shortcode_inside_syntaxhighlighter_block_is_ignored(self):
        content = (
            '<!-- wp:syntaxhighlighter/code --><pre>[sourcecode]'
            '[unknown-widget id="123"][/sourcecode]</pre>'
            '<!-- /wp:syntaxhighlighter/code -->'
        )
        result = analyze_content(content, self.config)
        self.assertNotIn("SC_UNKNOWN", result["matched_rule_ids"])
        self.assertNotIn("RISK_UNKNOWN_SHORTCODE", result["risk_reasons"])
        self.assertEqual("syntaxhighlighter", result["code_format"])

    def test_unknown_shortcode_inside_pre_is_ignored(self):
        result = analyze_content('<pre>[unknown-widget id="123"]</pre>', self.config)
        self.assertNotIn("SC_UNKNOWN", result["matched_rule_ids"])

    def test_unknown_shortcode_inside_code_is_ignored(self):
        result = analyze_content('<code>[unknown-widget id="123"]</code>', self.config)
        self.assertNotIn("SC_UNKNOWN", result["matched_rule_ids"])

    def test_unknown_shortcode_inside_code_block_pro_is_ignored(self):
        content = (
            '<!-- wp:kevinbatdorf/code-block-pro -->'
            '<div class="wp-block-kevinbatdorf-code-block-pro"><pre class="shiki"><code>'
            '<span class="line">[unknown-widget id="123"]</span>'
            '</code></pre></div><!-- /wp:kevinbatdorf/code-block-pro -->'
        )
        result = analyze_content(content, self.config)
        self.assertNotIn("SC_UNKNOWN", result["matched_rule_ids"])
        self.assertEqual("code-block-pro", result["code_format"])

    def test_unknown_shortcode_in_ordinary_content_is_still_detected(self):
        result = analyze_content('<p>[unknown-widget id="123"]</p>', self.config)
        self.assertIn("SC_UNKNOWN", result["matched_rule_ids"])
        self.assertIn("RISK_UNKNOWN_SHORTCODE", result["risk_reasons"])
        self.assertEqual("high", result["risk_level"])

    def test_unknown_shortcode_inside_sourcecode_is_ignored(self):
        content = '[sourcecode language="php"]\n[unknown-widget id="123"]\n[/sourcecode]'
        result = analyze_content(content, self.config)
        self.assertIn("SH_SHORTCODE", result["matched_rule_ids"])
        self.assertNotIn("SC_UNKNOWN", result["matched_rule_ids"])
        self.assertFalse({"SC_UNCLOSED", "SC_ORPHAN_CLOSE", "SH_DAMAGED"} & set(result["matched_rule_ids"]))

    def test_unclosed_sourcecode_still_detects_damage_with_protected_content(self):
        content = '[sourcecode language="php"]\n[unknown-widget id="123"]'
        result = analyze_content(content, self.config)
        self.assertTrue({"SC_UNCLOSED", "SH_DAMAGED"} <= set(result["matched_rule_ids"]))
        self.assertNotIn("SC_UNKNOWN", result["matched_rule_ids"])
        self.assertIn("RISK_DAMAGED_STRUCTURE", result["risk_reasons"])
        self.assertEqual("manual-review", result["risk_level"])

    def test_unknown_shortcode_outside_code_region_is_not_hidden(self):
        content = '<pre>[unknown-inside id="1"]</pre><p>[unknown-outside id="2"]</p>'
        result = analyze_content(content, self.config)
        self.assertIn("SC_UNKNOWN", result["matched_rule_ids"])
        self.assertEqual(1, result["rule_counts"]["SC_UNKNOWN"])

    def test_valid_block_with_malformed_open_is_damaged(self):
        content = "<!-- wp:paragraph --><p>正常</p><!-- /wp:paragraph -->\n<!-- wp:code"
        result = analyze_content(content, self.config)
        self.assertEqual("unknown", result["editor_format"])
        self.assertIn("GB_BLOCK_DAMAGED", result["matched_rule_ids"])
        self.assertNotIn("GB_BLOCK_BALANCED", result["matched_rule_ids"])
        self.assertFalse(result["blocks"]["balanced"])

    def test_valid_block_with_malformed_close_is_damaged(self):
        content = "<!-- wp:paragraph --><p>正常</p><!-- /wp:paragraph -->\n<!-- /wp:code"
        result = analyze_content(content, self.config)
        self.assertEqual("unknown", result["editor_format"])
        self.assertIn("GB_BLOCK_DAMAGED", result["matched_rule_ids"])
        self.assertNotIn("GB_BLOCK_BALANCED", result["matched_rule_ids"])

    def test_orphan_gutenberg_close_is_damaged(self):
        result = analyze_content("<!-- /wp:paragraph -->", self.config)
        self.assertEqual("unknown", result["editor_format"])
        self.assertIn("GB_BLOCK_DAMAGED", result["matched_rule_ids"])
        self.assertNotIn("GB_BLOCK_BALANCED", result["matched_rule_ids"])

    def test_mismatched_gutenberg_names_are_damaged(self):
        content = "<!-- wp:paragraph --><p>内容</p><!-- /wp:code -->"
        result = analyze_content(content, self.config)
        self.assertEqual("unknown", result["editor_format"])
        self.assertIn("GB_BLOCK_DAMAGED", result["matched_rule_ids"])
        self.assertNotIn("GB_BLOCK_BALANCED", result["matched_rule_ids"])

    def test_self_closing_gutenberg_block_is_balanced(self):
        result = analyze_content("<!-- wp:separator /-->", self.config)
        self.assertEqual("gutenberg", result["editor_format"])
        self.assertIn("GB_BLOCK_BALANCED", result["matched_rule_ids"])
        self.assertNotIn("GB_BLOCK_DAMAGED", result["matched_rule_ids"])
        self.assertTrue(result["blocks"]["balanced"])

    def test_ordinary_html_comment_is_not_gutenberg_damage(self):
        result = analyze_content("<!-- 普通说明 -->", self.config)
        self.assertEqual("classic", result["editor_format"])
        self.assertNotIn("GB_BLOCK_DAMAGED", result["matched_rule_ids"])

    def test_html_entity_gutenberg_example_is_not_a_block(self):
        result = analyze_content("&lt;!-- wp:paragraph --&gt;", self.config)
        self.assertEqual("classic", result["editor_format"])
        self.assertNotIn("GB_BLOCK_COMMENT", result["matched_rule_ids"])
        self.assertNotIn("GB_BLOCK_DAMAGED", result["matched_rule_ids"])

    def test_damaged_gutenberg_never_reports_balanced(self):
        damaged_contents = [
            "<!-- wp:paragraph --><p>正常</p><!-- /wp:paragraph --><!-- wp:code",
            "<!-- wp:paragraph --><p>正常</p><!-- /wp:paragraph --><!-- /wp:code",
            "<!-- /wp:paragraph -->",
            "<!-- wp:paragraph --><!-- /wp:code -->",
        ]
        for content in damaged_contents:
            with self.subTest(content=content):
                result = analyze_content(content, self.config)
                self.assertIn("GB_BLOCK_DAMAGED", result["matched_rule_ids"])
                self.assertNotIn("GB_BLOCK_BALANCED", result["matched_rule_ids"])
                self.assertFalse(result["blocks"]["balanced"])


if __name__ == "__main__":
    unittest.main()
