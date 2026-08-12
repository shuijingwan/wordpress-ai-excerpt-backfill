import csv
import json
from pathlib import Path
import tempfile
import unittest

from src.candidate_execution import SafetyError, sha256_text
from src.syntaxhighlighter_batch_validation import (load_batch, validate_batch,
                                                     write_outputs)


CONFIG = json.loads(
    (Path(__file__).resolve().parents[1] / "config/classification.json").read_text(encoding="utf-8")
)


def cbp(language="php", code="echo 1;"):
    attrs = json.dumps({"language": language, "code": code}, separators=(",", ":"))
    return (f"<!-- wp:kevinbatdorf/code-block-pro {attrs} -->"
            "<div class=\"wp-block-kevinbatdorf-code-block-pro\"><pre class=\"shiki\"><code>"
            f"{code}</code></pre></div><!-- /wp:kevinbatdorf/code-block-pro -->")


def sh(code="old"):
    return f"<!-- wp:syntaxhighlighter/code --><pre>{code}</pre><!-- /wp:syntaxhighlighter/code -->"


def core_code(code="old"):
    return ("<!-- wp:code --><pre class=\"wp-block-code\"><code>"
            f"{code}</code></pre><!-- /wp:code -->")


def batch_row(post_id, before_sh=1, expected_cbp=1):
    return {
        "batch_id": "batch-1", "batch_sequence": "1", "chinese_post_id": str(post_id),
        "english_post_id": str(post_id + 1000), "chinese_title": f"标题 {post_id}",
        "before_content_sha256": sha256_text(sh()),
        "before_syntaxhighlighter_count": str(before_sh), "before_code_block_pro_count": "0",
        "expected_code_block_pro_count_after": str(expected_cbp),
    }


class Wp:
    def __init__(self, rows, changes=None):
        self.posts = {}
        changes = changes or {}
        for row in rows:
            zh = int(row["chinese_post_id"]); en = int(row["english_post_id"])
            self.posts[zh] = {"id": zh, "status": "publish", "title": {"raw": row["chinese_title"]},
                              "excerpt": {"raw": ""}, "content": {"raw": cbp()}}
            self.posts[en] = {"id": en, "status": "publish", "title": {"raw": "English"},
                              "excerpt": {"raw": "anything"}, "content": {"raw": "anything"}}
        for post_id, values in changes.items(): self.posts[post_id].update(values)
        self.calls = []
    def get_post(self, post_id): self.calls.append(post_id); return self.posts[post_id]


class Polylang:
    def __init__(self, changes=None): self.changes = changes or {}
    def check(self, zh, en):
        value = {"chinese_post_id": zh, "chinese_language": "zh",
                 "linked_english_post_id": en, "english_post_id": en,
                 "english_language": "en", "linked_chinese_post_id": zh}
        value.update(self.changes.get(zh, {})); return value


