import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/build-full-inventory.py"
SPEC = importlib.util.spec_from_file_location("build_full_inventory", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def raw_record(post_id, content, excerpt="", title="人工标题"):
    return {
        "schema_version": 1,
        "post_id": post_id,
        "post_type": "post",
        "post_status": "publish",
        "title": title,
        "published_at": "2020-01-01 00:00:00",
        "modified_at": "2020-01-02 00:00:00",
        "language_source": "polylang",
        "language": "zh",
        "excerpt": excerpt,
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def translation_record(post_id, english_id=None, status=None):
    return {
        "schema_version": 1,
        "post_id": post_id,
        "has_english_translation": english_id is not None,
        "english_post_id": english_id,
        "english_post_status": status,
    }


class FullInventoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.raw = self.root / "raw.jsonl"
        self.translations = self.root / "translations.jsonl"
        self.prefix = self.root / "inventory"

    def tearDown(self):
        self.temporary.cleanup()

    def write_jsonl(self, path, records):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    def test_builds_csv_json_and_markdown_without_content(self):
        content = "<p>经典内容</p>"
        self.write_jsonl(self.raw, [raw_record(1, content)])
        self.write_jsonl(self.translations, [translation_record(1)])
        summary, paths = MODULE.build_inventory([self.raw], self.translations, self.prefix)
        self.assertEqual(1, summary["total_posts"])
        self.assertEqual(1, summary["categories"]["non-gutenberg"])
        for path in paths:
            self.assertTrue(path.is_file())
        csv_text = paths[0].read_text(encoding="utf-8")
        self.assertNotIn(content, csv_text)
        self.assertNotIn("content_sha256", csv_text)

    def test_classifies_complete_code_block_pro_and_candidate(self):
        content = (
            '<!-- wp:kevinbatdorf/code-block-pro -->'
            '<div class="wp-block-kevinbatdorf-code-block-pro"><textarea>x</textarea>'
            '<pre class="shiki"><code><span class="line">x</span></code></pre></div>'
            '<!-- /wp:kevinbatdorf/code-block-pro -->'
        )
        self.write_jsonl(self.raw, [raw_record(2, content)])
        self.write_jsonl(self.translations, [translation_record(2, 20, "publish")])
        summary, paths = MODULE.build_inventory([self.raw], self.translations, self.prefix)
        self.assertEqual(1, summary["categories"]["gutenberg-code-block-pro"])
        self.assertEqual(1, summary["phase1_eligible"])
        self.assertEqual(1, summary["translation_replacement_candidates"])
        self.assertEqual(1, summary["excerpt_empty_gutenberg_code_block_pro"])
        with paths[0].open(encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual("1", row["code_block_pro_count"])
        self.assertEqual("True", row["excerpt_empty"])

    def test_reports_gutenberg_without_cbp_and_damaged_blocks(self):
        records = [
            raw_record(3, "<!-- wp:paragraph --><p>A</p><!-- /wp:paragraph -->"),
            raw_record(4, "<!-- wp:paragraph --><p>A</p>"),
            raw_record(8, "<!-- wp:legacy/widget /-->"),
        ]
        self.write_jsonl(self.raw, records)
        self.write_jsonl(
            self.translations,
            [translation_record(3), translation_record(4), translation_record(8)],
        )
        summary, paths = MODULE.build_inventory([self.raw], self.translations, self.prefix)
        self.assertEqual(2, summary["categories"]["gutenberg-without-code-block-pro"])
        self.assertEqual(1, summary["categories"]["mixed-or-anomalous"])
        with paths[0].open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual("True", rows[1]["has_damaged_blocks"])
        self.assertEqual("True", rows[1]["has_unparseable_blocks"])
        self.assertEqual("False", rows[2]["has_damaged_blocks"])
        self.assertEqual("True", rows[2]["has_unparseable_blocks"])

    def test_detects_shortcode_and_core_code(self):
        content = (
            "<!-- wp:shortcode -->[caption]x[/caption]<!-- /wp:shortcode -->"
            "<!-- wp:code --><pre><code>x</code></pre><!-- /wp:code -->"
        )
        self.write_jsonl(self.raw, [raw_record(5, content)])
        self.write_jsonl(self.translations, [translation_record(5)])
        _, paths = MODULE.build_inventory([self.raw], self.translations, self.prefix)
        with paths[0].open(encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual("True", row["has_shortcode"])
        self.assertEqual("True", row["has_core_code"])

    def test_duplicate_post_id_fails_without_formal_outputs(self):
        record = raw_record(6, "<p>A</p>")
        self.write_jsonl(self.raw, [record, record])
        self.write_jsonl(self.translations, [translation_record(6)])
        with self.assertRaises(MODULE.InventoryError):
            MODULE.build_inventory([self.raw], self.translations, self.prefix)
        self.assertFalse(Path(str(self.prefix) + ".csv").exists())
        self.assertFalse(Path(str(self.prefix) + ".summary.json").exists())
        self.assertFalse(Path(str(self.prefix) + ".md").exists())

    def test_missing_or_extra_translation_relation_fails(self):
        self.write_jsonl(self.raw, [raw_record(7, "<p>A</p>")])
        for records in ([], [translation_record(7), translation_record(8)]):
            with self.subTest(records=records):
                self.write_jsonl(self.translations, records)
                with self.assertRaises(MODULE.InventoryError):
                    MODULE.build_inventory([self.raw], self.translations, self.prefix)


if __name__ == "__main__":
    unittest.main()
