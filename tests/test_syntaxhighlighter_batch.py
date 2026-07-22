import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/build-syntaxhighlighter-batch.py"
SPEC = importlib.util.spec_from_file_location("build_syntaxhighlighter_batch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


FIELDS = [
    "chinese_post_id", "english_post_id", "chinese_title", "published_at", "permalink",
    "chinese_excerpt_empty", "english_status", "syntaxhighlighter_count",
    "syntaxhighlighter_languages", "syntaxhighlighter_balanced", "code_block_pro_count",
    "mixed_code_formats", "content_sha256", "old_phase1_manifest_member",
    "preview_status", "preview_reasons",
]


def row(post_id, **changes):
    value = {
        "chinese_post_id": post_id, "english_post_id": post_id + 1000,
        "chinese_title": f"标题 {post_id}", "published_at": f"2026-01-{post_id % 28 + 1:02d} 00:00:00",
        "permalink": f"https://example.invalid/{post_id}/", "chinese_excerpt_empty": "True",
        "english_status": "publish", "syntaxhighlighter_count": "1",
        "syntaxhighlighter_languages": "php", "syntaxhighlighter_balanced": "True",
        "code_block_pro_count": "0", "mixed_code_formats": "False",
        "content_sha256": f"{post_id:064x}", "old_phase1_manifest_member": "False",
        "preview_status": "ready", "preview_reasons": "",
    }
    value.update(changes)
    return value


class SyntaxHighlighterBatchTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.preview = self.root / "preview.csv"
        self.pilot = self.root / "pilot.csv"
        self.old = self.root / "old.csv"
        self.output = self.root / "syntaxhighlighter-migration-batch-test.csv"

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, rows, fields=None):
        fields = fields or list(rows[0])
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)

    def id_file(self, path, ids):
        self.write(path, [{"chinese_post_id": value} for value in ids], ["chinese_post_id"])

    def build(self, rows, expected=20):
        self.write(self.preview, rows, FIELDS)
        if not self.pilot.exists(): self.id_file(self.pilot, [])
        if not self.old.exists(): self.id_file(self.old, [])
        return MODULE.build_batch(
            self.preview, self.output, expected, "test-batch", self.pilot, self.old,
            allocated_at="2026-07-22T00:00:00+00:00",
        )

    def test_selects_twenty_deterministically_and_calculates_expected_count(self):
        rows = [row(value, syntaxhighlighter_count=str(1 + value % 3)) for value in range(1, 26)]
        first, stats = self.build(rows)
        expected_ids = [int(item["chinese_post_id"]) for item in sorted(
            rows, key=lambda item: (
                int(item["syntaxhighlighter_count"]),
                -int(item["published_at"].replace("-", "").replace(":", "").replace(" ", "")),
                -int(item["chinese_post_id"]),
            ),
        )[:20]]
        self.assertEqual(expected_ids, [item["chinese_post_id"] for item in first])
        self.assertEqual(20, len(first)); self.assertEqual(5, stats["remaining_unallocated_ready"])
        self.assertTrue(all(item["batch_sequence"] == 1 for item in first))
        self.assertTrue(all(
            item["expected_code_block_pro_count_after"] == item["before_syntaxhighlighter_count"]
            for item in first
        ))

    def test_pilot_old_and_existing_batch_ids_are_excluded(self):
        rows = [row(value) for value in range(1, 25)]
        self.id_file(self.pilot, [1]); self.id_file(self.old, [2])
        existing = self.root / "syntaxhighlighter-migration-batch-existing.csv"
        self.write(existing, [{"chinese_post_id": 3, "batch_sequence": 1}])
        selected, stats = self.build(rows)
        ids = {item["chinese_post_id"] for item in selected}
        self.assertFalse({1, 2, 3} & ids)
        self.assertEqual([1], stats["excluded_pilot_ids"])
        self.assertEqual(1, stats["excluded_old_count"])
        self.assertEqual(1, stats["excluded_existing_batch_count"])
        self.assertTrue(all(item["batch_sequence"] == 2 for item in selected))

    def test_ineligible_rows_never_fill_expected_count(self):
        invalid = [
            row(101, preview_status="mixed"), row(102, preview_status="abnormal"),
            row(103, chinese_excerpt_empty="False"), row(104, english_status="draft"),
            row(105, code_block_pro_count="1"), row(106, syntaxhighlighter_balanced="False"),
            row(107, mixed_code_formats="True"), row(108, syntaxhighlighter_count="0"),
            row(109, old_phase1_manifest_member="True"),
        ]
        with self.assertRaisesRegex(MODULE.BatchError, "expected 20, found 19"):
            self.build([row(value) for value in range(1, 20)] + invalid)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.BatchError, "refusing to overwrite"):
            self.build([row(value) for value in range(1, 21)])
        self.assertEqual("keep", self.output.read_text(encoding="utf-8"))

    def test_expected_count_is_contract_not_eligibility_relaxation(self):
        with self.assertRaisesRegex(MODULE.BatchError, "expected 20, found 1"):
            self.build([row(1)] + [row(value, preview_status="mixed") for value in range(2, 30)])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