class SyntaxHighlighterBatchValidationTest(unittest.TestCase):
    def rows(self, count=20): return [batch_row(value) for value in range(1, count + 1)]

    def validate(self, rows=None, wp=None, polylang=None):
        rows = rows or self.rows()
        return validate_batch(rows, wp or Wp(rows), polylang or Polylang(), CONFIG,
                              validated_at="2026-07-22T00:00:00+00:00")

    def test_twenty_ready_and_language_differences_do_not_block(self):
        rows = self.rows(); source = Wp(rows)
        source.posts[1]["content"]["raw"] = cbp("plaintext")
        results = self.validate(rows, source)
        self.assertEqual(20, len(results)); self.assertTrue(all(r["validation_status"] == "ready" for r in results))
        self.assertEqual("plaintext", results[0]["code_block_pro_languages"])
        self.assertEqual(list(range(1, 21)), source.calls[::2])

    def test_load_rejects_wrong_count_and_duplicate_ids_without_modifying_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.csv"; rows = self.rows(19)
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
            before = path.read_bytes()
            with self.assertRaisesRegex(SafetyError, "exactly 20"): load_batch(path, 20)
            self.assertEqual(before, path.read_bytes())
            rows = self.rows(); rows[-1]["chinese_post_id"] = rows[0]["chinese_post_id"]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
            with self.assertRaisesRegex(SafetyError, "Chinese post IDs"): load_batch(path, 20)
            rows = self.rows(); rows[-1]["english_post_id"] = rows[0]["english_post_id"]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
            with self.assertRaisesRegex(SafetyError, "English post IDs"): load_batch(path, 20)

    def test_load_uses_partial_batch_fixed_count_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.csv"
            rows = self.rows(12)
            for row in rows:
                row["batch_expected_count"] = "12"
            fields = list(rows[0])

            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            self.assertEqual(12, len(load_batch(path, 20)))

            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows[:-1])
            with self.assertRaisesRegex(SafetyError, "exactly 12, got 11"):
                load_batch(path, 20)

            rows.append({
                **rows[-1], "chinese_post_id": "13",
                "english_post_id": "1013",
            })
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            with self.assertRaisesRegex(SafetyError, "exactly 12, got 13"):
                load_batch(path, 20)

    def test_one_or_two_syntaxhighlighters_become_zero_and_expected_cbp_passes(self):
        rows = [batch_row(1, 1, 1), batch_row(2, 2, 2)]
        source = Wp(rows); source.posts[2]["content"]["raw"] = cbp("js") + cbp("css")
        results = self.validate(rows, source)
        self.assertEqual([0, 0], [r["after_syntaxhighlighter_count"] for r in results])
        self.assertEqual([1, 2], [r["after_code_block_pro_count"] for r in results])
        self.assertTrue(all(r["validation_status"] == "ready" for r in results))

    def test_code_block_pro_count_equal_to_or_above_minimum_passes(self):
        rows = [
            batch_row(1, 12, 12),
            batch_row(2, 12, 12),
            batch_row(3, 23, 23),
        ]
        source = Wp(rows)
        source.posts[1]["content"]["raw"] = cbp() * 12
        source.posts[2]["content"]["raw"] = cbp() * 13
        source.posts[3]["content"]["raw"] = cbp() * 25
        results = self.validate(rows, source)
        self.assertEqual([12, 13, 25], [
            row["after_code_block_pro_count"] for row in results])
        self.assertTrue(all(
            row["validation_status"] == "ready" for row in results))

    def test_remaining_syntaxhighlighter_fails_even_when_cbp_meets_minimum(self):
        rows = [batch_row(1, 12, 12)]
        source = Wp(rows)
        source.posts[1]["content"]["raw"] = cbp() * 12 + sh()
        result = self.validate(rows, source)[0]
        self.assertEqual("pending", result["validation_status"])
        self.assertIn("syntaxhighlighter-remains", result["validation_reasons"])
        self.assertNotIn(
            "code-block-pro-count-below-expected", result["validation_reasons"])

    def test_code_block_pro_count_below_minimum_fails(self):
        rows = [batch_row(1, 12, 12)]
        source = Wp(rows)
        source.posts[1]["content"]["raw"] = cbp() * 11
        result = self.validate(rows, source)[0]
        self.assertEqual("abnormal", result["validation_status"])
        self.assertIn(
            "code-block-pro-count-below-expected", result["validation_reasons"])

    def test_residual_syntaxhighlighter_and_unchanged_hash_are_pending(self):
        rows = [batch_row(1), batch_row(2)]
        source = Wp(rows); source.posts[1]["content"]["raw"] = cbp() + sh()
        source.posts[2]["content"]["raw"] = sh()
        results = self.validate(rows, source)
        self.assertEqual(["pending", "abnormal"], [r["validation_status"] for r in results])
        self.assertIn("syntaxhighlighter-remains", results[0]["validation_reasons"])
        self.assertIn("content-hash-unchanged", results[1]["validation_reasons"])
        self.assertIn(
            "code-block-pro-count-below-expected",
            results[1]["validation_reasons"])

    def test_clean_gutenberg_without_code_needs_no_content_change(self):
        content = "<!-- wp:paragraph --><p>正文</p><!-- /wp:paragraph -->"
        row = batch_row(1, before_sh=0, expected_cbp=0)
        row.update({
            "before_content_sha256": sha256_text(content),
            "source_editor_format": "gutenberg",
        })
        source = Wp([row]); source.posts[1]["content"]["raw"] = content
        result = self.validate([row], source)[0]
        self.assertEqual("ready", result["validation_status"])
        self.assertNotIn("content-hash-unchanged", result["validation_reasons"])

    def test_unknown_code_format_fails_even_when_cbp_meets_minimum(self):
        rows = [batch_row(1, 1, 1)]
        source = Wp(rows)
        source.posts[1]["content"]["raw"] = cbp() + core_code()
        result = self.validate(rows, source)[0]
        self.assertEqual("abnormal", result["validation_status"])
        self.assertIn("unexpected-code-format:core-code", result["validation_reasons"])

    def test_count_gutenberg_excerpt_english_and_polylang_failures_are_abnormal(self):
        rows = [batch_row(i) for i in range(1, 6)]; source = Wp(rows)
        source.posts[1]["content"]["raw"] = ""
        source.posts[2]["content"]["raw"] = "<!-- wp:paragraph --><p>x</p>"
        source.posts[3]["excerpt"]["raw"] = "not empty"
        source.posts[1004]["status"] = "draft"
        relations = Polylang({5: {"linked_english_post_id": 9999}})
        results = self.validate(rows, source, relations)
        self.assertTrue(all(r["validation_status"] == "abnormal" for r in results))
        reasons = [r["validation_reasons"] for r in results]
        self.assertIn("code-block-pro-count-below-expected", reasons[0])
        self.assertIn("gutenberg-unbalanced", reasons[1])
        self.assertIn("chinese-excerpt-not-empty", reasons[2])
        self.assertIn("english-not-publish", reasons[3])
        self.assertIn("polylang-relation-abnormal", reasons[4])

    def test_english_content_is_not_compared(self):
        rows = [batch_row(1)]; source = Wp(rows)
        source.posts[1001]["content"]["raw"] = "different and no code block"
        self.assertEqual("ready", self.validate(rows, source)[0]["validation_status"])

    def test_mixed_stage_rejects_classic_content_outside_balanced_blocks(self):
        row = batch_row(1)
        row.update({
            "source_editor_format": "mixed",
            "source_migration_type":
                "mixed-syntaxhighlighter-to-gutenberg-code-block-pro",
        })
        source = Wp([row])
        source.posts[1]["content"]["raw"] = "classic paragraph" + cbp()
        result = self.validate([row], source)[0]
        self.assertEqual("abnormal", result["validation_status"])
        self.assertEqual("mixed", result["after_editor_format"])
        self.assertTrue(result["classic_outside_blocks_after"])
        self.assertIn(
            "editor-format-not-gutenberg", result["validation_reasons"])
        self.assertIn(
            "classic-outside-blocks-remain", result["validation_reasons"])

    def test_mixed_stage_accepts_normalized_gutenberg_content(self):
        row = batch_row(1)
        row.update({
            "source_editor_format": "mixed",
            "source_migration_type":
                "mixed-syntaxhighlighter-to-gutenberg-code-block-pro",
        })
        source = Wp([row])
        source.posts[1]["content"]["raw"] = (
            "<!-- wp:paragraph --><p>normalized</p><!-- /wp:paragraph -->"
            + cbp()
        )
        result = self.validate([row], source)[0]
        self.assertEqual("ready", result["validation_status"])
        self.assertEqual("mixed", result["before_editor_format"])
        self.assertEqual("gutenberg", result["after_editor_format"])
        self.assertFalse(result["classic_outside_blocks_after"])

    def test_preserved_code_block_pro_must_not_be_duplicated_or_lost(self):
        row = batch_row(1, 1, 2)
        row["preserved_code_block_pro_code_sha256"] = sha256_text("keep")
        source = Wp([row])
        source.posts[1]["content"]["raw"] = cbp("go", "keep") + cbp("php", "new")
        self.assertEqual("ready", self.validate([row], source)[0]["validation_status"])
        source.posts[1]["content"]["raw"] = cbp("go", "keep") + cbp("php", "keep")
        result = self.validate([row], source)[0]
        self.assertEqual("abnormal", result["validation_status"])
        self.assertIn("code-block-pro-duplicate-code", result["validation_reasons"])

    def test_old_stage_does_not_require_editor_normalization(self):
        row = batch_row(1)
        source = Wp([row])
        source.posts[1]["content"]["raw"] = "classic paragraph" + cbp()
        result = self.validate([row], source)[0]
        self.assertEqual("ready", result["validation_status"])
        self.assertEqual("mixed", result["after_editor_format"])

    def test_existing_outputs_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "validation.csv"; snapshot = Path(directory) / "snapshot.jsonl"
            output.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(SafetyError, "refusing to overwrite"):
                write_outputs(self.validate(), output, snapshot)
            self.assertEqual("keep", output.read_text(encoding="utf-8")); self.assertFalse(snapshot.exists())

    def test_output_and_snapshot_paths_must_differ(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "same"
            with self.assertRaisesRegex(SafetyError, "must be different paths"):
                write_outputs(self.validate(), path, path)
            self.assertFalse(path.exists())


if __name__ == "__main__": unittest.main()
