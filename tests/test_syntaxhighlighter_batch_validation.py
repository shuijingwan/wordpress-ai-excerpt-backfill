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

    def test_one_or_two_syntaxhighlighters_become_zero_and_expected_cbp_passes(self):
        rows = [batch_row(1, 1, 1), batch_row(2, 2, 2)]
        source = Wp(rows); source.posts[2]["content"]["raw"] = cbp("js") + cbp("css")
        results = self.validate(rows, source)
        self.assertEqual([0, 0], [r["after_syntaxhighlighter_count"] for r in results])
        self.assertEqual([1, 2], [r["after_code_block_pro_count"] for r in results])
        self.assertTrue(all(r["validation_status"] == "ready" for r in results))

    def test_residual_syntaxhighlighter_and_unchanged_hash_are_pending(self):
        rows = [batch_row(1), batch_row(2)]
        source = Wp(rows); source.posts[1]["content"]["raw"] = cbp() + sh()
        source.posts[2]["content"]["raw"] = sh()
        results = self.validate(rows, source)
        self.assertEqual(["pending", "abnormal"], [r["validation_status"] for r in results])
        self.assertIn("syntaxhighlighter-remains", results[0]["validation_reasons"])
        self.assertIn("content-hash-unchanged", results[1]["validation_reasons"])

    def test_count_gutenberg_excerpt_english_and_polylang_failures_are_abnormal(self):
        rows = [batch_row(i) for i in range(1, 6)]; source = Wp(rows)
        source.posts[1]["content"]["raw"] = cbp() + cbp()
        source.posts[2]["content"]["raw"] = "<!-- wp:paragraph --><p>x</p>"
        source.posts[3]["excerpt"]["raw"] = "not empty"
        source.posts[1004]["status"] = "draft"
        relations = Polylang({5: {"linked_english_post_id": 9999}})
        results = self.validate(rows, source, relations)
        self.assertTrue(all(r["validation_status"] == "abnormal" for r in results))
        reasons = [r["validation_reasons"] for r in results]
        self.assertIn("code-block-pro-count-mismatch", reasons[0])
        self.assertIn("gutenberg-unbalanced", reasons[1])
        self.assertIn("chinese-excerpt-not-empty", reasons[2])
        self.assertIn("english-not-publish", reasons[3])
        self.assertIn("polylang-relation-abnormal", reasons[4])

    def test_english_content_is_not_compared(self):
        rows = [batch_row(1)]; source = Wp(rows)
        source.posts[1001]["content"]["raw"] = "different and no code block"
        self.assertEqual("ready", self.validate(rows, source)[0]["validation_status"])

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
