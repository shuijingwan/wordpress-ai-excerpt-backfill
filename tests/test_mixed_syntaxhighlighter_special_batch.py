import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/build-mixed-syntaxhighlighter-special-batch.py"
SPEC = importlib.util.spec_from_file_location("mixed_special_batch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

RAW = [
    ROOT / "data/raw/wordpress-zh-posts-20260721T064740Z-829274.jsonl",
    ROOT / "data/raw/wordpress-zh-posts-20260721T065005Z-829489.jsonl",
    ROOT / "data/raw/wordpress-zh-posts-20260721T065041Z-829565.jsonl",
    ROOT / "data/raw/wordpress-zh-posts-20260721T065625Z-829916.jsonl",
]
TRANSLATIONS = ROOT / "data/raw/wordpress-zh-translation-links-20260721.jsonl"


class _History:
    @staticmethod
    def discover_batches(root, errors):
        return []

    @staticmethod
    def validate_batch_index(batches, conflicts, errors):
        return {}


class MixedSyntaxHighlighterSpecialBatchTest(unittest.TestCase):
    def build(self, directory):
        output = Path(directory) / MODULE.OUTPUT_NAME
        with mock.patch.object(MODULE, "_history_module", return_value=_History):
            return MODULE.build_batch(
                RAW, TRANSLATIONS, output, repository_root=Path(directory),
                allocated_at="2026-08-12T00:00:00+00:00"), output

    def test_fixed_special_batch_contains_only_the_five_audited_articles(self):
        with tempfile.TemporaryDirectory() as directory:
            (rows, stats), output = self.build(directory)
            self.assertEqual(MODULE.BATCH_ID, stats["batch_id"])
            self.assertEqual(
                [2710, 4984, 5152, 5520, 12389],
                [row["chinese_post_id"] for row in rows])
            self.assertEqual(5, len(rows))
            self.assertTrue(output.is_file())
            with output.open(encoding="utf-8", newline="") as handle:
                persisted = list(csv.DictReader(handle))
            self.assertEqual(5, len(persisted))
            self.assertEqual("True", persisted[1]["before_shortcode_structure_damaged"])
            self.assertEqual("go", persisted[4]["before_code_block_pro_languages"])
            self.assertTrue(persisted[4]["preserved_code_block_pro_code_sha256"])

    def test_completed_or_already_allocated_article_blocks_the_entire_special_batch(self):
        class AssignedHistory(_History):
            @staticmethod
            def validate_batch_index(batches, conflicts, errors):
                return {2710: {"batch_id": "completed-batch"}}

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(MODULE, "_history_module", return_value=AssignedHistory):
            with self.assertRaisesRegex(MODULE.SpecialBatchError, "already belongs"):
                MODULE.build_batch(
                    RAW, TRANSLATIONS, Path(directory) / MODULE.OUTPUT_NAME,
                    repository_root=Path(directory),
                    allocated_at="2026-08-12T00:00:00+00:00")

    def test_an_extra_raw_article_cannot_become_a_sixth_special_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            extra = Path(directory) / "extra.jsonl"
            extra.write_text(json.dumps({
                "post_id": 999999, "exported_at": "2026-08-12T00:00:00+00:00",
            }) + "\n", encoding="utf-8")
            output = Path(directory) / MODULE.OUTPUT_NAME
            with mock.patch.object(MODULE, "_history_module", return_value=_History):
                rows, _ = MODULE.build_batch(
                    [*RAW, extra], TRANSLATIONS, output,
                    repository_root=Path(directory),
                    allocated_at="2026-08-12T00:00:00+00:00")
            self.assertEqual(5, len(rows))
            self.assertNotIn(999999, [row["chinese_post_id"] for row in rows])


if __name__ == "__main__":
    unittest.main()
