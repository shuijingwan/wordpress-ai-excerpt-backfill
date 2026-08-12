import contextlib
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/build-mixed-syntaxhighlighter-batch.py"
SPEC = importlib.util.spec_from_file_location(
    "build_mixed_syntaxhighlighter_batch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CONFIG = json.loads(
    (ROOT / "config/classification.json").read_text(encoding="utf-8"))


def content(language="php"):
    return (
        "classic paragraph\n"
        f'<!-- wp:syntaxhighlighter/code {{"language":"{language}"}} -->'
        '<pre class="wp-block-syntaxhighlighter-code">echo 1;</pre>'
        '<!-- /wp:syntaxhighlighter/code -->'
    )


def candidate(post_id, published_at=None):
    body = content()
    content_sha256 = hashlib.sha256(body.encode()).hexdigest()
    published = published_at or f"2026-07-{post_id % 28 + 1:02d} 00:00:00"
    preview = {
        "chinese_post_id": str(post_id),
        "english_post_id": str(post_id + 1000),
        "chinese_title": f"标题 {post_id}",
        "published_at": published,
        "permalink": f"https://example.invalid/{post_id}/",
        "chinese_excerpt_empty": "True",
        "english_status": "publish",
        "editor_format": "mixed",
        "syntaxhighlighter_count": "1",
        "syntaxhighlighter_balanced": "True",
        "code_block_pro_count": "0",
        "mixed_code_formats": "False",
        "content_sha256": content_sha256,
        "old_phase1_manifest_member": "False",
        "preview_status": "abnormal",
        "preview_reasons": "editor-format-mixed",
    }
    post = {
        "post_id": post_id,
        "post_type": "post",
        "post_status": "publish",
        "language_source": "polylang",
        "language": "zh",
        "excerpt": "",
        "content": body,
        "title": f"标题 {post_id}",
        "published_at": published,
        "permalink": f"https://example.invalid/{post_id}/",
        "content_sha256": content_sha256,
        "exported_at": "2026-07-21T07:00:08+00:00",
    }
    relation = {
        "post_id": post_id,
        "has_english_translation": True,
        "english_post_id": post_id + 1000,
        "english_post_status": "publish",
    }
    return preview, post, relation


