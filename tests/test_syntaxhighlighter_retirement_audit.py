import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/build-syntaxhighlighter-retirement-audit.py"
SPEC = importlib.util.spec_from_file_location("retirement_audit", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def record(post_id, content, **overrides):
    value = {
        "schema_version": 1, "post_id": post_id, "language_source": "polylang",
        "language": "zh", "post_type": "post", "post_status": "publish",
        "title": f"标题 {post_id}", "published_at": f"2026-01-{post_id:02d} 10:00:00",
        "permalink": f"https://www.shuijingwanwq.com/{post_id}/", "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    value.update(overrides)
    return value


class RetirementAuditTest(unittest.TestCase):
    def setUp(self):
        self.config = MODULE.load_config(ROOT / "config/syntaxhighlighter-retirement.json")
        self.pattern = MODULE.build_pattern(self.config["shortcodes"])

    def test_production_shortcode_inventory_is_complete_and_excludes_non_shortcodes(self):
        self.assertEqual(65, len(self.config["shortcodes"]))
        self.assertIn("sourcecode", self.config["shortcodes"])
        self.assertIn("c-sharp", self.config["shortcodes"])
        self.assertNotIn("latex", self.config["shortcodes"])
        self.assertNotIn("r", self.config["shortcodes"])

    def test_matches_open_close_attributes_and_every_content_region(self):
        content = (
            '<!-- wp:kevinbatdorf/code-block-pro {"code":"[php]"} -->'
            '<pre><code>[js foo="bar"]x[/js]</code></pre><!-- /wp:kevinbatdorf/code-block-pro -->'
            '<!-- wp:code --><pre>[sourcecode language="php"]x[/sourcecode]</pre><!-- /wp:code -->'
        )
        rows = MODULE.audit([record(1, content)], self.pattern, self.config["shortcodes"])
        self.assertEqual(1, len(rows))
        self.assertEqual("[sourcecode], [js], [php]", rows[0]["matched_shortcodes"])
        self.assertEqual(5, rows[0]["match_count"])

    def test_is_case_sensitive_and_ignores_escaped_or_prefix_names(self):
        content = "[PHP]x[/PHP] [[php]x[/php]] [php-extra]x[/php-extra]"
        self.assertEqual([], MODULE.audit([record(1, content)], self.pattern, self.config["shortcodes"]))

    def test_orphan_closing_tag_is_a_candidate(self):
        rows = MODULE.audit([record(1, "text [/yaml]")], self.pattern, self.config["shortcodes"])
        self.assertEqual("[yaml]", rows[0]["matched_shortcodes"])
        self.assertEqual(1, rows[0]["match_count"])

    def test_scope_hash_duplicate_and_empty_input_are_rejected(self):
        with self.assertRaises(MODULE.AuditError):
            MODULE.validate_record(record(1, "x", language="en"), "x", 1, set())
        with self.assertRaises(MODULE.AuditError):
            MODULE.validate_record(record(1, "x", content_sha256="0" * 64), "x", 1, set())
        seen = set()
        MODULE.validate_record(record(1, "x"), "x", 1, seen)
        with self.assertRaises(MODULE.AuditError):
            MODULE.validate_record(record(1, "x"), "x", 2, seen)

    def test_reports_aggregate_sort_and_write_expected_fields(self):
        records = [record(1, "[php]x[/php] [php]"), record(2, "[text]x[/text]")]
        rows = MODULE.audit(records, self.pattern, self.config["shortcodes"])
        self.assertEqual([2, 1], [row["post_id"] for row in rows])
        with tempfile.TemporaryDirectory() as directory:
            csv_path, txt_path = Path(directory) / "audit.csv", Path(directory) / "audit.txt"
            MODULE.write_reports(rows, csv_path, txt_path)
            with csv_path.open(encoding="utf-8", newline="") as handle:
                persisted = list(csv.DictReader(handle))
            self.assertEqual(list(MODULE.FIELDS), list(persisted[0]))
            self.assertEqual("3", persisted[1]["match_count"])
            text = txt_path.read_text(encoding="utf-8")
            self.assertIn("人工检查候选清单", text)
            self.assertIn("post=2&action=edit", text)


if __name__ == "__main__":
    unittest.main()
