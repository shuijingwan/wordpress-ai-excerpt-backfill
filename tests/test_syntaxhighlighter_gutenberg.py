import json
from pathlib import Path
import unittest

from src.analyzer import analyze_content


ROOT = Path(__file__).resolve().parents[1]


class SyntaxHighlighterGutenbergTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "config/classification.json").read_text(encoding="utf-8"))

    def analyze(self, content):
        return analyze_content(content, self.config)

    def test_language_attribute_is_counted(self):
        result = self.analyze(
            '<!-- wp:syntaxhighlighter/code {"language":"php"} -->'
            '<pre class="wp-block-syntaxhighlighter-code">x</pre>'
            '<!-- /wp:syntaxhighlighter/code -->'
        )
        self.assertEqual("syntaxhighlighter", result["code_format"])
        self.assertEqual(1, result["syntaxhighlighter_count"])
        self.assertEqual(["php"], result["syntaxhighlighter_languages"])
        self.assertTrue(result["syntaxhighlighter_balanced"])
        self.assertTrue(result["syntaxhighlighter_attributes_valid"])

    def test_no_attributes_is_valid(self):
        result = self.analyze(
            '<!-- wp:syntaxhighlighter/code --><pre>x</pre>'
            '<!-- /wp:syntaxhighlighter/code -->'
        )
        self.assertEqual(1, result["syntaxhighlighter_count"])
        self.assertEqual([], result["syntaxhighlighter_languages"])
        self.assertTrue(result["syntaxhighlighter_balanced"])

    def test_multiple_blocks_and_languages(self):
        content = "".join(
            '<!-- wp:syntaxhighlighter/code {"language":"%s"} --><pre>x</pre>'
            '<!-- /wp:syntaxhighlighter/code -->' % language
            for language in ("php", "bash", "php")
        )
        result = self.analyze(content)
        self.assertEqual(3, result["syntaxhighlighter_count"])
        self.assertEqual(["bash", "php"], result["syntaxhighlighter_languages"])

    def test_unbalanced_block_is_damaged(self):
        result = self.analyze('<!-- wp:syntaxhighlighter/code --><pre>x</pre>')
        self.assertEqual(0, result["syntaxhighlighter_count"])
        self.assertFalse(result["syntaxhighlighter_balanced"])
        self.assertIn("SH_DAMAGED", result["matched_rule_ids"])
        self.assertEqual("manual-review", result["risk_level"])

    def test_invalid_attribute_json_is_explicit(self):
        result = self.analyze(
            '<!-- wp:syntaxhighlighter/code {"language":php} --><pre>x</pre>'
            '<!-- /wp:syntaxhighlighter/code -->'
        )
        self.assertEqual(1, result["syntaxhighlighter_count"])
        self.assertFalse(result["syntaxhighlighter_attributes_valid"])
        self.assertIn("SH_ATTRIBUTES_INVALID", result["matched_rule_ids"])
        self.assertEqual("manual-review", result["risk_level"])

    def test_real_code_block_pro_mix(self):
        content = (
            '<!-- wp:syntaxhighlighter/code --><pre>x</pre><!-- /wp:syntaxhighlighter/code -->'
            '<!-- wp:kevinbatdorf/code-block-pro -->'
            '<div class="wp-block-kevinbatdorf-code-block-pro"><textarea>y</textarea>'
            '<pre class="shiki"><code><span class="line">y</span></code></pre></div>'
            '<!-- /wp:kevinbatdorf/code-block-pro -->'
        )
        result = self.analyze(content)
        self.assertEqual("mixed", result["code_format"])
        self.assertTrue(result["mixed_code_formats"])
        self.assertEqual(1, result["syntaxhighlighter_count"])
        self.assertEqual(1, result["code_block_pro_count"])

    def test_code_block_pro_textarea_example_is_not_counted(self):
        content = (
            '<!-- wp:kevinbatdorf/code-block-pro -->'
            '<div class="wp-block-kevinbatdorf-code-block-pro"><textarea>'
            '<!-- wp:syntaxhighlighter/code {"language":"php"} -->x'
            '<!-- /wp:syntaxhighlighter/code -->'
            '</textarea><pre class="shiki"><code><span class="line">x</span></code></pre></div>'
            '<!-- /wp:kevinbatdorf/code-block-pro -->'
        )
        result = self.analyze(content)
        self.assertEqual(0, result["syntaxhighlighter_count"])
        self.assertEqual("code-block-pro", result["code_format"])
        self.assertEqual("gutenberg", result["editor_format"])
        self.assertFalse({"GB_BLOCK_DAMAGED", "SH_DAMAGED"} & set(result["matched_rule_ids"]))

    def test_escaped_html_example_is_not_counted(self):
        result = self.analyze(
            '<!-- wp:paragraph --><p>&lt;!-- wp:syntaxhighlighter/code '
            '{&quot;language&quot;:&quot;php&quot;} --&gt;</p><!-- /wp:paragraph -->'
        )
        self.assertEqual(0, result["syntaxhighlighter_count"])

    def test_plain_block_name_text_is_not_counted(self):
        result = self.analyze(
            '<!-- wp:paragraph --><p>名称是 wp:syntaxhighlighter/code</p><!-- /wp:paragraph -->'
        )
        self.assertEqual(0, result["syntaxhighlighter_count"])

    def test_sourcecode_support_remains(self):
        result = self.analyze('[sourcecode language="php"]echo 1;[/sourcecode]')
        self.assertEqual("syntaxhighlighter", result["code_format"])
        self.assertIn("SH_SHORTCODE", result["matched_rule_ids"])


if __name__ == "__main__":
    unittest.main()