class MixedSyntaxHighlighterBatchTest(unittest.TestCase):
    def select(self, ids, maximum=20, excluded_chinese=(),
               excluded_english=(), published=None):
        triples = [
            candidate(post_id, (published or {}).get(post_id))
            for post_id in ids
        ]
        return MODULE.select_candidates(
            [item[0] for item in triples],
            {item[1]["post_id"]: item[1] for item in triples},
            {item[2]["post_id"]: item[2] for item in triples},
            CONFIG, set(excluded_chinese), set(excluded_english), maximum)

    def test_selects_newest_twenty_with_descending_id_tiebreak(self):
        published = {
            post_id: (
                "2026-07-29 00:00:00" if post_id in {21, 22}
                else f"2026-07-{post_id:02d} 00:00:00"
            )
            for post_id in range(1, 26)
        }
        selected, stats = self.select(
            range(1, 26), published=published)
        ids = [int(item[0]["chinese_post_id"]) for item in selected]
        self.assertEqual([22, 21, 25, 24, 23], ids[:5])
        self.assertEqual(20, len(ids))
        self.assertEqual(25, stats["remaining_eligible_count"])
        self.assertEqual(5, stats["remaining_after_selection"])

    def test_final_partial_batch_selects_every_remaining_candidate(self):
        selected, stats = self.select([1, 2, 3], maximum=20)
        self.assertEqual(3, len(selected))
        self.assertEqual(3, stats["selected_count"])
        self.assertEqual(0, stats["remaining_after_selection"])

    def test_excludes_completed_allocated_and_explicit_abnormal_ids_before_sorting(self):
        selected, stats = self.select(
            [1, 2, 3, 2710, 4984, 5152, 5520, 12389], maximum=20,
            excluded_chinese={3}, excluded_english={1002})
        self.assertEqual(
            [1], [int(item[0]["chinese_post_id"]) for item in selected])
        self.assertEqual(
            [2710, 4984, 5152, 5520, 12389],
            stats["explicit_abnormal_candidates_rejected"])

    def test_business_eligible_candidate_is_not_filtered_by_preview_format(self):
        preview, post, relation = candidate(1)
        preview["preview_reasons"] = (
            "editor-format-mixed;mixed-code-formats:"
            "classic-pre-code|syntaxhighlighter"
        )
        selected, stats = MODULE.select_candidates(
            [preview], {1: post}, {1: relation}, CONFIG, set(), set(), 20)
        self.assertEqual([1], [int(item[0]["chinese_post_id"]) for item in selected])
        self.assertEqual(1, stats["remaining_eligible_count"])

    def write_csv(self, path, rows):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader(); writer.writerows(rows)

    def write_jsonl(self, path, rows):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8")

    def build_fixture(self, directory, ids=(1, 2), preview_changes=None,
                      existing_batches=0):
        root = Path(directory)
        triples = [candidate(post_id) for post_id in ids]
        previews = [dict(value[0]) for value in triples]
        if preview_changes:
            previews[0].update(preview_changes)
        preview = root / "preview.csv"; self.write_csv(preview, previews)
        raw = root / "raw.jsonl"; self.write_jsonl(
            raw, [value[1] for value in triples])
        translations = root / "translations.jsonl"; self.write_jsonl(
            translations, [value[2] for value in triples])
        analysis = root / "data/analysis"; analysis.mkdir(parents=True)
        for sequence in range(1, existing_batches + 1):
            suffix = f"202607{20 + sequence:02d}-01"
            self.write_csv(
                analysis / f"mixed-syntaxhighlighter-migration-batch-{suffix}.csv",
                [{"batch_id": f"mixed-syntaxhighlighter-{suffix}",
                  "batch_sequence": sequence,
                  "source_type": MODULE.SOURCE_TYPE}])
        suffix = f"20260801-{existing_batches + 1:02d}"
        batch_id = f"mixed-syntaxhighlighter-{suffix}"
        output = analysis / f"mixed-syntaxhighlighter-migration-batch-{suffix}.csv"
        return root, preview, raw, translations, output, batch_id

    def build(self, fixture, maximum=20, allocated_at="2026-08-01T00:00:00+00:00"):
        root, preview, raw, translations, output, batch_id = fixture
        with mock.patch.object(MODULE, "_historical_exclusions", return_value=(set(), set())):
            rows, stats = MODULE.build_batch(
                preview, [raw], translations, output, batch_id,
                repository_root=root, maximum=maximum,
                allocated_at=allocated_at,
                config_path=ROOT / "config/classification.json")
        return rows, stats

    def test_complete_build_batch_writes_contract_and_field_order(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.build_fixture(directory, ids=(1, 2), existing_batches=1)
            rows, stats = self.build(fixture)
            output = fixture[4]
            self.assertEqual(2, len(rows)); self.assertEqual(2, stats["batch_sequence"])
            self.assertEqual([2, 1], [row["chinese_post_id"] for row in rows])
            self.assertTrue(all(
                row["source_type"] == "mixed_syntaxhighlighter_daily"
                for row in rows))
            with output.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle); persisted = list(reader)
            self.assertEqual(list(MODULE.FIELDS), reader.fieldnames)
            self.assertEqual(["2", "1"], [row["chinese_post_id"] for row in persisted])
            self.assertFalse(any(output.parent.glob(f".{output.name}.*.tmp")))

    def test_final_partial_batch_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.build_fixture(directory, ids=(1, 2, 3))
            rows, stats = self.build(fixture, maximum=20)
            self.assertEqual(3, len(rows)); self.assertEqual(3, stats["selected_count"])
            self.assertEqual(0, stats["remaining_after_selection"])
            self.assertTrue(fixture[4].is_file())

    def test_preview_raw_identity_mismatches_fail_closed(self):
        cases = {
            "content_sha256": "0" * 64,
            "chinese_title": "不同标题",
            "published_at": "2000-01-01 00:00:00",
            "permalink": "https://example.invalid/different/",
        }
        for field, value in cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture = self.build_fixture(
                    directory, preview_changes={field: value})
                with self.assertRaisesRegex(MODULE.MixedBatchError, "preview/raw"):
                    self.build(fixture)
                self.assertFalse(fixture[4].exists())

    def test_preview_raw_structure_mismatch_fails_closed(self):
        for field, value in (
                ("syntaxhighlighter_count", "2"),
                ("code_block_pro_count", "1"),
                ("editor_format", "gutenberg"),
                ("syntaxhighlighter_balanced", "False"),
                ("mixed_code_formats", "True")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture = self.build_fixture(
                    directory, preview_changes={field: value})
                with self.assertRaisesRegex(
                        MODULE.MixedBatchError, "structure mismatch"):
                    self.build(fixture)
                self.assertFalse(fixture[4].exists())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.build_fixture(directory)
            fixture[4].write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.MixedBatchError, "overwrite"):
                self.build(fixture)
            self.assertEqual("keep\n", fixture[4].read_text(encoding="utf-8"))

    def test_atomic_replace_failure_leaves_no_output_or_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.build_fixture(directory)
            with mock.patch.object(
                    MODULE.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    self.build(fixture)
            output = fixture[4]
            self.assertFalse(output.exists())
            self.assertFalse(any(output.parent.glob(f".{output.name}.*.tmp")))

    def test_batch_id_filename_and_sequence_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = list(self.build_fixture(directory))
            fixture[5] = "invalid-batch"
            with self.assertRaisesRegex(MODULE.MixedBatchError, "batch ID"):
                self.build(tuple(fixture))
        with tempfile.TemporaryDirectory() as directory:
            fixture = list(self.build_fixture(directory))
            fixture[4] = fixture[4].with_name(
                "mixed-syntaxhighlighter-migration-batch-20260101-01.csv")
            with self.assertRaisesRegex(MODULE.MixedBatchError, "filename"):
                self.build(tuple(fixture))
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.build_fixture(directory, existing_batches=2)
            first = fixture[0] / "data/analysis/mixed-syntaxhighlighter-migration-batch-20260721-01.csv"
            with first.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["batch_sequence"] = "2"; self.write_csv(first, rows)
            with self.assertRaisesRegex(MODULE.MixedBatchError, "duplicated"):
                self.build(fixture)

    def test_derived_csv_is_ignored_for_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.build_fixture(directory, existing_batches=1)
            derived = fixture[0] / "data/analysis/mixed-syntaxhighlighter-migration-batch-20260721-01-validation.csv"
            self.write_csv(derived, [{"irrelevant": "value"}])
            rows, stats = self.build(fixture)
            self.assertEqual(2, stats["batch_sequence"])
            self.assertTrue(rows)

    def test_cli_success_and_argument_error_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.build_fixture(directory, ids=(1,))
            root, preview, raw, translations, output, batch_id = fixture
            with mock.patch.object(
                    MODULE, "_historical_exclusions", return_value=(set(), set())), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                MODULE.main([
                    "--preview", str(preview), "--translations", str(translations),
                    "--output", str(output), "--batch-id", batch_id,
                    "--repo-root", str(root), "--config",
                    str(ROOT / "config/classification.json"), str(raw)])
            self.assertTrue(output.is_file())
            self.assertIn("Batch written", stdout.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.main([])


if __name__ == "__main__":
    unittest.main()
