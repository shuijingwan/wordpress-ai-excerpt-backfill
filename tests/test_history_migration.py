import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/history-migration.py"
SPEC = importlib.util.spec_from_file_location("history_migration", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


SYNTAX_FIELDS = [
    "schema_version", "batch_id", "batch_sequence", "allocated_at",
    "chinese_post_id", "english_post_id", "chinese_title", "published_at",
    "before_content_sha256", "before_syntaxhighlighter_count",
    "before_code_block_pro_count", "migration_status", "validation_status",
]
MIXED_FIELDS = [
    field for field in SYNTAX_FIELDS if field != "before_content_sha256"
] + [
    "batch_expected_count", "source_editor_format", "target_editor_format",
    "source_migration_type", "source_type", "content_sha256", "snapshot_id",
    "snapshot_generated_at", "expected_code_block_pro_count_after",
]
MANIFEST_FIELDS = [
    "chinese_post_id", "english_post_id", "chinese_title", "execution_status",
]


class HistoryMigrationStatusTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data/analysis").mkdir(parents=True)
        (self.root / "data/backups/single-candidate").mkdir(parents=True)
        self.write_manifest(
            self.root / MODULE.LEGACY_BATCH["relative_path"],
            [(100, 1100)], expected_override=1,
        )
        self.write_manifest(
            self.root / MODULE.PILOT_BATCH["relative_path"],
            [(200, 1200)], expected_override=1,
        )
        self.original_legacy_count = MODULE.LEGACY_BATCH["expected_count"]
        MODULE.LEGACY_BATCH["expected_count"] = 1

    def tearDown(self):
        MODULE.LEGACY_BATCH["expected_count"] = self.original_legacy_count
        self.temporary.cleanup()

    def write_csv(self, path, fields, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def write_manifest(self, path, pairs, expected_override=None):
        del expected_override
        self.write_csv(path, MANIFEST_FIELDS, [
            {
                "chinese_post_id": chinese,
                "english_post_id": english,
                "chinese_title": f"标题 {chinese}",
                "execution_status": "pending",
            }
            for chinese, english in pairs
        ])

    def write_batch(self, name, pairs, sequence=1, fields=None,
                    batch_expected_count=None):
        path = self.root / "data/analysis" / name
        values = []
        batch_id = "syntaxhighlighter-" + name[
            len("syntaxhighlighter-migration-batch-"):-len(".csv")
        ]
        for chinese, english in pairs:
            values.append({
                "schema_version": 1,
                "batch_id": batch_id,
                "batch_sequence": sequence,
                "batch_expected_count": batch_expected_count,
                "allocated_at": f"2026-07-{20 + sequence:02d}T00:00:00+00:00",
                "chinese_post_id": chinese,
                "english_post_id": english,
                "chinese_title": f"标题 {chinese}",
                "published_at": f"2020-01-{chinese % 28 + 1:02d} 00:00:00",
                "before_content_sha256": "a" * 64,
                "before_syntaxhighlighter_count": 1,
                "before_code_block_pro_count": 0,
                "migration_status": "pending",
                "validation_status": "not-checked",
            })
        output_fields = fields or SYNTAX_FIELDS
        if batch_expected_count is not None and fields is None:
            output_fields = SYNTAX_FIELDS + ["batch_expected_count"]
        self.write_csv(path, output_fields, values)
        return path, batch_id

    def write_mixed_batch(self, pairs, sequence=1, suffix="20260801-01"):
        path = (
            self.root / "data/analysis"
            / f"mixed-syntaxhighlighter-migration-batch-{suffix}.csv"
        )
        batch_id = f"mixed-syntaxhighlighter-{suffix}"
        values = [{
            "schema_version": 1,
            "batch_id": batch_id,
            "batch_sequence": sequence,
            "batch_expected_count": len(pairs),
            "allocated_at": "2026-08-01T00:00:00+00:00",
            "chinese_post_id": chinese,
            "english_post_id": english,
            "chinese_title": f"标题 {chinese}",
            "published_at": "2024-01-01 00:00:00",
            "before_syntaxhighlighter_count": 1,
            "before_code_block_pro_count": 0,
            "expected_code_block_pro_count_after": 1,
            "migration_status": "pending",
            "validation_status": "not-checked",
            "source_editor_format": "mixed",
            "target_editor_format": "gutenberg",
            "source_type": "mixed_syntaxhighlighter_daily",
            "source_migration_type":
                "mixed-syntaxhighlighter-to-gutenberg-code-block-pro",
            "content_sha256": "f" * 64,
            "snapshot_id": "snapshot-20260801",
            "snapshot_generated_at": "2026-08-01T00:00:00+00:00",
        } for chinese, english in pairs]
        self.write_csv(path, MIXED_FIELDS, values)
        return path, batch_id

    def write_special_mixed_batch(self, pairs):
        path = (
            self.root / "data/analysis"
            / "mixed-syntaxhighlighter-special-batch-20260812-01.csv"
        )
        batch_id = "mixed-syntaxhighlighter-special-20260812-01"
        values = [{
            "schema_version": 1,
            "batch_id": batch_id,
            "batch_sequence": 1,
            "batch_expected_count": len(pairs),
            "allocated_at": "2026-08-12T00:00:00+00:00",
            "chinese_post_id": chinese,
            "english_post_id": english,
            "chinese_title": f"标题 {chinese}",
            "published_at": "2024-01-01 00:00:00",
            "before_syntaxhighlighter_count": 2,
            "before_code_block_pro_count": 0,
            "expected_code_block_pro_count_after": 2,
            "migration_status": "pending",
            "validation_status": "not-checked",
            "source_editor_format": "mixed",
            "target_editor_format": "gutenberg",
            "source_type": "mixed_syntaxhighlighter_special",
            "source_migration_type":
                "mixed-syntaxhighlighter-to-gutenberg-code-block-pro",
            "content_sha256": "e" * 64,
            "snapshot_id": "production-readonly-special-audit",
            "snapshot_generated_at": "2026-08-12T00:00:00+00:00",
        } for chinese, english in pairs]
        self.write_csv(path, MIXED_FIELDS, values)
        return path, batch_id

    def write_execution(self, chinese, english, status="completed", raw=None):
        path = (
            self.root / "data/backups/single-candidate"
            / f"chinese-{chinese}.execution.json"
        )
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(json.dumps({
                "schema_version": 1,
                "chinese_post_id": chinese,
                "english_post_id": english,
                "status": status,
            }), encoding="utf-8")
        return path

    def write_validation(self, batch_id, pairs, status="ready"):
        suffix = batch_id.removeprefix("syntaxhighlighter-")
        path = (
            self.root / "data/analysis"
            / f"syntaxhighlighter-migration-batch-{suffix}-validation.csv"
        )
        self.write_csv(
            path,
            ["batch_id", "chinese_post_id", "english_post_id",
             "validation_status", "validated_at"],
            [{
                "batch_id": batch_id,
                "chinese_post_id": chinese,
                "english_post_id": english,
                "validation_status": status,
                "validated_at": "2026-07-22T00:00:00+00:00",
            } for chinese, english in pairs],
        )
        return path

    def write_record_validation(self, chinese=401, english=1401, **changes):
        path = self.root / "evidence/validation.csv"
        fields = [
            "schema_version", "batch_id", "batch_sequence", "validated_at",
            "chinese_post_id", "english_post_id", "chinese_title",
            "before_content_sha256", "after_content_sha256",
            "before_syntaxhighlighter_count", "after_syntaxhighlighter_count",
            "before_code_block_pro_count", "expected_code_block_pro_count_after",
            "after_code_block_pro_count", "code_block_pro_languages",
            "chinese_excerpt_empty", "chinese_status", "chinese_language",
            "english_status", "polylang_relation_status", "gutenberg_balanced",
            "validation_status", "validation_reasons",
        ]
        value = {
            "schema_version": 1,
            "batch_id": "syntaxhighlighter-20260723-01",
            "batch_sequence": 2,
            "validated_at": "2026-07-23T00:00:00+00:00",
            "chinese_post_id": chinese,
            "english_post_id": english,
            "chinese_title": f"标题 {chinese}",
            "before_content_sha256": "a" * 64,
            "after_content_sha256": "b" * 64,
            "before_syntaxhighlighter_count": 1,
            "after_syntaxhighlighter_count": 0,
            "before_code_block_pro_count": 0,
            "expected_code_block_pro_count_after": 1,
            "after_code_block_pro_count": 1,
            "code_block_pro_languages": "plaintext",
            "chinese_excerpt_empty": "True",
            "chinese_status": "publish",
            "chinese_language": "zh",
            "english_status": "publish",
            "polylang_relation_status": "normal",
            "gutenberg_balanced": "True",
            "validation_status": "ready",
            "validation_reasons": "",
        }
        value.update(changes)
        self.write_csv(path, fields, [value])
        return path

    def validation_row(self, **changes):
        path = self.write_record_validation(**changes)
        with path.open(encoding="utf-8", newline="") as handle:
            return next(csv.DictReader(handle))

    def fake_source(self):
        class Source:
            posts = {
                401: {
                    "id": 401, "status": "publish",
                    "title": {"raw": "标题 401"},
                    "excerpt": {"raw": ""},
                    "content": {"raw": "not persisted"},
                },
                1401: {
                    "id": 1401, "status": "publish",
                    "title": {"raw": "English"},
                    "excerpt": {"raw": ""},
                    "content": {"raw": "English body"},
                },
            }

            def get_post(self, post_id):
                return self.posts[int(post_id)]

            def check(self, chinese, english):
                return {}
        return Source()

    def prepare_converted(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 1, True)
        (self.root / "config").mkdir()
        (self.root / "config/classification.json").write_text(
            "{}", encoding="utf-8")

    def create_execution_manifest(self, post_id=401):
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", post_id
        ).read_text(encoding="utf-8"))
        path = MODULE._validation_paths(
            self.root, state["batch_id"], post_id)[2]
        row = {
            field: "" for field in MODULE.EXECUTION_MANIFEST_FIELDS
        }
        row.update({
            "chinese_post_id": post_id,
            "chinese_title": f"标题 {post_id}",
            "chinese_content_sha256": "b" * 64,
            "chinese_excerpt_empty": "True",
            "english_post_id": post_id + 1000,
            "english_post_status": "publish",
            "english_title_sha256": "c" * 64,
            "english_excerpt_sha256": "d" * 64,
            "english_content_sha256": "e" * 64,
            "candidate_reason": "test",
            "execution_status": "pending",
            "expected_code_block_pro_count": "1",
            "expected_syntaxhighlighter_count": "0",
        })
        MODULE._atomic_write_csv(
            path, MODULE.EXECUTION_MANIFEST_FIELDS, [row])
        return path

    def prepare_blocked_run(self, post_id=401):
        self.prepare_converted()
        path = self.write_record_validation(
            chinese=post_id, english=post_id + 1000)
        MODULE.record_validation(
            self.root, post_id, str(path.relative_to(self.root)))
        self.create_execution_manifest(post_id)
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", post_id)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        attempt = MODULE._record_attempt_start(self.root, state, "run")
        MODULE._block_after_operation_error(
            self.root, state, "run", attempt, {
                "category": "executor_failed_without_state",
                "returncode": 1, "stderr_summary": "safe error",
                "stdout_summary": "",
            })
        return state_path

    def prepare_init_fixture(self):
        self.write_execution(100, 1100)
        self.write_execution(200, 1200)
        completed_pairs = [(value, value + 1000) for value in range(301, 321)]
        _, completed_batch = self.write_batch(
            "syntaxhighlighter-migration-batch-20260722-01.csv",
            completed_pairs, sequence=1,
        )
        for chinese, english in completed_pairs:
            self.write_execution(chinese, english)
        self.write_validation(completed_batch, completed_pairs)
        waiting_pairs = [(value, value + 1000) for value in range(401, 421)]
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260723-01.csv",
            waiting_pairs, sequence=2,
        )
        return completed_pairs, waiting_pairs

    def snapshot(self):
        return {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

    def status(self):
        return MODULE.build_status(self.root)

    def write_priority_batch(self, pairs):
        path, _ = self.write_batch(
            "syntaxhighlighter-migration-batch-20260724-01.csv",
            pairs, sequence=3)
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            row["batch_id"] = "syntaxhighlighter-priority-20260724-01"
        self.write_csv(path, SYNTAX_FIELDS, rows)
        return path

    def test_reads_valid_fixed_batch_and_preserves_order(self):
        pairs = [(303, 1303), (301, 1301), (302, 1302)]
        pairs.extend((value, value + 1000) for value in range(304, 321))
        _, batch_id = self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv",
            pairs,
        )
        result = self.status()
        batch = next(item for item in MODULE.discover_batches(self.root, [])
                     if item["batch_id"] == batch_id)
        self.assertEqual([303, 301, 302],
                         [item["chinese_post_id"] for item in batch["articles"][:3]])
        self.assertEqual(3, len(result["batches"]))
        self.assertTrue(result["integrity_ok"])

    def test_finds_multiple_batches_but_not_derived_csvs(self):
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv", [(301, 1301)])
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260722-01.csv", [(302, 1302)], 2)
        self.write_csv(
            self.root / "data/analysis/"
            "syntaxhighlighter-migration-batch-20260722-01-validation.csv",
            ["batch_id", "chinese_post_id", "english_post_id", "validation_status"],
            [{
                "batch_id": "syntaxhighlighter-20260722-01",
                "chinese_post_id": 302,
                "english_post_id": 1302,
                "validation_status": "ready",
            }],
        )
        self.write_csv(
            self.root / "data/analysis/"
            "syntaxhighlighter-migration-batch-20260722-01-execution-candidates.csv",
            MANIFEST_FIELDS,
            [{
                "chinese_post_id": 302,
                "english_post_id": 1302,
                "chinese_title": "标题 302",
                "execution_status": "pending",
            }],
        )
        result = self.status()
        self.assertEqual(4, len(result["batches"]))
        self.assertEqual(1, next(
            item for item in result["batches"]
            if item["batch_id"] == "syntaxhighlighter-20260722-01"
        )["validation_evidence_count"])

    def test_discovers_old_and_mixed_batches_as_separate_families(self):
        _, old_id = self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv",
            [(301, 1301)])
        _, mixed_id = self.write_mixed_batch([(501, 1501)], sequence=2)
        errors = []
        batches = MODULE.discover_batches(self.root, errors)
        self.assertEqual([], errors)
        by_id = {batch["batch_id"]: batch for batch in batches}
        self.assertEqual("syntaxhighlighter_daily", by_id[old_id]["source_type"])
        self.assertEqual(
            "mixed_syntaxhighlighter_daily", by_id[mixed_id]["source_type"])
        self.assertNotEqual(old_id, mixed_id)
        self.assertEqual(
            "f" * 64,
            by_id[mixed_id]["articles"][0]["source_row"][
                "before_content_sha256"])

    def test_mixed_manifest_contract_is_strict(self):
        path, _ = self.write_mixed_batch([(501, 1501)])
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["target_editor_format"] = "mixed"
        self.write_csv(path, MIXED_FIELDS, rows)
        errors = []
        MODULE.discover_batches(self.root, errors)
        self.assertTrue(any(
            "target_editor_format must be 'gutenberg'" in error
            for error in errors))

    def test_mixed_manual_conversion_requires_normalization_confirmation(self):
        self.write_execution(100, 1100)
        self.write_execution(200, 1200)
        _, batch_id = self.write_mixed_batch([(501, 1501)])
        MODULE.init_state(self.root, apply=True)
        with self.assertRaisesRegex(
                MODULE.ReadError, "gutenberg-normalization-confirmed"):
            MODULE.mark_converted(self.root, 501, 1, 1, True)
        state = MODULE.mark_converted(
            self.root, 501, 1, 1, True,
            gutenberg_normalization_confirmed=True)
        self.assertEqual(
            "awaiting_readonly_validation", state["workflow_status"])
        saved = json.loads(MODULE._state_path(
            self.root, batch_id, 501).read_text(encoding="utf-8"))
        self.assertEqual(
            "confirmed", saved["gutenberg_normalization"]["status"])
        self.assertEqual("confirmed", saved["manual_conversion"]["status"])
        self.assertEqual("confirmed", saved["language_review"]["status"])

    def test_special_mixed_batch_initializes_and_reuses_manual_conversion_flow(self):
        self.write_execution(100, 1100)
        self.write_execution(200, 1200)
        _, batch_id = self.write_special_mixed_batch([
            (2710, 11572), (4984, 14491), (5152, 14415),
            (5520, 14235), (12389, 12394),
        ])
        result = MODULE.init_state(self.root, apply=True)
        self.assertEqual(5, next(
            item["created_count"] for item in result["batches"]
            if item["batch_id"] == batch_id))
        status = MODULE.build_status(self.root)
        special = next(item for item in status["batches"] if item["batch_id"] == batch_id)
        self.assertEqual("mixed_syntaxhighlighter_special", special["source_type"])
        self.assertEqual({"awaiting_manual_conversion": 5}, special["coordination_status_counts"])
        state = MODULE.mark_converted(
            self.root, 2710, 2, 2, True,
            gutenberg_normalization_confirmed=True)
        self.assertEqual("awaiting_readonly_validation", state["workflow_status"])

    def test_special_mixed_execution_gate_requires_recorded_ready_validation(self):
        _, batch_id = self.write_special_mixed_batch([(4984, 14491)])
        batch = next(item for item in MODULE._context(self.root)[1]
                     if item["batch_id"] == batch_id)
        state = {
            "batch_id": batch_id,
            "chinese_post_id": 4984,
            "workflow_status": "awaiting_manual_conversion",
        }
        self.assertFalse(MODULE._special_validated_mixed_execution_allowed(batch, state))
        state.update({
            "workflow_status": "ready_for_execution",
            "manual_conversion": {"status": "confirmed"},
            "gutenberg_normalization": {"status": "confirmed"},
            "language_review": {"status": "confirmed"},
            "validation_evidence": {
                "status": "ready", "failure_reasons": [],
            },
        })
        self.assertTrue(MODULE._special_validated_mixed_execution_allowed(batch, state))
        manifest = MODULE._validation_paths(self.root, batch_id, 4984)[2]
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("manifest", encoding="utf-8")
        command = MODULE._executor_command(
            self.root, state, preflight=True, special_validated_mixed=True)
        self.assertIn("--special-validated-mixed", command)
        ordinary = dict(batch, source_type="syntaxhighlighter_daily")
        self.assertFalse(MODULE._special_validated_mixed_execution_allowed(ordinary, state))

    def test_execution_manifest_distinguishes_migration_source(self):
        article = {
            "chinese_post_id": 401, "english_post_id": 1401,
            "title": "标题 401",
        }
        row = self.validation_row()
        source = self.fake_source()
        old = MODULE._execution_manifest_row(article, row, source)
        mixed = MODULE._execution_manifest_row(
            article, row, source, "mixed_syntaxhighlighter_daily")
        self.assertEqual(
            "syntaxhighlighter-to-code-block-pro",
            old["source_migration_type"])
        self.assertEqual(
            "mixed-syntaxhighlighter-to-gutenberg-code-block-pro",
            mixed["source_migration_type"])

    def test_status_keeps_old_and_mixed_batches_separate(self):
        self.write_execution(100, 1100)
        self.write_execution(200, 1200)
        _, old_id = self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv",
            [(301, 1301)])
        self.write_execution(301, 1301)
        _, mixed_id = self.write_mixed_batch([(501, 1501)], sequence=2)
        result = self.status()
        by_id = {batch["batch_id"]: batch for batch in result["batches"]}
        self.assertIn(old_id, by_id)
        self.assertIn(mixed_id, by_id)
        self.assertEqual(
            "syntaxhighlighter_daily", by_id[old_id]["source_type"])
        self.assertEqual(
            "mixed_syntaxhighlighter_daily", by_id[mixed_id]["source_type"])

    def test_completed_and_missing_execution_evidence(self):
        self.write_execution(100, 1100)
        result = self.status()
        self.assertEqual(1, result["execution_counts"]["completed"])
        self.assertEqual(1, result["execution_counts"]["no_execution_evidence"])
        self.assertTrue(result["integrity_ok"])

    def test_damaged_execution_json_is_error(self):
        self.write_execution(100, 1100, raw="{bad")
        result = self.status()
        self.assertFalse(result["integrity_ok"])
        self.assertTrue(any("invalid execution JSON" in item for item in result["errors"]))

    def test_recognizes_real_execution_status_categories(self):
        pairs = [(value, value + 1000) for value in range(301, 321)]
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv", pairs)
        self.write_execution(301, 1301, "completed")
        self.write_execution(302, 1302, "translation_started")
        self.write_execution(303, 1303, "translation_failed")
        self.write_execution(304, 1304, "pending")
        counts = self.status()["execution_counts"]
        self.assertEqual(1, counts["completed"])
        self.assertEqual(1, counts["translation_started"])
        self.assertEqual(1, counts["failed"])
        self.assertEqual(1, counts["pending"])

    def test_duplicate_chinese_id_within_batch(self):
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv",
            [(301, 1301), (301, 1301)],
        )
        result = self.status()
        self.assertTrue(any(
            item["type"] == "duplicate_chinese_post_id_within_batch"
            for item in result["conflicts"]
        ))

    def test_duplicate_chinese_id_across_batches(self):
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv", [(100, 1100)])
        result = self.status()
        self.assertTrue(any(
            item["type"] == "duplicate_chinese_post_id_across_batches"
            for item in result["conflicts"]
        ))

    def test_different_english_mapping_is_conflict(self):
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv", [(100, 9999)])
        result = self.status()
        self.assertTrue(any(
            item.get("english_mapping_conflict") is True
            for item in result["conflicts"]
        ))

    def test_missing_required_field_is_error(self):
        fields = [field for field in SYNTAX_FIELDS if field != "english_post_id"]
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv",
            [(301, 1301)], fields=fields,
        )
        result = self.status()
        self.assertFalse(result["integrity_ok"])
        self.assertTrue(any("missing required fields: english_post_id" in item
                            for item in result["errors"]))

    def test_invalid_post_id_is_error(self):
        path, _ = self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv", [(301, 1301)])
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["chinese_post_id"] = "not-an-id"
        self.write_csv(path, SYNTAX_FIELDS, rows)
        result = self.status()
        self.assertFalse(result["integrity_ok"])
        self.assertTrue(any("invalid chinese_post_id" in item for item in result["errors"]))

    def test_abnormal_fixed_count_is_error(self):
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv", [(301, 1301)])
        result = self.status()
        self.assertFalse(result["integrity_ok"])
        self.assertTrue(any("expected 20 fixed articles, found 1" in item
                            for item in result["errors"]))

    def test_explicit_five_article_batch_count_is_strict(self):
        pairs = [(value, value + 1000) for value in range(501, 506)]
        path = self.write_priority_batch(pairs)
        result = self.status()
        self.assertTrue(result["integrity_ok"])
        batch = next(item for item in MODULE.discover_batches(self.root, [])
                     if item["batch_id"]
                     == "syntaxhighlighter-priority-20260724-01")
        self.assertEqual(5, batch["expected_count"])

        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.write_csv(path, SYNTAX_FIELDS, rows[:-1])
        result = self.status()
        self.assertTrue(any("expected 5 fixed articles, found 4" in error
                            for error in result["errors"]))

        rows.append({
            **rows[-1], "chinese_post_id": 506, "english_post_id": 1506,
        })
        self.write_csv(path, SYNTAX_FIELDS, rows)
        result = self.status()
        self.assertTrue(any("expected 5 fixed articles, found 6" in error
                            for error in result["errors"]))

    def test_metadata_fixed_twelve_article_batch_count_is_strict(self):
        pairs = [(value, value + 1000) for value in range(701, 713)]
        path, batch_id = self.write_batch(
            "syntaxhighlighter-migration-batch-20260729-01.csv",
            pairs, sequence=4, batch_expected_count=12)
        result = self.status()
        self.assertTrue(result["integrity_ok"])
        batch = next(item for item in MODULE.discover_batches(self.root, [])
                     if item["batch_id"] == batch_id)
        self.assertEqual(12, batch["expected_count"])

        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = reader.fieldnames
        self.write_csv(path, fields, rows[:-1])
        result = self.status()
        self.assertTrue(any("expected 12 fixed articles, found 11" in error
                            for error in result["errors"]))

        rows.append({
            **rows[-1], "chinese_post_id": 713, "english_post_id": 1713,
        })
        self.write_csv(path, fields, rows)
        result = self.status()
        self.assertTrue(any("expected 12 fixed articles, found 13" in error
                            for error in result["errors"]))

    def test_current_twelve_article_legacy_batch_mapping_remains_compatible(self):
        pairs = [(value, value + 1000) for value in range(801, 813)]
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260728-01.csv",
            pairs, sequence=4)
        result = self.status()
        self.assertTrue(result["integrity_ok"])

    def test_existing_expected_counts_and_default_twenty_are_unchanged(self):
        self.assertEqual(42, self.original_legacy_count)
        self.assertEqual(1, MODULE.PILOT_BATCH["expected_count"])
        self.assertEqual(20, MODULE.DEFAULT_SYNTAX_BATCH_EXPECTED_COUNT)
        self.assertEqual(
            20, MODULE.SYNTAX_BATCH_EXPECTED_COUNTS[
                "syntaxhighlighter-20260722-01"])
        pairs = [(value, value + 1000) for value in range(601, 620)]
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260725-01.csv",
            pairs, sequence=4)
        result = self.status()
        self.assertTrue(any("expected 20 fixed articles, found 19" in error
                            for error in result["errors"]))

    def test_priority_batch_init_mark_and_validate_live(self):
        pairs = [(value, value + 1000) for value in range(501, 506)]
        self.write_priority_batch(pairs)
        self.write_execution(100, 1100)
        self.write_execution(200, 1200)
        result = MODULE.init_state(self.root, apply=True)
        self.assertEqual(7, result["created_count"])
        self.assertTrue(all(
            MODULE._state_path(
                self.root, "syntaxhighlighter-priority-20260724-01", post_id
            ).is_file()
            for post_id in range(501, 506)))
        state = MODULE.mark_converted(self.root, 501, 1, 1, True)
        self.assertEqual("awaiting_readonly_validation", state["workflow_status"])
        row = self.validation_row(
            batch_id="syntaxhighlighter-priority-20260724-01",
            batch_sequence=3, chinese_post_id=501, english_post_id=1501,
            chinese_title="标题 501")
        source = self.fake_source()
        source.posts[501] = {
            **source.posts.pop(401), "id": 501,
            "title": {"raw": "标题 501"},
        }
        source.posts[1501] = {**source.posts.pop(1401), "id": 1501}
        (self.root / "config").mkdir()
        (self.root / "config/classification.json").write_text(
            "{}", encoding="utf-8")
        with mock.patch(
                "src.syntaxhighlighter_batch_validation.validate_batch",
                return_value=[row]):
            validated = MODULE.validate_live(
                self.root, 501, source_factory=lambda rows: source)
        self.assertEqual("ready_for_execution", validated["workflow_status"])

    def test_json_output_is_valid_and_incomplete_is_success(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main([
                "status", "--json", "--repo-root", str(self.root),
            ])
        value = json.loads(output.getvalue())
        self.assertEqual(MODULE.EXIT_OK, code)
        self.assertGreater(value["execution_counts"]["no_execution_evidence"], 0)

    def test_integrity_conflict_returns_nonzero(self):
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv", [(100, 1100)])
        with redirect_stdout(io.StringIO()):
            code = MODULE.main(["status", "--repo-root", str(self.root)])
        self.assertEqual(MODULE.EXIT_INTEGRITY_ERROR, code)

    def test_status_is_strictly_read_only(self):
        self.write_batch(
            "syntaxhighlighter-migration-batch-20260721-01.csv", [(301, 1301)])
        before = self.snapshot()
        self.status()
        after = self.snapshot()
        self.assertEqual(before, after)

    def test_init_state_preview_is_read_only_and_json_is_valid(self):
        self.prepare_init_fixture()
        before = self.snapshot()
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main([
                "init-state", "--json", "--repo-root", str(self.root),
            ])
        result = json.loads(output.getvalue())
        self.assertEqual(MODULE.EXIT_OK, code)
        self.assertEqual(42, result["planned_count"])
        self.assertEqual(42, result["would_create_count"])
        self.assertEqual(0, result["created_count"])
        self.assertFalse(result["writes_performed"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / MODULE.STATE_ROOT).exists())

    def test_apply_creates_identity_fields_and_expected_mappings(self):
        _, waiting = self.prepare_init_fixture()
        result = MODULE.init_state(self.root, apply=True)
        self.assertTrue(result["integrity_ok"])
        self.assertEqual(42, result["created_count"])
        self.assertEqual(0, result["unchanged_count"])
        self.assertEqual(22, result["legacy_import_count"])
        self.assertEqual(20, result["awaiting_manual_conversion_count"])
        waiting_state = json.loads(
            MODULE._state_path(self.root, "syntaxhighlighter-20260723-01", 401)
            .read_text(encoding="utf-8")
        )
        self.assertEqual(waiting[0][0], waiting_state["chinese_post_id"])
        self.assertEqual("syntaxhighlighter-20260723-01", waiting_state["batch_id"])
        self.assertEqual(1, waiting_state["batch_position"])
        self.assertEqual(
            "data/analysis/syntaxhighlighter-migration-batch-20260723-01.csv",
            waiting_state["source_batch_file"],
        )
        self.assertEqual(64, len(waiting_state["source_batch_sha256"]))
        self.assertEqual(64, len(waiting_state["source_row_sha256"]))
        self.assertEqual(
            "awaiting_manual_conversion", waiting_state["workflow_status"])
        self.assertFalse(waiting_state["legacy_import"])

    def test_historical_completed_evidence_and_manual_unknowns_are_explicit(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        cbp = json.loads(
            MODULE._state_path(self.root, "gutenberg-cbp-fixed-42", 100)
            .read_text(encoding="utf-8")
        )
        pilot = json.loads(
            MODULE._state_path(self.root, "syntaxhighlighter-pilot-17586", 200)
            .read_text(encoding="utf-8")
        )
        migrated = json.loads(
            MODULE._state_path(self.root, "syntaxhighlighter-20260722-01", 301)
            .read_text(encoding="utf-8")
        )
        self.assertEqual("completed", cbp["workflow_status"])
        self.assertTrue(cbp["legacy_import"])
        self.assertEqual("not_applicable", cbp["manual_conversion"]["status"])
        self.assertEqual(
            "historical_unrecorded", cbp["language_review"]["status"])
        self.assertEqual("historical_unrecorded",
                         pilot["manual_conversion"]["status"])
        self.assertIsNone(pilot["validation_evidence"])
        self.assertEqual("ready", migrated["validation_evidence"]["status"])
        self.assertNotIn("confirmed_at", migrated["manual_conversion"])
        self.assertNotIn("confirmed_by", migrated["language_review"])

    def test_second_apply_is_idempotent_and_does_not_repeat_events(self):
        self.prepare_init_fixture()
        first = MODULE.init_state(self.root, apply=True)
        self.assertEqual(42, first["created_count"])
        state_root = self.root / MODULE.STATE_ROOT
        before = {
            path.relative_to(state_root):
                (path.read_bytes(), path.stat().st_mtime_ns)
            for path in state_root.rglob("*") if path.is_file()
        }
        second = MODULE.init_state(self.root, apply=True)
        after = {
            path.relative_to(state_root):
                (path.read_bytes(), path.stat().st_mtime_ns)
            for path in state_root.rglob("*") if path.is_file()
        }
        self.assertEqual(0, second["created_count"])
        self.assertEqual(42, second["unchanged_count"])
        self.assertFalse(second["writes_performed"])
        self.assertEqual(before, after)
        events = [
            json.loads(line)
            for path in state_root.glob("*/events.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(42, len(events))
        self.assertEqual(42, len({item["event_id"] for item in events}))

    def test_existing_state_identity_conflict_is_not_overwritten(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["english_post_id"] = 9999
        path.write_text(json.dumps(value), encoding="utf-8")
        before = path.read_bytes()
        result = MODULE.init_state(self.root, apply=True)
        self.assertFalse(result["integrity_ok"])
        self.assertTrue(any(
            item["type"] == "coordination_state_identity_conflict"
            for item in result["conflicts"]
        ))
        self.assertEqual(before, path.read_bytes())

    def test_fixed_batch_drift_is_reported_by_status(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        path = (
            self.root / "data/analysis"
            / "syntaxhighlighter-migration-batch-20260723-01.csv"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        result = MODULE.build_status(self.root)
        self.assertFalse(result["integrity_ok"])
        self.assertFalse(result["state_integrity"])
        self.assertTrue(result["batch_drift"])

    def test_damaged_state_and_event_are_errors(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        state_path.write_text("{bad", encoding="utf-8")
        event_path = MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01")
        event_path.write_text("{bad\n", encoding="utf-8")
        result = MODULE.build_status(self.root)
        self.assertFalse(result["integrity_ok"])
        self.assertTrue(any("invalid coordination state JSON" in item
                            for item in result["state_errors"]))
        self.assertTrue(any("invalid event JSON" in item
                            for item in result["state_errors"]))

    def test_status_before_and_after_initialization(self):
        self.prepare_init_fixture()
        before = MODULE.build_status(self.root)
        self.assertEqual(0, before["coordination_state_count"])
        self.assertEqual(42, before["uninitialized_count"])
        MODULE.init_state(self.root, apply=True)
        after = MODULE.build_status(self.root)
        self.assertEqual(42, after["coordination_state_count"])
        self.assertEqual(0, after["uninitialized_count"])
        self.assertEqual(20, after["awaiting_manual_conversion_count"])
        self.assertEqual(22, after["coordination_status_counts"]["completed"])

    def test_apply_json_is_valid(self):
        self.prepare_init_fixture()
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main([
                "init-state", "--apply", "--json",
                "--repo-root", str(self.root),
            ])
        result = json.loads(output.getvalue())
        self.assertEqual(MODULE.EXIT_OK, code)
        self.assertEqual(42, result["created_count"])
        self.assertTrue(result["writes_performed"])

    def test_lock_conflict_returns_nonzero(self):
        self.prepare_init_fixture()
        with MODULE.InitLock(self.root):
            output = io.StringIO()
            with redirect_stdout(output):
                code = MODULE.main([
                    "init-state", "--apply", "--json",
                    "--repo-root", str(self.root),
                ])
        result = json.loads(output.getvalue())
        self.assertEqual(MODULE.EXIT_LOCK_CONFLICT, code)
        self.assertFalse(result["integrity_ok"])

    def test_atomic_state_write_failure_leaves_no_temporary_file(self):
        self.prepare_init_fixture()
        with mock.patch.object(
                MODULE, "_atomic_write_json", side_effect=OSError("injected")):
            result = MODULE.init_state(self.root, apply=True)
        self.assertFalse(result["integrity_ok"])
        state_root = self.root / MODULE.STATE_ROOT
        self.assertFalse(any(state_root.rglob("*.tmp")))
        self.assertFalse(any(state_root.glob("*/chinese-*.json")))

    def test_initialization_does_not_modify_fixed_or_execution_evidence(self):
        self.prepare_init_fixture()
        protected_roots = [
            self.root / "data/analysis",
            self.root / "data/backups/single-candidate",
        ]
        before = {
            path: path.read_bytes()
            for base in protected_roots
            for path in base.rglob("*") if path.is_file()
        }
        MODULE.init_state(self.root, apply=True)
        after = {
            path: path.read_bytes()
            for base in protected_roots
            for path in base.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)

    def test_show_current_preserves_fixed_order_and_json_is_valid(self):
        _, waiting = self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        result = MODULE.show_current(self.root)
        self.assertEqual("syntaxhighlighter-20260723-01", result["batch_id"])
        self.assertEqual([value for value, _ in waiting],
                         [item["chinese_post_id"] for item in result["articles"]])
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main([
                "show-current", "--json", "--repo-root", str(self.root)])
        self.assertEqual(MODULE.EXIT_OK, code)
        self.assertEqual(result["batch_id"], json.loads(output.getvalue())["batch_id"])

    def test_mark_converted_requires_language_and_matching_syntax_count(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        with self.assertRaisesRegex(MODULE.ReadError, "language-review-confirmed"):
            MODULE.mark_converted(self.root, 401, 1, 1, False)
        result = MODULE.mark_converted(self.root, 401, 1, 2, True)
        self.assertEqual("awaiting_readonly_validation", result["workflow_status"])

    def test_mark_converted_is_atomic_and_idempotent(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        first = MODULE.mark_converted(self.root, 401, 1, 1, True)
        path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        event_path = MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01")
        before = (path.read_bytes(), event_path.read_bytes(), path.stat().st_mtime_ns)
        second = MODULE.mark_converted(self.root, 401, 1, 1, True)
        after = (path.read_bytes(), event_path.read_bytes(), path.stat().st_mtime_ns)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(before, after)
        self.assertEqual("awaiting_readonly_validation", state["workflow_status"])
        self.assertEqual("confirmed", state["manual_conversion"]["status"])
        self.assertEqual("confirmed", state["language_review"]["status"])

    def test_mark_converted_rejects_cbp_and_completed(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        with self.assertRaisesRegex(MODULE.ReadError, "only accepts"):
            MODULE.mark_converted(self.root, 100, 1, 1, True)
        with self.assertRaisesRegex(MODULE.ReadError, "cannot mark"):
            MODULE.mark_converted(self.root, 301, 1, 1, True)

    def test_record_validation_passes_and_is_idempotent(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 1, True)
        path = self.write_record_validation()
        first = MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        event_path = MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01")
        before = (state_path.read_bytes(), event_path.read_bytes(),
                  state_path.stat().st_mtime_ns)
        second = MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.assertTrue(first["validation_passed"])
        self.assertEqual("ready_for_execution", first["workflow_status"])
        self.assertFalse(second["changed"])
        self.assertEqual(before, (state_path.read_bytes(), event_path.read_bytes(),
                                  state_path.stat().st_mtime_ns))

    def test_record_validation_rejects_cbp_below_expected_minimum(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 1, True)
        path = self.write_record_validation(
            validation_status="abnormal",
            validation_reasons="code-block-pro-count-below-expected",
            after_code_block_pro_count=0)
        result = MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.assertEqual("validation_failed", result["workflow_status"])
        self.assertFalse(result["validation_passed"])

    def test_record_validation_accepts_cbp_above_expected_and_manual_minima(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 3, True)
        path = self.write_record_validation(after_code_block_pro_count=4)
        result = MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.assertEqual("ready_for_execution", result["workflow_status"])
        self.assertTrue(result["validation_passed"])

    def test_record_validation_rejects_cbp_below_manual_confirmed_minimum(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 3, True)
        path = self.write_record_validation(
            validation_status="abnormal",
            validation_reasons="manual-cbp-count-below-confirmed",
            after_code_block_pro_count=2)
        result = MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.assertEqual("validation_failed", result["workflow_status"])
        self.assertIn(
            "manual_cbp_count", result["failure_reasons"])

    def test_record_validation_failure_is_isolated(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 1, True)
        path = self.write_record_validation(
            validation_status="abnormal",
            validation_reasons="code-block-pro-count-below-expected",
            after_code_block_pro_count=0,
        )
        result = MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        other = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 402
        ).read_text(encoding="utf-8"))
        self.assertEqual("validation_failed", result["workflow_status"])
        self.assertEqual("awaiting_manual_conversion", other["workflow_status"])

    def test_record_validation_rejects_bad_file_identity_hash_and_paths(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 1, True)
        bad = self.root / "evidence/bad.csv"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{bad", encoding="utf-8")
        with self.assertRaises(MODULE.ReadError):
            MODULE.record_validation(self.root, 401, "evidence/bad.csv")
        mismatch = self.write_record_validation(chinese=999)
        with self.assertRaisesRegex(MODULE.ReadError, "exactly one row"):
            MODULE.record_validation(
                self.root, 401, str(mismatch.relative_to(self.root)))
        wrong_hash = self.write_record_validation(before_content_sha256="c" * 64)
        with self.assertRaisesRegex(MODULE.ReadError, "SHA-256 mismatch"):
            MODULE.record_validation(
                self.root, 401, str(wrong_hash.relative_to(self.root)))
        with self.assertRaisesRegex(MODULE.ReadError, "repository-relative"):
            MODULE.record_validation(self.root, 401, str(wrong_hash.resolve()))
        with self.assertRaisesRegex(MODULE.ReadError, "repository-relative"):
            MODULE.record_validation(self.root, 401, "../validation.csv")

    def test_record_validation_rejects_truncated_row_without_mutating_state(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 1, True)
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        event_path = MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01")
        before = (state_path.read_bytes(), event_path.read_bytes())
        path = self.write_record_validation()
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text(lines[0] + "\n" + ",".join(lines[1].split(",")[:4]) + "\n",
                        encoding="utf-8")
        with self.assertRaisesRegex(MODULE.ReadError, str(path)):
            MODULE.record_validation(
                self.root, 401, str(path.relative_to(self.root)))
        self.assertEqual(before, (state_path.read_bytes(), event_path.read_bytes()))

    def test_plan_run_only_ready_and_summary_counts(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 1, True)
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        plan = MODULE.plan_run(self.root)
        self.assertEqual([401], [item["post_id"] for item in plan["items"]])
        self.assertTrue(plan["items"][0]["allowed"])
        result = MODULE.summary(self.root)
        current = next(item for item in result["batches"]
                       if item["batch_id"] == "syntaxhighlighter-20260723-01")
        self.assertEqual(19, current["awaiting_manual_conversion"])
        self.assertEqual(1, current["ready_for_execution"])
        self.assertEqual(20, current["pending"])
        self.assertFalse(result["can_create_next_batch"])

    def test_read_only_commands_do_not_modify_files_and_drift_blocks_plan(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        before = self.snapshot()
        MODULE.show_current(self.root)
        MODULE.summary(self.root)
        MODULE.plan_run(self.root)
        self.assertEqual(before, self.snapshot())
        path = (self.root / "data/analysis/"
                "syntaxhighlighter-migration-batch-20260723-01.csv")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(MODULE.ReadError, "integrity"):
            MODULE.plan_run(self.root)

    def test_mark_converted_lock_conflict(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        with MODULE.InitLock(self.root):
            with self.assertRaisesRegex(MODULE.ReadError, "lock is already held"):
                MODULE.mark_converted(self.root, 401, 1, 1, True)

    def test_validate_live_passes_creates_scoped_evidence_and_is_idempotent(self):
        self.prepare_converted()
        row = self.validation_row()
        source = self.fake_source()
        with mock.patch(
                "src.syntaxhighlighter_batch_validation.validate_batch",
                return_value=[row]):
            first = MODULE.validate_live(
                self.root, 401, source_factory=lambda rows: source)
        paths = MODULE._validation_paths(
            self.root, "syntaxhighlighter-20260723-01", 401)
        self.assertEqual("ready_for_execution", first["workflow_status"])
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertIn("chinese-401.csv", first["validation_file"])
        before = self.snapshot()
        second = MODULE.validate_live(
            self.root, 401,
            source_factory=mock.Mock(side_effect=AssertionError("no fetch")))
        self.assertFalse(second["changed"])
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(b"not persisted", paths[0].read_bytes())
        self.assertNotIn(b"English body", paths[2].read_bytes())

    def test_validate_live_business_failure_and_operation_failure(self):
        self.prepare_converted()
        source = self.fake_source()
        failed = self.validation_row(
            validation_status="abnormal",
            validation_reasons="code-block-pro-count-below-expected",
            after_code_block_pro_count=0)
        with mock.patch(
                "src.syntaxhighlighter_batch_validation.validate_batch",
                return_value=[failed]):
            result = MODULE.validate_live(
                self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("validation_failed", result["workflow_status"])

        other = 402
        MODULE.mark_converted(self.root, other, 1, 1, True)
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", other)
        before = state_path.read_bytes()
        with self.assertRaisesRegex(MODULE.ReadError, "operation failed"):
            MODULE.validate_live(
                self.root, other,
                source_factory=mock.Mock(side_effect=OSError("network")))
        self.assertEqual(before, state_path.read_bytes())

    def test_validate_live_refresh_replaces_failed_evidence_after_success(self):
        self.prepare_converted()
        source = self.fake_source()
        failed = self.validation_row(
            validation_status="abnormal",
            validation_reasons="syntaxhighlighter-remains",
            after_syntaxhighlighter_count=1,
            after_code_block_pro_count=0)
        with mock.patch(
                "src.syntaxhighlighter_batch_validation.validate_batch",
                return_value=[failed]):
            MODULE.validate_live(
                self.root, 401, source_factory=lambda rows: source)
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        old_state = json.loads(state_path.read_text(encoding="utf-8"))

        refreshed = self.validation_row(
            after_content_sha256="c" * 64,
            after_code_block_pro_count=1)
        with mock.patch(
                "src.syntaxhighlighter_batch_validation.validate_batch",
                return_value=[refreshed]):
            result = MODULE.validate_live(
                self.root, 401, source_factory=lambda rows: source,
                refresh=True)
        new_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("ready_for_execution", result["workflow_status"])
        self.assertNotEqual(
            old_state["validation_evidence"]["sha256"],
            new_state["validation_evidence"]["sha256"])
        self.assertEqual(
            "c" * 64, new_state["validation_evidence"]["after_content_sha256"])

    def test_validate_live_refresh_timeout_preserves_failed_evidence(self):
        self.prepare_converted()
        source = self.fake_source()
        failed = self.validation_row(
            validation_status="abnormal",
            validation_reasons="syntaxhighlighter-remains",
            after_syntaxhighlighter_count=1,
            after_code_block_pro_count=0)
        with mock.patch(
                "src.syntaxhighlighter_batch_validation.validate_batch",
                return_value=[failed]):
            MODULE.validate_live(
                self.root, 401, source_factory=lambda rows: source)
        before = self.snapshot()
        with self.assertRaisesRegex(MODULE.ReadError, "operation failed"):
            MODULE.validate_live(
                self.root, 401,
                source_factory=mock.Mock(side_effect=OSError("SSH timeout")),
                refresh=True)
        self.assertEqual(before, self.snapshot())

    def test_run_ready_preview_and_execute_completed(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        before = self.snapshot()
        preview = MODULE.run_ready(self.root)
        self.assertEqual(1, preview["allowed_count"])
        self.assertEqual(before, self.snapshot())

        def runner(command, **kwargs):
            self.assertIn("execute-single-candidate.py", command[1])
            state = json.loads(MODULE._state_path(
                self.root, "syntaxhighlighter-20260723-01", 401
            ).read_text(encoding="utf-8"))
            if "--preflight-live" in command:
                self.assertEqual("ready_for_execution", state["workflow_status"])
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.assertIn("--execute", command)
            self.assertEqual("execution_in_progress", state["workflow_status"])
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner, max_attempts=1)
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual("completed", state["workflow_status"])
        self.assertEqual("completed", result["results"][0]["result"])
        self.assertEqual("completed", result["results"][0]["category"])
        self.assertEqual(0, result["results"][0]["returncode"])
        self.assertEqual("", result["results"][0]["error"])
        self.assertEqual([], MODULE.run_ready(self.root)["items"])

    def test_run_ready_intermediate_states_fail_continue_and_report_progress(self):
        self.prepare_converted()
        for post_id in (401, 402, 403):
            if post_id != 401:
                MODULE.mark_converted(self.root, post_id, 1, 1, True)
            path = self.write_record_validation(
                chinese=post_id, english=post_id + 1000)
            unique = path.with_name(f"progress-validation-{post_id}.csv")
            path.replace(unique)
            MODULE.record_validation(
                self.root, post_id, str(unique.relative_to(self.root)))
            self.create_execution_manifest(post_id)
        statuses = {401: "prepared", 402: "excerpt_generated", 403: "completed"}
        progress = []

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            post_id = int(command[command.index("--post-id") + 1])
            self.write_execution(post_id, post_id + 1000, statuses[post_id])
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            progress=lambda *values: progress.append(values),
            max_attempts=1)
        self.assertEqual(
            ["prepared", "excerpt_generated", "completed"],
            [item["result"] for item in result["results"]])
        self.assertEqual(
            ["incomplete_execution_state", "incomplete_execution_state", "completed"],
            [item["category"] for item in result["results"]])
        self.assertEqual([0, 0, 0],
                         [item["returncode"] for item in result["results"]])
        self.assertTrue(all("error" in item for item in result["results"]))
        self.assertEqual(
            [("start", 1, 3), ("attempt_failed", 1, 3),
             ("final_failed", 1, 3), ("continue", 1, 3),
             ("start", 2, 3), ("attempt_failed", 2, 3),
             ("final_failed", 2, 3), ("continue", 2, 3),
             ("start", 3, 3), ("finish", 3, 3)],
            [(kind, index, total)
             for kind, index, total, _, _ in progress])
        rendered = [
            MODULE.render_run_progress(*values) for values in progress]
        self.assertTrue(rendered[0].startswith("[1/3] 开始处理"))
        self.assertTrue(any(line.startswith("[2/3] 开始处理") for line in rendered))
        self.assertTrue(any(line.startswith("[3/3] 开始处理") for line in rendered))
        events = MODULE._read_events(MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01"))
        started = {
            (event["chinese_post_id"], event["evidence"]["attempt"])
            for event in events if event["event_type"] == "run_attempt_started"}
        terminal = {
            (event["chinese_post_id"], event["evidence"]["attempt"])
            for event in events if event["event_type"] in {
                "run_attempt_completed", "run_attempt_failed"}
        }
        self.assertEqual(started, terminal)

    def test_run_ready_cli_progress_prints_with_flush(self):
        item = {
            "batch_id": "batch", "post_id": 401, "english_post_id": 1401,
            "allowed": True, "blocking_reasons": [],
        }
        completed = {
            **item, "result": "completed", "category": "completed",
            "returncode": 0, "error": "",
        }
        result = {
            "schema_version": 1, "mode": "execute",
            "repository_root": str(self.root), "selected_count": 1,
            "results": [completed], "writes_performed": True,
            "integrity_ok": True,
        }

        def fake_run(root, execute, batch_id, post_id, progress):
            self.assertIsNone(post_id)
            progress("start", 1, 1, item, None)
            progress("finish", 1, 1, item, completed)
            return result

        with mock.patch.object(MODULE, "run_ready", side_effect=fake_run), \
                mock.patch("builtins.print") as printer:
            self.assertEqual(0, MODULE.main([
                "run-ready", "--execute", "--repo-root", str(self.root)]))
        progress_calls = [
            call for call in printer.call_args_list
            if call.kwargs.get("flush") is True]
        self.assertEqual(2, len(progress_calls))
        self.assertIn("[1/1] 开始处理", progress_calls[0].args[0])
        self.assertIn("[1/1] 处理完成", progress_calls[1].args[0])

    def test_run_ready_cli_passes_explicit_post_id(self):
        expected = {
            "schema_version": 1, "mode": "preview",
            "repository_root": str(self.root), "selected_count": 0,
            "allowed_count": 0, "items": [], "writes_performed": False,
            "integrity_ok": True,
        }
        with mock.patch.object(MODULE, "run_ready", return_value=expected) as run:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(MODULE.EXIT_OK, MODULE.main([
                    "run-ready", "--post-id", "401", "--json",
                    "--repo-root", str(self.root)]))
        run.assert_called_once_with(
            self.root, execute=False, batch_id=None, post_id=401,
            progress=None)

    def test_run_ready_post_id_filters_without_changing_batch_mode(self):
        self.prepare_converted()
        MODULE.mark_converted(self.root, 402, 1, 1, True)
        for post_id in (401, 402):
            path = self.write_record_validation(
                chinese=post_id, english=post_id + 1000)
            MODULE.record_validation(
                self.root, post_id, str(path.relative_to(self.root)))
            self.create_execution_manifest(post_id)
        batch = MODULE.run_ready(self.root)
        selected = MODULE.run_ready(self.root, post_id=402)
        missing = MODULE.run_ready(self.root, post_id=499)
        self.assertEqual(2, batch["selected_count"])
        self.assertEqual([401, 402], [item["post_id"] for item in batch["items"]])
        self.assertEqual(1, selected["selected_count"])
        self.assertEqual(402, selected["items"][0]["post_id"])
        self.assertEqual(0, missing["selected_count"])
        self.assertEqual([], missing["items"])

    def test_run_ready_whole_article_retry_run_then_success(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        executions = 0
        waits = []
        progress = []

        def runner(command, **kwargs):
            nonlocal executions
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            executions += 1
            status = "prepared" if executions == 1 else "completed"
            self.write_execution(401, 1401, status)
            return mock.Mock(
                returncode=1 if executions == 1 else 0,
                stdout="", stderr="HTTP Error 400")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner, sleeper=waits.append,
            progress=lambda *values: progress.append(values))
        self.assertEqual("completed", result["results"][0]["result"])
        self.assertEqual(2, result["results"][0]["attempts"])
        self.assertEqual([5], waits)
        self.assertEqual(2, executions)
        self.assertEqual(1, result["completed_count"])
        rendered = [MODULE.render_run_progress(*values) for values in progress]
        self.assertTrue(any("第 1/3 次失败" in line for line in rendered))
        self.assertTrue(any("第 2/3 次尝试" in line and "mode=run" in line
                            for line in rendered))

    def test_run_ready_retry_switches_to_resume(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        execution_commands = []

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            execution_commands.append(command)
            if len(execution_commands) == 1:
                self.write_execution(401, 1401, "translation_failed")
                return mock.Mock(
                    returncode=1, stdout="", stderr="translation failed")
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            sleeper=lambda seconds: None)
        self.assertEqual("completed", result["results"][0]["result"])
        self.assertNotIn("--resume", execution_commands[0])
        self.assertIn("--resume", execution_commands[1])

    def test_run_ready_does_not_retry_exhausted_excerpt_generation(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        executions = 0
        waits = []
        progress = []

        def runner(command, **kwargs):
            nonlocal executions
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            executions += 1
            execution_path = self.write_execution(401, 1401, "excerpt_rejected")
            value = json.loads(execution_path.read_text(encoding="utf-8"))
            value["excerpt_generation_attempts"] = 3
            MODULE._atomic_write_json(execution_path, value)
            return mock.Mock(
                returncode=2,
                stdout="",
                stderr=(
                    "ERROR: generated Chinese excerpt contains "
                    "forbidden markup or payload"
                ),
            )

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner, sleeper=waits.append,
            progress=lambda *values: progress.append(values))
        item = result["results"][0]
        self.assertEqual(1, executions)
        self.assertEqual([], waits)
        self.assertEqual(1, item["candidate_attempts"])
        self.assertEqual(3, item["excerpt_generation_attempts"])
        self.assertEqual("excerpt_rejected", item["result"])
        self.assertEqual(
            "rejected_excerpt_generation",
            item["category"],
        )
        rendered = [MODULE.render_run_progress(*values) for values in progress]
        self.assertFalse(any("第 2/3 次尝试" in line for line in rendered))
        self.assertTrue(any(
            "candidate_attempts=1 excerpt_generation_attempts=3" in line
            for line in rendered))

    def test_token_validation_failure_is_classified_and_not_retried(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        executions = []
        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            executions.append(command)
            execution = {
                "schema_version": 1, "chinese_post_id": 401,
                "english_post_id": 1401, "status": "translation_failed",
                "error_response": {
                    "code": "swq_full_article_token_validation_failed",
                    "message": "Protected token validation failed",
                    "data": {"extra_tokens": ["SWQINLINE1END"]},
                },
            }
            MODULE._atomic_write_json(MODULE._execution_path(self.root, 401), execution)
            return mock.Mock(returncode=1, stdout="", stderr="HTTP Error 500")
        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            sleeper=lambda seconds: self.fail("deterministic error must not wait"))
        item = result["results"][0]
        self.assertEqual("protected_token_validation_error", item["category"])
        self.assertEqual(1, item["attempts"])
        self.assertEqual(1, len(executions))
        self.assertIn("Protected token validation failed", item["error"])
        self.assertIn("execution.json", item["execution_evidence"])

    def test_excerpt_generated_with_saved_live_excerpt_retries_as_resume(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        execution_commands = []

        class Source:
            def get_post(self, post_id):
                return {
                    "id": post_id,
                    "excerpt": {"raw": "已经保存的摘要"},
                }

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            execution_commands.append(command)
            if len(execution_commands) == 1:
                self.write_execution(401, 1401, "excerpt_generated")
                return mock.Mock(
                    returncode=1, stdout="", stderr="RemoteDisconnected")
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            source_factory=lambda rows: Source(),
            sleeper=lambda seconds: None)
        self.assertEqual("completed", result["results"][0]["result"])
        self.assertNotIn("--resume", execution_commands[0])
        self.assertIn("--resume", execution_commands[1])

    def test_excerpt_generated_with_empty_live_excerpt_restarts_run(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        execution_commands = []

        class Source:
            def get_post(self, post_id):
                return {"id": post_id, "excerpt": {"raw": ""}}

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            execution_commands.append(command)
            if len(execution_commands) == 1:
                self.write_execution(401, 1401, "excerpt_generated")
                return mock.Mock(returncode=1, stdout="", stderr="timeout")
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            source_factory=lambda rows: Source(),
            sleeper=lambda seconds: None)
        self.assertEqual("completed", result["results"][0]["result"])
        self.assertEqual(2, len(execution_commands))
        self.assertTrue(all(
            "--resume" not in command for command in execution_commands))

    def test_excerpt_observation_timeout_stays_safely_retryable(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        execution_commands = []

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            execution_commands.append(command)
            self.write_execution(401, 1401, "excerpt_generated")
            return mock.Mock(returncode=1, stdout="", stderr="timeout")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            source_factory=mock.Mock(
                side_effect=MODULE.SafetyError(
                    "batch read-only SSH query timed out")),
            sleeper=lambda seconds: None)
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual("observation_failed", result["results"][0]["result"])
        self.assertEqual(
            "transient_network_error", result["results"][0]["category"])
        self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertEqual("observe", state["recovery"]["action"])
        self.assertEqual(1, len(execution_commands))
        self.assertTrue(MODULE.run_ready(self.root)["items"][0]["allowed"])

    def test_run_ready_stops_at_three_and_continues(self):
        self.prepare_converted()
        for post_id in (401, 402):
            if post_id == 402:
                MODULE.mark_converted(self.root, 402, 1, 1, True)
            path = self.write_record_validation(
                chinese=post_id, english=post_id + 1000)
            unique = path.with_name(f"retry-validation-{post_id}.csv")
            path.replace(unique)
            MODULE.record_validation(
                self.root, post_id, str(unique.relative_to(self.root)))
            self.create_execution_manifest(post_id)
        counts = {401: 0, 402: 0}
        waits = []

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            post_id = int(command[command.index("--post-id") + 1])
            counts[post_id] += 1
            status = "prepared" if post_id == 401 else "completed"
            self.write_execution(post_id, post_id + 1000, status)
            return mock.Mock(
                returncode=1 if post_id == 401 else 0,
                stdout="", stderr="failed")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner, sleeper=waits.append)
        self.assertEqual([3, 1], [counts[401], counts[402]])
        self.assertEqual([5, 5], waits)
        self.assertEqual(["prepared", "completed"],
                         [item["result"] for item in result["results"]])
        self.assertEqual(1, result["failed_count"])
        self.assertEqual(0, result["pending_count"])

    def test_run_ready_rejects_unchanged_execution_state(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["recovery"] = {
            "status": "applied", "stage": "run", "action": "restart",
        }
        execution_path = self.write_execution(401, 1401, "prepared")
        execution = MODULE._execution_details(
            self.root, {"chinese_post_id": 401, "english_post_id": 1401})
        state["recovery"]["execution_sha256"] = execution["sha256"]
        MODULE._atomic_write_json(state_path, state)

        def runner(command, **kwargs):
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner, max_attempts=1)
        item = result["results"][0]
        self.assertEqual("stale_execution_state", item["category"])
        self.assertEqual(0, item["returncode"])
        self.assertIn("did not update", item["error"])
        self.assertTrue(execution_path.is_file())

    def test_run_ready_failure_isolated_and_maps_excerpt_failure(self):
        self.prepare_converted()
        for post_id in (401, 402):
            if post_id == 402:
                MODULE.mark_converted(self.root, 402, 1, 1, True)
            path = self.write_record_validation(
                chinese=post_id, english=post_id + 1000)
            unique = path.with_name(f"validation-{post_id}.csv")
            path.replace(unique)
            MODULE.record_validation(
                self.root, post_id, str(unique.relative_to(self.root)))
            self.create_execution_manifest(post_id)

        def runner(command, **kwargs):
            post_id = int(command[command.index("--post-id") + 1])
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            status = "excerpt_rejected" if post_id == 401 else "completed"
            self.write_execution(post_id, post_id + 1000, status)
            return mock.Mock(returncode=2 if post_id == 401 else 0,
                             stdout="", stderr="")

        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        self.assertEqual(2, len(result["results"]))
        self.assertEqual(2, result["results"][0]["returncode"])
        self.assertNotEqual("-", result["results"][0]["category"])
        self.assertIn("error", result["results"][0])
        states = {
            post_id: json.loads(MODULE._state_path(
                self.root, "syntaxhighlighter-20260723-01", post_id
            ).read_text(encoding="utf-8"))["workflow_status"]
            for post_id in (401, 402)
        }
        self.assertEqual("excerpt_failed", states[401])
        self.assertEqual("completed", states[402])

    def test_reconcile_orphaned_run_intermediate_states_and_scope(self):
        self.prepare_converted()
        for post_id in (401, 402):
            if post_id == 402:
                MODULE.mark_converted(self.root, 402, 1, 1, True)
            path = self.write_record_validation(
                chinese=post_id, english=post_id + 1000)
            unique = path.with_name(f"reconcile-run-{post_id}.csv")
            path.replace(unique)
            MODULE.record_validation(
                self.root, post_id, str(unique.relative_to(self.root)))
            self.create_execution_manifest(post_id)
        other_before = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 402).read_bytes()

        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        MODULE._record_attempt_start(self.root, state, "run")
        self.write_execution(401, 1401, "prepared")
        execution = MODULE._execution_details(
            self.root, {"chinese_post_id": 401, "english_post_id": 1401})
        MODULE._apply_execution_state(
            self.root, state, execution, "legacy incomplete run")

        before = self.snapshot()
        preview = MODULE.reconcile_attempts(self.root, 401, stage="run")
        self.assertTrue(preview["eligible"])
        self.assertEqual("ready_for_execution",
                         preview["items"][0]["target_workflow_status"])
        self.assertEqual(before, self.snapshot())

        applied = MODULE.reconcile_attempts(
            self.root, 401, stage="run", apply=True)
        self.assertTrue(applied["changed"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertEqual(0, state["retry_counts"]["run"])
        preview = MODULE.run_ready(self.root)
        recovered = next(
            item for item in preview["items"] if item["post_id"] == 401)
        self.assertTrue(recovered["allowed"])
        self.assertEqual(other_before, MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 402).read_bytes())

    def test_reconcile_excerpt_generated_requires_observation_and_completed_skips(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        MODULE._record_attempt_start(self.root, state, "run")
        self.write_execution(401, 1401, "excerpt_generated")
        execution = MODULE._execution_details(
            self.root, {"chinese_post_id": 401, "english_post_id": 1401})
        MODULE._apply_execution_state(
            self.root, state, execution, "legacy incomplete run")

        blocked = MODULE.reconcile_attempts(self.root, 401, stage="run")
        self.assertFalse(blocked["eligible"])
        self.assertIn("explicit Chinese excerpt state",
                      ";".join(blocked["blocking_reasons"]))
        applied = MODULE.reconcile_attempts(
            self.root, 401, stage="run", chinese_excerpt_empty=False,
            apply=True)
        self.assertTrue(applied["changed"])
        self.assertEqual(
            "ready_for_translation_resume",
            json.loads(state_path.read_text(encoding="utf-8"))["workflow_status"])
        self.assertTrue(MODULE.resume(self.root, post_id=401)["items"][0]["allowed"])

        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "completed"
        MODULE._atomic_write_json(state_path, state)
        self.write_execution(401, 1401, "completed")
        before = self.snapshot()
        result = MODULE.reconcile_attempts(
            self.root, 401, stage="run", apply=True)
        self.assertFalse(result["eligible"])
        self.assertEqual(before, self.snapshot())

    def test_run_ready_timeout_blocks_one_and_continues(self):
        self.prepare_converted()
        for post_id in (401, 402):
            if post_id == 402:
                MODULE.mark_converted(self.root, 402, 1, 1, True)
            path = self.write_record_validation(
                chinese=post_id, english=post_id + 1000)
            unique = path.with_name(f"timeout-validation-{post_id}.csv")
            path.replace(unique)
            MODULE.record_validation(
                self.root, post_id, str(unique.relative_to(self.root)))
            self.create_execution_manifest(post_id)

        def runner(command, **kwargs):
            post_id = int(command[command.index("--post-id") + 1])
            if post_id == 401:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(402, 1402, "completed")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        self.assertEqual(["operation_error", "completed"],
                         [item["result"] for item in result["results"]])
        states = [
            json.loads(MODULE._state_path(
                self.root, "syntaxhighlighter-20260723-01", post_id
            ).read_text(encoding="utf-8"))["workflow_status"]
            for post_id in (401, 402)
        ]
        self.assertEqual(["ready_for_execution", "completed"], states)

    def test_sync_execution_preview_apply_and_identity_conflict(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        self.write_execution(401, 1401, "translation_failed")
        before = self.snapshot()
        preview = MODULE.sync_execution(self.root)
        self.assertEqual(1, preview["planned_count"])
        self.assertEqual(before, self.snapshot())
        applied = MODULE.sync_execution(self.root, apply=True)
        self.assertEqual(1, applied["changed_count"])
        again = MODULE.sync_execution(self.root, apply=True)
        self.assertEqual(0, again["changed_count"])
        self.write_execution(402, 9999, "completed")
        with self.assertRaisesRegex(MODULE.ReadError, "English post ID mismatch"):
            MODULE.sync_execution(self.root)

    def test_resume_preview_execute_and_retry_limit(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        self.write_execution(401, 1401, "translation_failed")
        MODULE.sync_execution(self.root, apply=True)
        self.create_execution_manifest(401)
        preview = MODULE.resume(self.root)
        self.assertEqual([401], [item["post_id"] for item in preview["items"]])
        self.assertTrue(preview["items"][0]["allowed"])

        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.assertIn("--execute", command)
            self.assertIn("--resume", command)
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        result = MODULE.resume(self.root, execute=True, runner=runner)
        self.assertEqual(2, len(commands))
        self.assertTrue(all("--resume" in command for command in commands))
        actual = [command for command in commands if "--execute" in command]
        self.assertEqual(1, len(actual))
        self.assertEqual("completed", result["results"][0]["category"])
        self.assertEqual(0, result["results"][0]["returncode"])
        self.assertEqual("", result["results"][0]["error"])
        rendered = MODULE.render_operation_text(result)
        self.assertNotIn("category=-", rendered)
        self.assertNotIn("returncode=-", rendered)
        self.assertNotIn("error=-", rendered)
        self.assertEqual([], MODULE.resume(self.root)["items"])

        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 402)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "translation_failed"
        state["retry_counts"] = {"resume": MODULE.MAX_RESUME_ATTEMPTS}
        MODULE._atomic_write_json(state_path, state)
        self.write_execution(402, 1402, "translation_failed")
        item = MODULE.resume(self.root, post_id=402)["items"][0]
        self.assertFalse(item["allowed"])
        self.assertIn("resume retry limit exhausted", item["blocking_reasons"])
        MODULE.resume(
            self.root, execute=True, post_id=402,
            runner=mock.Mock(side_effect=AssertionError("must not run")))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["workflow_status"])

    def test_resume_attempt_two_allowed_three_blocked_and_completed_excluded(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        state_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "translation_failed"
        state["retry_counts"] = {"resume": 2}
        MODULE._atomic_write_json(state_path, state)
        self.write_execution(401, 1401, "translation_failed")

        item = MODULE.resume(self.root, post_id=401)["items"][0]
        self.assertTrue(item["allowed"])
        self.assertEqual(2, item["attempts"])
        self.assertEqual(3, MODULE.MAX_RESUME_ATTEMPTS)
        self.assertEqual(2, MODULE.MAX_RUN_ATTEMPTS)

        state["retry_counts"]["resume"] = 3
        MODULE._atomic_write_json(state_path, state)
        item = MODULE.resume(self.root, post_id=401)["items"][0]
        self.assertFalse(item["allowed"])
        self.assertIn("resume retry limit exhausted", item["blocking_reasons"])

        state["workflow_status"] = "completed"
        MODULE._atomic_write_json(state_path, state)
        self.write_execution(401, 1401, "completed")
        self.assertEqual([], MODULE.resume(self.root, post_id=401)["items"])

    def prepare_translation_failed(self, post_id=401):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        self.write_execution(post_id, post_id + 1000, "translation_failed")
        MODULE.sync_execution(self.root, apply=True)
        self.create_execution_manifest(post_id)
        return MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", post_id)

    def resume_runner(self, execute):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            return execute(command, **kwargs)

        return calls, runner

    def test_resume_nonzero_uses_fresh_state_and_records_terminal_failure(self):
        state_path = self.prepare_translation_failed()
        calls, runner = self.resume_runner(lambda command, **kwargs: (
            self.write_execution(401, 1401, "translation_failed"),
            mock.Mock(returncode=2, stdout="", stderr="resume safety failure"),
        )[1])
        result = MODULE.resume(self.root, execute=True, post_id=401, runner=runner)
        item = result["results"][0]
        self.assertEqual(2, len(calls))
        self.assertEqual("translation_failed", item["result"])
        self.assertEqual("executor_failed_with_state", item["category"])
        self.assertEqual(2, item["returncode"])
        self.assertIn("resume safety failure", item["error"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("translation_failed", state["workflow_status"])
        events = MODULE._read_events(MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01"))
        terminal = [event for event in events
                    if event["event_type"] == "resume_attempt_failed"]
        self.assertEqual([1], [event["evidence"]["attempt"] for event in terminal])

    def test_resume_stale_execution_cannot_impersonate_current_result(self):
        state_path = self.prepare_translation_failed()
        calls, runner = self.resume_runner(
            lambda command, **kwargs:
                mock.Mock(returncode=0, stdout='{"status":"completed"}', stderr=""))
        result = MODULE.resume(self.root, execute=True, post_id=401, runner=runner)
        item = result["results"][0]
        self.assertEqual(2, len(calls))
        self.assertEqual("blocked", item["result"])
        self.assertEqual("stale_execution_state", item["category"])
        self.assertEqual(0, item["returncode"])
        self.assertIn("did not update", item["error"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["workflow_status"])

    def test_resume_preflight_failure_records_terminal_event(self):
        state_path = self.prepare_translation_failed()
        runner = mock.Mock(return_value=mock.Mock(
            returncode=1, stdout="", stderr="preflight rejected"))
        result = MODULE.resume(self.root, execute=True, post_id=401, runner=runner)
        item = result["results"][0]
        runner.assert_called_once()
        self.assertEqual("preflight_failed", item["category"])
        self.assertEqual(1, item["returncode"])
        self.assertIn("preflight rejected", item["error"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["workflow_status"])
        events = MODULE._read_events(MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01"))
        self.assertEqual(1, sum(
            event["event_type"] == "resume_attempt_failed" for event in events))

    def test_resume_subprocess_exception_records_terminal_event(self):
        state_path = self.prepare_translation_failed()
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="", stderr="")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        result = MODULE.resume(self.root, execute=True, post_id=401, runner=runner)
        item = result["results"][0]
        self.assertEqual(2, len(calls))
        self.assertEqual("transient_network_error", item["category"])
        self.assertEqual(-1, item["returncode"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["workflow_status"])
        events = MODULE._read_events(MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01"))
        started = [event["evidence"]["attempt"] for event in events
                   if event["event_type"] == "resume_attempt_started"]
        failed = [event["evidence"]["attempt"] for event in events
                  if event["event_type"] == "resume_attempt_failed"]
        self.assertEqual(started, failed)

    def test_reconcile_orphaned_resume_attempts_preview_apply_and_scope(self):
        state_path = self.prepare_translation_failed()
        other_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 402)
        other = json.loads(other_path.read_text(encoding="utf-8"))
        other["workflow_status"] = "completed"
        MODULE._atomic_write_json(other_path, other)
        other_before = other_path.read_bytes()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for _ in range(3):
            MODULE._record_attempt_start(self.root, state, "resume")
            state["workflow_status"] = "translation_failed"
            MODULE._atomic_write_json(state_path, state)

        before = self.snapshot()
        preview = MODULE.reconcile_attempts(self.root, 401)
        self.assertEqual([1, 2, 3], preview["items"][0]["orphaned_attempts"])
        self.assertEqual(3, preview["items"][0]["current_resume_count"])
        self.assertEqual(0, preview["items"][0]["corrected_resume_count"])
        self.assertEqual(before, self.snapshot())

        applied = MODULE.reconcile_attempts(self.root, 401, apply=True)
        self.assertTrue(applied["changed"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(0, state["retry_counts"]["resume"])
        self.assertEqual(other_before, other_path.read_bytes())
        events = MODULE._read_events(MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01"))
        audit = [event for event in events
                 if event["event_type"] == "resume_orphaned_attempts_reconciled"]
        self.assertEqual(1, len(audit))
        self.assertEqual([1, 2, 3], audit[0]["evidence"]["orphaned_attempts"])
        repeated = MODULE.reconcile_attempts(self.root, 401, apply=True)
        self.assertFalse(repeated["changed"])

        state = json.loads(state_path.read_text(encoding="utf-8"))
        next_attempt = MODULE._record_attempt_start(self.root, state, "resume")
        self.assertEqual(4, next_attempt)
        self.assertEqual(1, state["retry_counts"]["resume"])

    def test_reconcile_preserves_terminated_failures_and_skips_completed(self):
        state_path = self.prepare_translation_failed()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        attempt = MODULE._record_attempt_start(self.root, state, "resume")
        failure = {
            "category": "preflight_failed", "returncode": 1,
            "stderr_summary": "rejected", "stdout_summary": "",
        }
        MODULE._block_after_operation_error(
            self.root, state, "resume", attempt, failure)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "translation_failed"
        MODULE._atomic_write_json(state_path, state)
        MODULE._record_attempt_start(self.root, state, "resume")
        state["workflow_status"] = "translation_failed"
        MODULE._atomic_write_json(state_path, state)

        applied = MODULE.reconcile_attempts(self.root, 401, apply=True)
        self.assertTrue(applied["changed"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(1, state["retry_counts"]["resume"])

        state["workflow_status"] = "completed"
        MODULE._atomic_write_json(state_path, state)
        self.write_execution(401, 1401, "completed")
        before = self.snapshot()
        result = MODULE.reconcile_attempts(self.root, 401, apply=True)
        self.assertFalse(result["eligible"])
        self.assertIn("completed article", result["blocking_reasons"][0])
        self.assertEqual(before, self.snapshot())

    def test_reconcile_resume_attempts_is_scoped_to_recovery_generation(self):
        state_path = self.prepare_translation_failed()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        terminal = {
            "result": "translation_failed", "category": "completed",
            "returncode": 0, "error": "",
        }

        for _ in range(3):
            attempt = MODULE._record_attempt_start(self.root, state, "resume")
            MODULE._record_attempt_outcome(
                self.root, state, "resume", attempt, terminal)
            state["workflow_status"] = "translation_failed"
            MODULE._atomic_write_json(state_path, state)

        state["recovery_generation"] = 1
        state["lifetime_retry_counts"] = {"resume": 3}
        state["retry_counts"] = {"run": 0, "resume": 0}
        state["workflow_status"] = "ready_for_translation_resume"
        restart_event = MODULE._transition_event(
            "failed_execution_restarted", state, "translation_failed",
            "ready_for_execution", "source changed", {"recovery_generation": 1},
            "2026-08-08T00:00:00+00:00", "restart-current|1")
        MODULE._persist_transition(self.root, state_path, state, restart_event)

        terminal_attempt = MODULE._record_attempt_start(self.root, state, "resume")
        MODULE._record_attempt_outcome(
            self.root, state, "resume", terminal_attempt, terminal)
        state["workflow_status"] = "ready_for_translation_resume"
        MODULE._atomic_write_json(state_path, state)
        orphaned_attempt = MODULE._record_attempt_start(self.root, state, "resume")

        self.assertEqual(4, terminal_attempt)
        self.assertEqual(5, orphaned_attempt)
        preview = MODULE.reconcile_attempts(self.root, 401, stage="resume")
        item = preview["items"][0]
        self.assertEqual([5], item["orphaned_attempts"])
        self.assertEqual([4], item["terminated_attempts"])
        self.assertEqual(2, item["current_resume_count"])
        self.assertEqual(1, item["corrected_resume_count"])

        applied = MODULE.reconcile_attempts(
            self.root, 401, stage="resume", apply=True)
        self.assertTrue(applied["changed"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(1, state["retry_counts"]["resume"])
        self.assertEqual(3, state["lifetime_retry_counts"]["resume"])

        self.write_execution(401, 1401, "translation_started")
        MODULE.sync_execution(self.root, apply=True)
        resume = MODULE.resume(self.root, post_id=401)
        self.assertEqual(1, resume["selected_count"])
        self.assertEqual(1, resume["allowed_count"])
        self.assertNotIn("resume retry limit exhausted",
                         resume["items"][0]["blocking_reasons"])

    def test_reconcile_corrects_stale_resume_count_without_current_orphans(self):
        state_path = self.prepare_translation_failed()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({
            "recovery_generation": 1,
            "lifetime_retry_counts": {"resume": 3},
            "retry_counts": {"run": 0, "resume": 3},
            "workflow_status": "ready_for_translation_resume",
        })
        restart_event = MODULE._transition_event(
            "failed_execution_restarted", state, "translation_failed",
            "ready_for_execution", "source changed", {"recovery_generation": 1},
            "2026-08-08T00:00:00+00:00", "restart-current|1")
        MODULE._persist_transition(self.root, state_path, state, restart_event)

        preview = MODULE.reconcile_attempts(self.root, 401, stage="resume")
        item = preview["items"][0]
        self.assertTrue(preview["eligible"])
        self.assertEqual(1, preview["planned_count"])
        self.assertEqual([], item["orphaned_attempts"])
        self.assertEqual([], item["terminated_attempts"])
        self.assertEqual(3, item["current_resume_count"])
        self.assertEqual(0, item["corrected_resume_count"])
        self.assertTrue(item["counter_drift"])
        self.assertEqual("counter_drift_correction", item["reconciliation_action"])

        applied = MODULE.reconcile_attempts(
            self.root, 401, stage="resume", apply=True)
        self.assertTrue(applied["changed"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(0, state["retry_counts"]["resume"])
        self.assertEqual(3, state["lifetime_retry_counts"]["resume"])
        self.assertEqual(1, state["recovery_generation"])

        self.write_execution(401, 1401, "translation_started")
        MODULE.sync_execution(self.root, apply=True)
        resume = MODULE.resume(self.root, post_id=401)
        self.assertEqual(1, resume["allowed_count"])
        self.assertNotIn("resume retry limit exhausted",
                         resume["items"][0]["blocking_reasons"])

    def test_new_json_commands_are_valid_and_read_only(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        before = self.snapshot()
        for command in ("run-ready", "resume", "sync-execution"):
            output = io.StringIO()
            with redirect_stdout(output):
                code = MODULE.main([
                    command, "--json", "--repo-root", str(self.root)])
            self.assertEqual(MODULE.EXIT_OK, code)
            json.loads(output.getvalue())
        self.assertEqual(before, self.snapshot())

    def test_validate_live_json_output_is_valid(self):
        expected = {
            "schema_version": 1, "mode": "already-recorded",
            "workflow_status": "ready_for_execution",
            "integrity_ok": True,
        }
        output = io.StringIO()
        with mock.patch.object(MODULE, "validate_live", return_value=expected):
            with redirect_stdout(output):
                code = MODULE.main([
                    "validate-live", "--post-id", "401", "--json",
                    "--repo-root", str(self.root)])
        self.assertEqual(MODULE.EXIT_OK, code)
        self.assertEqual(expected, json.loads(output.getvalue()))

    def test_validate_live_cli_passes_refresh_flag(self):
        output = io.StringIO()
        with mock.patch.object(
                MODULE, "validate_live",
                return_value={
                    "schema_version": 1, "mode": "live-readonly",
                    "workflow_status": "ready_for_execution",
                    "integrity_ok": True,
                }
        ) as validate:
            with redirect_stdout(output):
                code = MODULE.main([
                    "validate-live", "--post-id", "401", "--refresh", "--json",
                    "--repo-root", str(self.root)])
        self.assertEqual(MODULE.EXIT_OK, code)
        validate.assert_called_once_with(self.root, 401, refresh=True)

    def test_script_entrypoint_can_import_repository_modules(self):
        completed = subprocess.run(
            ["python3", str(SCRIPT), "--help"],
            cwd=self.root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(
            0, completed.returncode, completed.stderr + completed.stdout)
        self.assertIn("validate-live", completed.stdout)

    def test_validate_and_run_lock_conflict(self):
        self.prepare_converted()
        with MODULE.InitLock(self.root):
            with self.assertRaisesRegex(MODULE.ReadError, "lock is already held"):
                MODULE.validate_live(self.root, 401)
            with self.assertRaisesRegex(MODULE.ReadError, "lock is already held"):
                MODULE.run_ready(self.root, execute=True)

    def test_preflight_failure_is_observable_safe_and_does_not_start_attempt(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            state = json.loads(MODULE._state_path(
                self.root, "syntaxhighlighter-20260723-01", 401
            ).read_text(encoding="utf-8"))
            self.assertEqual("ready_for_execution", state["workflow_status"])
            return mock.Mock(
                returncode=1, stdout="diagnostic token=visible-secret",
                stderr=(
                    "HttpJsonError: network request failed: URLError "
                    "SSLEOFError UNEXPECTED_EOF_WHILE_READING "
                    "Authorization: Bearer abc Cookie=session-value"))

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner, max_attempts=1)
        item = result["results"][0]
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual(1, len(calls))
        self.assertIn("--preflight-live", calls[0])
        self.assertEqual("operation_error", item["result"])
        self.assertEqual("transient_network_error", item["category"])
        self.assertNotIn("abc", item["stderr_summary"])
        self.assertNotIn("visible-secret", item["stdout_summary"])
        self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertEqual({}, state["retry_counts"])

    def test_deterministic_preflight_failure_is_not_retried(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=1, stdout="", stderr="phase1_ineligible")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            sleeper=lambda seconds: self.fail("deterministic preflight must not retry"))
        self.assertEqual(1, len(calls))
        self.assertEqual("preflight_failed", result["results"][0]["category"])
        self.assertEqual(1, result["results"][0]["attempts"])

    def test_nested_readonly_255_stops_batch_without_outer_retry_or_state_write(self):
        self.prepare_converted()
        MODULE.mark_converted(self.root, 402, 1, 1, True)
        for post_id in (401, 402):
            path = self.write_record_validation(
                chinese=post_id, english=post_id + 1000)
            unique = path.with_name(f"readonly-{post_id}.csv")
            path.replace(unique)
            MODULE.record_validation(
                self.root, post_id, str(unique.relative_to(self.root)))
            self.create_execution_manifest(post_id)
        calls = []
        nested = {"preflight_passed": False, "request_counts": {
            "wordpress_get": 2, "ssh_readonly": 1, "post": 0,
            "glm": 0, "translation": 0}, "polylang_check": {
                "error": "read-only Polylang SSH check exited with 255 after 2 attempts"}}
        def runner(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=1, stdout=json.dumps(nested), stderr="")
        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            sleeper=lambda seconds: self.fail("outer retry is forbidden"))
        self.assertEqual(1, len(calls))
        self.assertEqual("production_readonly_unavailable",
                         result["results"][0]["category"])
        self.assertEqual(1, result["processed_count"])
        self.assertEqual(1, result["pending_count"])
        self.assertTrue(result["stopped_early"])
        self.assertFalse(result["integrity_ok"])
        for post_id in (401, 402):
            state = json.loads(MODULE._state_path(
                self.root, "syntaxhighlighter-20260723-01", post_id
            ).read_text(encoding="utf-8"))
            self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertFalse(result["writes_performed"])

    def test_direct_readonly_255_after_attempt_recovers_ready_without_blocking(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            return mock.Mock(returncode=2, stdout="", stderr=
                "ERROR: read-only Polylang SSH check exited with 255 after 2 attempts")
        result = MODULE.run_ready(
            self.root, execute=True, runner=runner,
            sleeper=lambda seconds: self.fail("outer retry is forbidden"))
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual(2, len(calls))
        self.assertEqual("production_readonly_unavailable",
                         result["results"][0]["category"])
        self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertFalse(MODULE._execution_path(self.root, 401).exists())
        self.assertFalse(MODULE._prewrite_path(self.root, 401).exists())

    def test_keyboard_interrupt_before_write_recovers_ready_and_stops(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            raise KeyboardInterrupt()
        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual("execution_interrupted",
                         result["results"][0]["category"])
        self.assertTrue(result["results"][0]["recovered_to_ready"])
        self.assertEqual("ready_for_execution", state["workflow_status"])

    def test_keyboard_interrupt_with_artifact_preserves_in_progress_evidence(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            MODULE._prewrite_path(self.root, 401).write_text("{}", encoding="utf-8")
            raise KeyboardInterrupt()
        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertFalse(result["results"][0]["recovered_to_ready"])
        self.assertEqual("execution_in_progress", state["workflow_status"])
        self.assertEqual("execution_interrupted_write_status_unknown",
                         state["last_failure"]["reason"])

    def test_non_network_preflight_exit_is_not_transient(self):
        failure = MODULE._classify_subprocess_failure(
            mock.Mock(returncode=1, stderr="manifest rejected", stdout=""),
            "preflight")
        self.assertEqual("preflight_failed", failure["category"])
        auth = MODULE._classify_subprocess_failure(
            mock.Mock(returncode=1, stderr="HTTP Error 401: Unauthorized", stdout=""),
            "preflight")
        self.assertEqual("authentication_error", auth["category"])

    def test_http_400_is_a_client_error_not_a_transient_network_error(self):
        failure = MODULE._classify_subprocess_failure(
            mock.Mock(returncode=1, stderr=
                      "HttpJsonError: HTTP request failed with status 400",
                      stdout=""), "execute")
        self.assertEqual("http_client_error", failure["category"])

    def test_excerpt_validation_forbidden_text_is_not_authentication_error(self):
        failure = MODULE._classify_subprocess_failure(
            mock.Mock(
                returncode=2,
                stderr=(
                    "ERROR: generated Chinese excerpt contains "
                    "forbidden markup or payload"
                ),
                stdout="",
            ),
            "execute",
        )
        self.assertNotEqual("authentication_error", failure["category"])

    def test_execute_transient_without_artifacts_returns_ready_and_keeps_attempt(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            return mock.Mock(
                returncode=1, stdout="",
                stderr="URLError: network request failed SSLEOFError")

        result = MODULE.run_ready(
            self.root, execute=True, runner=runner, max_attempts=1)
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual(2, len(calls))
        self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertEqual(1, state["retry_counts"]["run"])
        self.assertTrue(result["results"][0]["recovered_to_ready"])

    def test_execute_nontransient_or_backup_blocks(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            backup = (self.root / "data/backups/single-candidate/"
                      "chinese-401.pre-write.json")
            backup.write_text("{}", encoding="utf-8")
            return mock.Mock(
                returncode=1, stdout="", stderr="URLError network request failed")

        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["workflow_status"])
        self.assertIn("pre-write", result["results"][0]["artifacts"][0])

    def test_invalid_execution_state_has_distinct_classification(self):
        self.prepare_converted()
        path = self.write_record_validation()
        MODULE.record_validation(
            self.root, 401, str(path.relative_to(self.root)))
        self.create_execution_manifest()

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(401, 1401, raw="{bad")
            return mock.Mock(returncode=1, stdout="", stderr="executor failed")

        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        self.assertEqual(
            "executor_state_invalid", result["results"][0]["category"])
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["workflow_status"])

    def test_recover_blocked_preview_apply_and_idempotency(self):
        state_path = self.prepare_blocked_run()
        before = self.snapshot()
        preview = MODULE.recover_blocked(self.root, 401)
        self.assertTrue(preview["eligible"])
        self.assertFalse(preview["changed"])
        self.assertEqual(before, self.snapshot())
        applied = MODULE.recover_blocked(self.root, 401, apply=True)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        events_before = MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01").read_bytes()
        repeated = MODULE.recover_blocked(self.root, 401, apply=True)
        self.assertTrue(applied["changed"])
        self.assertFalse(repeated["changed"])
        self.assertTrue(repeated["already_recovered"])
        self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertEqual(1, state["retry_counts"]["run"])
        self.assertIn("last_failure", state)
        self.assertEqual(events_before, MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01").read_bytes())

    def test_recover_blocked_rejects_artifacts_validation_drift_and_limit(self):
        state_path = self.prepare_blocked_run()
        backup = (self.root / "data/backups/single-candidate/"
                  "chinese-401.pre-write.json")
        backup.write_text("{}", encoding="utf-8")
        self.assertFalse(MODULE.recover_blocked(self.root, 401)["eligible"])
        backup.unlink()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["retry_counts"]["run"] = MODULE.MAX_RUN_ATTEMPTS
        MODULE._atomic_write_json(state_path, state)
        self.assertIn(
            "run retry limit exhausted",
            MODULE.recover_blocked(self.root, 401)["blocking_reasons"])
        state["retry_counts"]["run"] = 1
        MODULE._atomic_write_json(state_path, state)
        validation = self.root / state["validation_evidence"]["source_file"]
        validation.write_text("drift", encoding="utf-8")
        self.assertTrue(any(
            "SHA-256 drift" in reason
            for reason in MODULE.recover_blocked(
                self.root, 401)["blocking_reasons"]))

    def test_recover_blocked_rejects_execution_and_completed(self):
        state_path = self.prepare_blocked_run()
        self.write_execution(401, 1401, "completed")
        self.assertFalse(MODULE.recover_blocked(self.root, 401)["eligible"])
        self.write_execution(401, 1401, raw=None)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "completed"
        MODULE._atomic_write_json(state_path, state)
        self.assertFalse(MODULE.recover_blocked(self.root, 401)["eligible"])

    def test_recover_blocked_json_is_valid(self):
        self.prepare_blocked_run()
        output = io.StringIO()
        with redirect_stdout(output):
            code = MODULE.main([
                "recover-blocked", "--post-id", "401", "--json",
                "--repo-root", str(self.root)])
        self.assertEqual(MODULE.EXIT_OK, code)
        self.assertTrue(json.loads(output.getvalue())["eligible"])

    def restart_source(self, *, english_title="English", english_excerpt="",
                       english_content="English body", excerpt="",
                       chinese_title="人工修复标题", chinese_content=None,
                       chinese_id=401, english_id=1401):
        from src.candidate_execution import sha256_text
        content = chinese_content or (
            '<!-- wp:kevinbatdorf/code-block-pro -->'
            '<div class="wp-block-kevinbatdorf-code-block-pro"><textarea>x</textarea>'
            '<pre class="shiki"><code><span class="line">changed code</span>'
            '</code></pre></div>'
            '<!-- /wp:kevinbatdorf/code-block-pro -->')

        class Source:
            def get_post(self, post_id):
                if int(post_id) == chinese_id:
                    return {"id": chinese_id, "status": "publish",
                            "title": {"raw": chinese_title},
                            "excerpt": {"raw": excerpt},
                            "content": {"raw": content}}
                return {"id": english_id, "status": "publish",
                        "title": {"raw": english_title},
                        "excerpt": {"raw": english_excerpt},
                        "content": {"raw": english_content}}

            def check(self, chinese, english):
                return {"chinese_post_id": chinese_id, "chinese_language": "zh",
                        "linked_english_post_id": english_id,
                        "english_post_id": english_id, "english_language": "en",
                        "linked_chinese_post_id": chinese_id}

        return Source(), content, sha256_text

    def prepare_source_restart(self, excerpt=""):
        state_path = self.prepare_blocked_run()
        (self.root / "config/classification.json").write_bytes(
            (ROOT / "config/classification.json").read_bytes())
        execution_path = self.write_execution(401, 1401, "translation_failed")
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution.update({"generated_excerpt": "旧摘要", "error": "title failed"})
        MODULE._atomic_write_json(execution_path, execution)
        source, content, sha256_text = self.restart_source(excerpt=excerpt)
        prewrite = {
            "schema_version": 1, "chinese_post_id": 401,
            "english_post_id": 1401, "status": "prepared",
            "before": {"chinese_excerpt": "", "english_title": "English",
                       "english_excerpt": "", "english_content": "English body"},
            "sha256": {"chinese_title": sha256_text("标题 401"),
                       "chinese_content": "b" * 64,
                       "chinese_excerpt": sha256_text(""),
                       "english_title": sha256_text("English"),
                       "english_excerpt": sha256_text(""),
                       "english_content": sha256_text("English body")},
        }
        MODULE._atomic_write_json(MODULE._prewrite_path(self.root, 401), prewrite)
        return state_path, source, content

    def prepare_blocked_mixed_translation_failure(self, with_validation=True):
        from src.candidate_execution import sha256_text

        post_id, english_id = 500, 1500
        content = "<!-- wp:paragraph --><p>正文</p><!-- /wp:paragraph -->"
        self.prepare_init_fixture()
        batch_path, batch_id = self.write_mixed_batch(
            [(post_id, english_id)], suffix="20260812-02")
        with batch_path.open(encoding="utf-8", newline="") as handle:
            batch_rows = list(csv.DictReader(handle))
        batch_rows[0]["expected_code_block_pro_count_after"] = "0"
        batch_rows[0]["content_sha256"] = sha256_text(content)
        self.write_csv(batch_path, MIXED_FIELDS, batch_rows)
        MODULE.init_state(self.root, apply=True)
        (self.root / "config").mkdir()
        (self.root / "config/classification.json").write_bytes(
            (ROOT / "config/classification.json").read_bytes())

        state_path = MODULE._state_path(self.root, batch_id, post_id)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({
            "workflow_status": "blocked",
            "manual_conversion": {"status": "confirmed"},
            "gutenberg_normalization": {"status": "confirmed"},
            "language_review": {"status": "confirmed"},
            "retry_counts": {"run": 1, "resume": 0},
        })
        source, _, _ = self.restart_source(
            excerpt="旧摘要", chinese_content=content,
            chinese_id=post_id, english_id=english_id)
        execution_path = self.write_execution(
            post_id, english_id, "translation_failed")
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution.update({
            "generated_excerpt": "旧摘要",
            "error_response": {"code": "swq_field_translation_failed",
                                "message": "invalid translation assistant reply"},
        })
        MODULE._atomic_write_json(execution_path, execution)
        prewrite = {
            "schema_version": 1, "chinese_post_id": post_id,
            "english_post_id": english_id, "status": "prepared",
            "sha256": {
                "chinese_title": sha256_text("人工修复标题"),
                "chinese_content": sha256_text(content),
                "chinese_excerpt": sha256_text(""),
                "english_title": sha256_text("English"),
                "english_excerpt": sha256_text(""),
                "english_content": sha256_text("English body"),
            },
        }
        MODULE._atomic_write_json(
            MODULE._prewrite_path(self.root, post_id), prewrite)
        manifest_path = MODULE._validation_paths(
            self.root, batch_id, post_id)[2]
        manifest = {field: "" for field in MODULE.EXECUTION_MANIFEST_FIELDS}
        manifest.update({
            "chinese_post_id": post_id, "chinese_title": "人工修复标题",
            "chinese_content_sha256": sha256_text(content),
            "chinese_excerpt_empty": "True", "english_post_id": english_id,
            "english_post_status": "publish",
            "english_title_sha256": sha256_text("English"),
            "english_excerpt_sha256": sha256_text(""),
            "english_content_sha256": sha256_text("English body"),
            "candidate_reason": "validated mixed test",
            "execution_status": "pending", "expected_code_block_pro_count": "0",
            "expected_syntaxhighlighter_count": "0",
        })
        MODULE._atomic_write_csv(
            manifest_path, MODULE.EXECUTION_MANIFEST_FIELDS, [manifest])
        if with_validation:
            validation_path = self.root / "data/analysis" / "validated-500.csv"
            validation_path.write_text("validated", encoding="utf-8")
            state["validation_evidence"] = {
                "status": "ready", "failure_reasons": [],
                "source_file": str(validation_path.relative_to(self.root)),
                "sha256": MODULE._file_sha256(validation_path),
            }
        MODULE._atomic_write_json(state_path, state)
        return state_path, source, batch_id, post_id

    def prepare_rejected_restart(self, *, excerpt="", missing_rejected=False):
        state_path, source, content = self.prepare_source_restart(excerpt=excerpt)
        from src.candidate_execution import sha256_text
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "excerpt_failed"
        MODULE._atomic_write_json(state_path, state)
        prewrite_path = MODULE._prewrite_path(self.root, 401)
        prewrite = json.loads(prewrite_path.read_text(encoding="utf-8"))
        prewrite["sha256"].update({
            "chinese_title": sha256_text("人工修复标题"),
            "chinese_content": sha256_text(content),
        })
        MODULE._atomic_write_json(prewrite_path, prewrite)
        rejected_dir = self.root / "data/backups/single-candidate/rejected"
        rejected_dir.mkdir()
        rejected_paths = []
        for attempt in range(1, 4):
            path = rejected_dir / f"chinese-401-glm47-rejected-attempt-{attempt}.txt"
            if not (missing_rejected and attempt == 3):
                path.write_text(
                    f"第 {attempt} 次摘要包含 SQLSTATE[HY000]，此前被错误识别为 shortcode。",
                    encoding="utf-8")
            rejected_paths.append(str(path))
        execution_path = MODULE._execution_path(self.root, 401)
        MODULE._atomic_write_json(execution_path, {
            "status": "excerpt_rejected", "chinese_post_id": 401,
            "english_post_id": 1401, "attempts": 3,
            "error": "generated Chinese excerpt contains HTML or shortcode markup",
            "rejected_excerpt_paths": rejected_paths,
        })
        return state_path, source, content, rejected_paths

    def test_restart_from_current_preview_is_read_only(self):
        _, source, _ = self.prepare_source_restart()
        before = self.snapshot()
        result = MODULE.restart_from_current(
            self.root, 401, source_factory=lambda rows: source)
        self.assertTrue(result["eligible"], result["blocking_reasons"])
        self.assertFalse(result["writes_performed"])
        self.assertEqual(before, self.snapshot())

    def test_recover_selects_restart_for_changed_source(self):
        _, source, _ = self.prepare_source_restart()
        before = self.snapshot()
        queries = []
        result = MODULE.recover(
            self.root, 401,
            source_factory=lambda rows: queries.append(rows) or source)
        self.assertEqual("restart_from_current", result["strategy"])
        self.assertEqual("changed", result["production_source"])
        self.assertEqual(1, len(queries))
        self.assertFalse(result["writes_performed"])
        self.assertEqual(before, self.snapshot())

    def test_recover_selects_resume_for_unchanged_source(self):
        state_path, source, content = self.prepare_source_restart()
        from src.candidate_execution import sha256_text
        prewrite_path = MODULE._prewrite_path(self.root, 401)
        prewrite = json.loads(prewrite_path.read_text(encoding="utf-8"))
        prewrite["sha256"].update({
            "chinese_title": sha256_text("人工修复标题"),
            "chinese_content": sha256_text(content),
        })
        MODULE._atomic_write_json(prewrite_path, prewrite)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "translation_failed"
        MODULE._atomic_write_json(state_path, state)
        before = self.snapshot()
        result = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("resume", result["strategy"])
        self.assertEqual("unchanged", result["production_source"])
        self.assertEqual(before, self.snapshot())

    def test_recover_completed_preview_skips_production_and_is_noop(self):
        state_path, _, _ = self.prepare_source_restart()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "completed"
        MODULE._atomic_write_json(state_path, state)
        before = self.snapshot()
        result = MODULE.recover(
            self.root, 401,
            source_factory=mock.Mock(side_effect=AssertionError(
                "completed recovery must not query production")))
        self.assertEqual("none", result["strategy"])
        self.assertNotEqual("blocked", result["strategy"])
        self.assertEqual("none", result["actual_error"])
        self.assertEqual("not_required", result["production_source"])
        self.assertFalse(result["writes_performed"])
        self.assertEqual(before, self.snapshot())

    def test_recover_reclassifies_blocked_translation_failure_as_resume(self):
        state_path, source, content = self.prepare_source_restart()
        from src.candidate_execution import sha256_text
        prewrite_path = MODULE._prewrite_path(self.root, 401)
        prewrite = json.loads(prewrite_path.read_text(encoding="utf-8"))
        prewrite["sha256"].update({
            "chinese_title": sha256_text("人工修复标题"),
            "chinese_content": sha256_text(content),
        })
        MODULE._atomic_write_json(prewrite_path, prewrite)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["workflow_status"])
        before = self.snapshot()
        result = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("resume", result["strategy"])
        self.assertEqual(
            "execution evidence is translation-failed and all recovery checks pass",
            result["strategy_reasons"][0])
        self.assertEqual("resume -> execute_single_candidate", result["will_execute"])
        self.assertFalse(result["writes_performed"])
        self.assertEqual(before, self.snapshot())

    def test_blocked_translation_failure_with_recovery_blocker_stays_blocked(self):
        state_path, source, content = self.prepare_source_restart()
        from src.candidate_execution import sha256_text
        prewrite_path = MODULE._prewrite_path(self.root, 401)
        prewrite = json.loads(prewrite_path.read_text(encoding="utf-8"))
        prewrite["sha256"].update({
            "chinese_title": sha256_text("人工修复标题"),
            "chinese_content": sha256_text(content),
        })
        MODULE._atomic_write_json(prewrite_path, prewrite)
        changed, _, _ = self.restart_source(english_content="外部修改的英文正文")
        result = MODULE.recover(
            self.root, 401, source_factory=lambda rows: changed)
        self.assertEqual("blocked", result["strategy"])
        self.assertIn("english_content_unchanged", result["strategy_reasons"])

    def test_recover_execute_passes_validated_mixed_flag_for_blocked_resume(self):
        state_path, source, batch_id, post_id = (
            self.prepare_blocked_mixed_translation_failure())
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(post_id, 1500, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")

        preview = MODULE.recover(
            self.root, post_id, source_factory=lambda rows: source)
        self.assertEqual("resume", preview["strategy"])
        result = MODULE.recover(
            self.root, post_id, execute=True,
            source_factory=lambda rows: source, runner=runner)
        self.assertEqual("completed", result["final_status"])
        self.assertEqual(2, len(calls))
        self.assertTrue(all("--resume" in command for command in calls))
        self.assertTrue(all("--special-validated-mixed" in command
                            for command in calls))
        self.assertEqual("completed", json.loads(state_path.read_text(
            encoding="utf-8"))["workflow_status"])
        self.assertEqual(batch_id, result["batch_id"])

    def test_recover_execute_does_not_bypass_missing_validation_evidence(self):
        state_path, source, _, post_id = self.prepare_blocked_mixed_translation_failure(
            with_validation=False)
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=1, stdout="", stderr="must not run")

        preview = MODULE.recover(
            self.root, post_id, source_factory=lambda rows: source)
        self.assertEqual("blocked", preview["strategy"])
        result = MODULE.recover(
            self.root, post_id, execute=True,
            source_factory=lambda rows: source, runner=runner)
        self.assertEqual("not_run", result["reexecution"])
        self.assertEqual([], calls)
        self.assertEqual("blocked", json.loads(state_path.read_text(
            encoding="utf-8"))["workflow_status"])

    def test_recover_execute_does_not_start_resume_when_live_drift_exists(self):
        state_path, source, _, post_id = (
            self.prepare_blocked_mixed_translation_failure())
        drifted, _, _ = self.restart_source(
            english_content="外部修改的英文正文", chinese_id=post_id,
            english_id=1500, chinese_content=
            "<!-- wp:paragraph --><p>正文</p><!-- /wp:paragraph -->")
        runner = mock.Mock(return_value=mock.Mock(
            returncode=1, stdout="", stderr="must not run"))
        result = MODULE.recover(
            self.root, post_id, execute=True,
            source_factory=lambda rows: drifted, runner=runner)
        self.assertEqual("blocked", result["strategy"])
        self.assertEqual("not_run", result["reexecution"])
        runner.assert_not_called()
        self.assertEqual("blocked", json.loads(state_path.read_text(
            encoding="utf-8"))["workflow_status"])

    def test_recover_execute_renders_child_failure_summary(self):
        _, source, _, post_id = self.prepare_blocked_mixed_translation_failure()
        runner = mock.Mock(return_value=mock.Mock(
            returncode=1, stdout="", stderr="preflight structure mismatch"))
        result = MODULE.recover(
            self.root, post_id, execute=True,
            source_factory=lambda rows: source, runner=runner)
        self.assertEqual("failed", result["reexecution"])
        self.assertIn(
            "子进程失败摘要: preflight structure mismatch",
            MODULE.render_recover_text(result))

    def test_recover_old_readonly_blocked_uses_retry_from_ready(self):
        state_path = self.prepare_blocked_run()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["last_failure"].update({
            "reason": "executor_failed_without_state",
            "stderr_summary":
                "read-only Polylang SSH check exited with 255 after 2 attempts"})
        MODULE._atomic_write_json(state_path, state)
        before = self.snapshot()
        preview = MODULE.recover(
            self.root, 401,
            source_factory=mock.Mock(side_effect=AssertionError(
                "pre-write readonly failure does not need production query")))
        self.assertEqual("retry_from_ready", preview["strategy"])
        self.assertEqual("production_readonly_unavailable",
                         preview["actual_error"])
        self.assertEqual(before, self.snapshot())
        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")
        executed = MODULE.recover(self.root, 401, execute=True, runner=runner)
        self.assertEqual("completed", executed["final_status"])
        self.assertEqual("completed", executed["reexecution"])

    def test_recover_execute_next_step_uses_new_excerpt_failed_status(self):
        state_path = self.prepare_blocked_run()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["last_failure"].update({
            "reason": "executor_failed_without_state",
            "stderr_summary":
                "read-only Polylang SSH check exited with 255 after 2 attempts"})
        MODULE._atomic_write_json(state_path, state)
        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(401, 1401, "excerpt_rejected")
            return mock.Mock(
                returncode=2, stdout="",
                stderr="generated Chinese excerpt contains Markdown or a list")
        result = MODULE.recover(self.root, 401, execute=True, runner=runner)
        output = MODULE.render_recover_text(result)
        next_line = next(line for line in output.splitlines()
                         if line.startswith("下一步:"))
        self.assertEqual("excerpt_failed", result["final_status"])
        self.assertIn("rejected excerpt evidence", next_line)
        self.assertNotIn("production_readonly_unavailable", next_line)

    def test_recover_orphaned_in_progress_without_artifacts_is_retry_from_ready(self):
        state_path = self.prepare_blocked_run()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "execution_in_progress"
        state["last_failure"] = None
        MODULE._atomic_write_json(state_path, state)
        before = self.snapshot()
        result = MODULE.recover(
            self.root, 401,
            source_factory=mock.Mock(side_effect=AssertionError(
                "artifact-free orphan does not need production query")))
        self.assertEqual("retry_from_ready", result["strategy"])
        self.assertEqual("execution_interrupted_before_write",
                         result["actual_error"])
        self.assertEqual(before, self.snapshot())

    def test_recover_completed_execute_skips_every_action_and_renders_noop(self):
        state_path, _, _ = self.prepare_source_restart()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "completed"
        MODULE._atomic_write_json(state_path, state)
        execution_before = MODULE._execution_path(self.root, 401).read_bytes()
        state_before = state_path.read_bytes()
        result = MODULE.recover(
            self.root, 401, execute=True,
            source_factory=mock.Mock(side_effect=AssertionError(
                "completed recovery must not query production")),
            runner=mock.Mock(side_effect=AssertionError(
                "completed recovery must not execute a candidate")))
        output = MODULE.render_recover_text(result)
        self.assertEqual("none", result["strategy"])
        self.assertEqual("not_run", result["baseline_rebuild"])
        self.assertEqual("not_run", result["reexecution"])
        self.assertFalse(result["writes_performed"])
        self.assertIn("真实错误: 无", output)
        self.assertIn("生产中文源: 无需检查", output)
        self.assertIn("恢复策略: none", output)
        self.assertIn("写入操作: 否", output)
        self.assertIn("下一步: 无", output)
        self.assertEqual(execution_before,
                         MODULE._execution_path(self.root, 401).read_bytes())
        self.assertEqual(state_before, state_path.read_bytes())

    def test_recover_restart_executes_only_selected_article_to_completion(self):
        state_path, source, _ = self.prepare_source_restart()
        other_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 402)
        other_before = other_path.read_bytes()
        execution_commands = []
        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            execution_commands.append(command)
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")
        result = MODULE.recover(
            self.root, 401, execute=True,
            source_factory=lambda rows: source, runner=runner)
        self.assertEqual("completed", result["baseline_rebuild"])
        self.assertEqual("completed", result["reexecution"])
        self.assertEqual("completed", result["final_status"])
        self.assertEqual(1, len(execution_commands))
        self.assertIn("401", execution_commands[0])
        self.assertEqual(other_before, other_path.read_bytes())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("completed", state["workflow_status"])

    def test_recover_reruns_prepared_current_restart_after_glm_timeout(self):
        """A rebuilt baseline may safely rerun when GLM failed before any write."""
        state_path, source, _ = self.prepare_source_restart()
        rebuilt = MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)
        self.assertTrue(rebuilt["changed"])

        def timeout_runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            execution_path = MODULE._execution_path(self.root, 401)
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            execution["started_at"] = "2026-08-13T00:00:01+00:00"
            MODULE._atomic_write_json(execution_path, execution)
            return mock.Mock(returncode=1, stdout="", stderr=(
                "HttpJsonError: network request failed: TimeoutError"))

        first = MODULE._run_ready_once(
            self.root, execute=True, batch_id="syntaxhighlighter-20260723-01",
            post_id=401, runner=timeout_runner)
        self.assertEqual("prepared", first["results"][0]["result"])
        self.assertEqual("transient_network_error", first["results"][0]["category"])
        blocked = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("blocked", blocked["workflow_status"])
        self.assertEqual("transient_network_error", blocked["last_failure"]["reason"])
        self.assertEqual("prepared", blocked["execution_evidence"]["status"])
        self.assertEqual(1, blocked["execution_evidence"]["recovery_generation"])
        self.assertEqual("applied", blocked["source_restart_recovery"]["status"])

        preview = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("retry_prepared_source_restart", preview["strategy"])

        calls = []
        def complete_runner(command, **kwargs):
            calls.append(command)
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")

        recovered = MODULE.recover(
            self.root, 401, execute=True, source_factory=lambda rows: source,
            runner=complete_runner)
        self.assertEqual("completed", recovered["final_status"])
        self.assertEqual("completed", recovered["reexecution"])
        self.assertTrue(all("--resume" not in command for command in calls))
        self.assertTrue(all("--recovery-restart" in command for command in calls))

    def test_recover_retries_persisted_excerpt_generation_failure(self):
        state_path, source, content = self.prepare_source_restart()
        from src.candidate_execution import sha256_text
        prewrite_path = MODULE._prewrite_path(self.root, 401)
        prewrite = json.loads(prewrite_path.read_text(encoding="utf-8"))
        prewrite["sha256"].update({
            "chinese_title": sha256_text("人工修复标题"),
            "chinese_content": sha256_text(content),
        })
        MODULE._atomic_write_json(prewrite_path, prewrite)
        execution_path = MODULE._execution_path(self.root, 401)
        MODULE._atomic_write_json(execution_path, {
            "schema_version": 1, "chinese_post_id": 401,
            "english_post_id": 1401, "status": "excerpt_generation_failed",
            "backup_path": str(prewrite_path), "started_at": "2026-08-14T00:00:00+00:00",
            "error": "HTTP request failed with status 400",
            "excerpt_generation_attempts": 1,
            "error_response": {"code": "invalid_request"},
        })
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({
            "workflow_status": "excerpt_failed",
            "execution_evidence": {
                "status": "excerpt_generation_failed",
                "sha256": MODULE._file_sha256(execution_path),
            },
            "last_failure": {"stage": "run", "reason": "http_client_error"},
        })
        MODULE._atomic_write_json(state_path, state)
        preview = MODULE.recover(self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("retry_excerpt_generation", preview["strategy"])
        self.assertEqual("glm_api_error", preview["actual_error"])

        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")

        recovered = MODULE.recover(
            self.root, 401, execute=True, source_factory=lambda rows: source,
            runner=runner)
        self.assertEqual("completed", recovered["final_status"])

    def test_recover_accepts_sha_bound_legacy_prepared_glm_http_400_failure(self):
        state_path, source, content = self.prepare_source_restart()
        from src.candidate_execution import sha256_text
        prewrite_path = MODULE._prewrite_path(self.root, 401)
        prewrite = json.loads(prewrite_path.read_text(encoding="utf-8"))
        prewrite["sha256"].update({
            "chinese_title": sha256_text("人工修复标题"),
            "chinese_content": sha256_text(content),
        })
        MODULE._atomic_write_json(prewrite_path, prewrite)
        execution_path = self.write_execution(401, 1401, "prepared")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({
            "workflow_status": "blocked",
            "execution_evidence": {
                "status": "prepared", "sha256": MODULE._file_sha256(execution_path),
            },
            "last_failure": {
                "stage": "run", "reason": "preflight_failed",
            },
            "recovery": {
                "status": "applied", "stage": "run",
                "action": "retry_excerpt_generation",
                "execution_sha256": MODULE._file_sha256(execution_path),
                "preserved_failure": {
                    "stage": "run", "reason": "transient_network_error",
                    "stderr_summary": "HttpJsonError: HTTP request failed with status 400",
                },
            },
        })
        MODULE._atomic_write_json(state_path, state)
        preview = MODULE.recover(self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("retry_excerpt_generation", preview["strategy"])
        self.assertEqual("http_client_error", preview["actual_error"])

    def test_legacy_excerpt_recovery_preflight_failure_preserves_retry_basis(self):
        state_path, source, content = self.prepare_source_restart()
        from src.candidate_execution import sha256_text
        prewrite_path = MODULE._prewrite_path(self.root, 401)
        prewrite = json.loads(prewrite_path.read_text(encoding="utf-8"))
        prewrite["sha256"].update({
            "chinese_title": sha256_text("人工修复标题"),
            "chinese_content": sha256_text(content),
        })
        MODULE._atomic_write_json(prewrite_path, prewrite)
        execution_path = self.write_execution(401, 1401, "prepared")
        execution_before = execution_path.read_bytes()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({
            "workflow_status": "blocked",
            "retry_counts": {"run": MODULE.MAX_RUN_ATTEMPTS, "resume": 0},
            "execution_evidence": {
                "status": "prepared", "sha256": MODULE._file_sha256(execution_path),
            },
            "last_failure": {
                "stage": "run", "reason": "transient_network_error",
                "stderr_summary": "HttpJsonError: HTTP request failed with status 400",
            },
        })
        MODULE._atomic_write_json(state_path, state)
        calls = []

        def readonly_failure(command, **kwargs):
            calls.append(command)
            self.assertIn("--preflight-live", command)
            return mock.Mock(returncode=1, stdout="", stderr=
                "ReadError: read-only source temporarily unavailable")

        failed = MODULE.recover(
            self.root, 401, execute=True, source_factory=lambda rows: source,
            runner=readonly_failure)
        self.assertEqual("failed", failed["reexecution"])
        self.assertEqual("preflight_failed", failed["operation_result"]["category"])
        self.assertEqual(1, len(calls))
        self.assertEqual(execution_before, execution_path.read_bytes())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["workflow_status"])
        self.assertEqual("transient_network_error", state["last_failure"]["reason"])
        self.assertEqual("preflight_failed", state["recovery"]["last_preflight_failure"]["category"])
        self.assertEqual(1, state["recovery"]["preflight_attempts"])
        retry = MODULE.recover(self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("retry_excerpt_generation", retry["strategy"])

        def glm_failure(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            MODULE._atomic_write_json(execution_path, {
                "schema_version": 1, "chinese_post_id": 401,
                "english_post_id": 1401, "status": "excerpt_generation_failed",
                "error": "HTTP request failed with status 400",
                "excerpt_generation_attempts": 1,
            })
            return mock.Mock(returncode=1, stdout="", stderr=
                "HttpJsonError: HTTP request failed with status 400")

        retried = MODULE.recover(
            self.root, 401, execute=True, source_factory=lambda rows: source,
            runner=glm_failure)
        self.assertEqual("excerpt_failed", retried["final_status"])
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        self.assertEqual("excerpt_generation_failed", execution["status"])

    def test_recover_accepts_legacy_prepared_restart_without_coordination_generation(self):
        """Pre-fix coordination evidence lacks generation but remains SHA-bound."""
        state_path, source, _ = self.prepare_source_restart()
        MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)

        def timeout_runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            execution_path = MODULE._execution_path(self.root, 401)
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            execution["started_at"] = "2026-08-13T00:00:01+00:00"
            MODULE._atomic_write_json(execution_path, execution)
            return mock.Mock(returncode=1, stdout="", stderr=(
                "HttpJsonError: network request failed: TimeoutError"))

        MODULE._run_ready_once(
            self.root, execute=True, batch_id="syntaxhighlighter-20260723-01",
            post_id=401, runner=timeout_runner)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        del state["execution_evidence"]["recovery_generation"]
        MODULE._atomic_write_json(state_path, state)

        preview = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("retry_prepared_source_restart", preview["strategy"])

        calls = []
        def complete_runner(command, **kwargs):
            calls.append(command)
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")

        recovered = MODULE.recover(
            self.root, 401, execute=True, source_factory=lambda rows: source,
            runner=complete_runner)
        self.assertEqual("completed", recovered["final_status"])
        self.assertTrue(all("--resume" not in command for command in calls))
        self.assertTrue(all("--recovery-restart" in command for command in calls))

    def test_recover_keeps_unproven_blocked_prepared_state_blocked(self):
        state_path, source, _ = self.prepare_source_restart()
        MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({
            "workflow_status": "blocked",
            "last_failure": {"stage": "run", "reason": "transient_network_error"},
            "execution_evidence": {
                **state["execution_evidence"], "recovery_generation": 999,
            },
        })
        MODULE._atomic_write_json(state_path, state)
        result = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("blocked", result["strategy"])
        self.assertTrue(any("neither translation_failed" in reason
                            for reason in result["strategy_reasons"]))

    def test_mark_manual_completed_confirms_translation_failure_without_changing_execution(self):
        state_path, source, _ = self.prepare_source_restart()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "translation_failed"
        MODULE._atomic_write_json(state_path, state)
        execution_path = MODULE._execution_path(self.root, 401)
        execution_before = execution_path.read_bytes()
        preview = MODULE.mark_manual_completed(
            self.root, 401, source_factory=lambda rows: source)
        self.assertTrue(preview["eligible"])
        self.assertFalse(preview["writes_performed"])
        self.assertEqual("translation_failed", json.loads(
            state_path.read_text(encoding="utf-8"))["workflow_status"])

        result = MODULE.mark_manual_completed(
            self.root, 401, confirmed=True, source_factory=lambda rows: source)
        self.assertTrue(result["changed"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("completed", state["workflow_status"])
        self.assertEqual("manual_external", state["manual_completion"]["method"])
        self.assertEqual(execution_before, execution_path.read_bytes())
        events = MODULE._read_events(MODULE._events_path(
            self.root, "syntaxhighlighter-20260723-01"))
        event = next(item for item in events if item["event_type"]
                     == "manual_external_completion_confirmed")
        self.assertEqual(1401, event["evidence"]["english_post_id"])
        batch = next(item for item in MODULE.summary(self.root)["batches"]
                     if item["batch_id"] == "syntaxhighlighter-20260723-01")
        self.assertEqual(1, batch["completed"])
        self.assertEqual(batch["total"] - 1, batch["remaining"])

    def test_mark_manual_completed_allows_blocked(self):
        state_path, source, _ = self.prepare_source_restart()
        result = MODULE.mark_manual_completed(
            self.root, 401, confirmed=True, source_factory=lambda rows: source)
        self.assertTrue(result["changed"])
        self.assertEqual("completed", json.loads(
            state_path.read_text(encoding="utf-8"))["workflow_status"])

    def test_mark_manual_completed_rejects_invalid_polylang(self):
        state_path, source, _ = self.prepare_source_restart()
        source.check = lambda chinese, english: {
            "chinese_post_id": chinese, "chinese_language": "zh",
            "linked_english_post_id": 9999, "english_post_id": english,
            "english_language": "en", "linked_chinese_post_id": chinese,
        }
        result = MODULE.mark_manual_completed(
            self.root, 401, confirmed=True, source_factory=lambda rows: source)
        self.assertFalse(result["eligible"])
        self.assertIn("Polylang relation mismatch", result["blocking_reasons"])
        self.assertEqual("blocked", json.loads(
            state_path.read_text(encoding="utf-8"))["workflow_status"])

    def test_mark_manual_completed_rejects_unpublished_or_empty_english(self):
        state_path, source, _ = self.prepare_source_restart()
        original_get = source.get_post
        def get_post(post_id):
            post = original_get(post_id)
            if int(post_id) == 1401:
                post = dict(post)
                post["status"] = "draft"
                post["content"] = {"raw": ""}
            return post
        source.get_post = get_post
        result = MODULE.mark_manual_completed(
            self.root, 401, confirmed=True, source_factory=lambda rows: source)
        self.assertFalse(result["eligible"])
        self.assertIn("English post is not publish", result["blocking_reasons"])
        self.assertIn("English content is empty", result["blocking_reasons"])
        self.assertEqual("blocked", json.loads(
            state_path.read_text(encoding="utf-8"))["workflow_status"])

    def test_mark_manual_completed_does_not_repeat_completed_article(self):
        state_path, _, _ = self.prepare_source_restart()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "completed"
        MODULE._atomic_write_json(state_path, state)
        result = MODULE.mark_manual_completed(
            self.root, 401, confirmed=True,
            source_factory=mock.Mock(side_effect=AssertionError("must not read")))
        self.assertFalse(result["eligible"])
        self.assertFalse(result["changed"])
        self.assertIn("article is already completed", result["blocking_reasons"])

    def test_recover_rejects_missing_execution_generation(self):
        state_path, source, _ = self.prepare_source_restart()
        MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)
        execution_path = MODULE._execution_path(self.root, 401)
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        del execution["recovery_generation"]
        MODULE._atomic_write_json(execution_path, execution)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({
            "workflow_status": "blocked",
            "last_failure": {
                "stage": "run", "reason": "transient_network_error"},
        })
        state["execution_evidence"]["sha256"] = MODULE._file_sha256(
            execution_path)
        MODULE._atomic_write_json(state_path, state)
        result = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("blocked", result["strategy"])
        self.assertTrue(any("neither translation_failed" in reason
                            for reason in result["strategy_reasons"]))

    def test_restart_from_current_accepts_exact_old_generated_excerpt(self):
        _, source, _ = self.prepare_source_restart(excerpt="旧摘要")
        before = self.snapshot()
        preview = MODULE.restart_from_current(
            self.root, 401, source_factory=lambda rows: source)
        self.assertTrue(preview["eligible"], preview["blocking_reasons"])
        self.assertEqual(before, self.snapshot())
        applied = MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)
        audit = json.loads((self.root / applied["archive"] / "recovery.json").read_text(
            encoding="utf-8"))
        self.assertEqual("known_previous_generated_excerpt",
                         audit["restart_excerpt_state"])
        self.assertTrue(audit["current_excerpt_matches_old_generated"])
        self.assertEqual(audit["current_excerpt_sha256"],
                         audit["old_generated_excerpt_sha256"])

    def test_restart_from_current_rejects_unknown_nonempty_excerpt(self):
        _, source, _ = self.prepare_source_restart(excerpt="人工写入的其他摘要")
        before = self.snapshot()
        result = MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)
        self.assertFalse(result["eligible"])
        self.assertIn("chinese_excerpt_known_for_restart",
                      result["blocking_reasons"])
        self.assertEqual(before, self.snapshot())

    def test_restart_from_current_rebaselines_increased_code_block_pro_count(self):
        from src.candidate_execution import sha256_text
        _, source, content = self.prepare_source_restart()
        increased, increased_content, _ = self.restart_source(
            chinese_content=content + content)
        result = MODULE.restart_from_current(
            self.root, 401, apply=True,
            source_factory=lambda rows: increased)
        self.assertTrue(result["eligible"], result["blocking_reasons"])
        manifest_path = MODULE._validation_paths(
            self.root, "syntaxhighlighter-20260723-01", 401)[2]
        with manifest_path.open(encoding="utf-8", newline="") as handle:
            manifest = next(csv.DictReader(handle))
        self.assertEqual("2", manifest["expected_code_block_pro_count"])
        self.assertEqual(
            sha256_text(increased_content),
            manifest["chinese_content_sha256"])

    def test_restart_from_current_rebaselines_reduced_code_block_pro_count(self):
        _, source, _ = self.prepare_source_restart()
        manifest_path = MODULE._validation_paths(
            self.root, "syntaxhighlighter-20260723-01", 401)[2]
        rows, _ = MODULE._read_csv(manifest_path)
        rows[0]["expected_code_block_pro_count"] = "2"
        MODULE._atomic_write_csv(
            manifest_path, MODULE.EXECUTION_MANIFEST_FIELDS, rows)
        result = MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)
        self.assertTrue(result["eligible"], result["blocking_reasons"])
        with manifest_path.open(encoding="utf-8", newline="") as handle:
            manifest = next(csv.DictReader(handle))
        self.assertEqual("1", manifest["expected_code_block_pro_count"])

    def test_restart_from_current_rejects_reintroduced_syntaxhighlighter_or_invalid_format(self):
        _, _, content = self.prepare_source_restart()
        syntax_source, _, _ = self.restart_source(
            chinese_content=content +
            '<!-- wp:syntaxhighlighter/code --><pre>x</pre>'
            '<!-- /wp:syntaxhighlighter/code -->')
        invalid_source, _, _ = self.restart_source(
            chinese_content="<pre><code>echo 1;</code></pre>")
        for source, reason in (
                (syntax_source, "syntaxhighlighter_zero"),
                (invalid_source, "gutenberg")):
            with self.subTest(reason=reason):
                result = MODULE.restart_from_current(
                    self.root, 401, source_factory=lambda rows, s=source: s)
                self.assertFalse(result["eligible"])
                self.assertIn(reason, result["blocking_reasons"])

    def test_restart_from_current_apply_archives_and_rebaselines_only_post(self):
        state_path, source, content = self.prepare_source_restart()
        old_execution_sha = MODULE._file_sha256(
            MODULE._execution_path(self.root, 401))
        old_prewrite_sha = MODULE._file_sha256(
            MODULE._prewrite_path(self.root, 401))
        other_path = MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 402)
        other_before = other_path.read_bytes()
        result = MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source,
            reason="operator edited Chinese source")
        self.assertTrue(result["changed"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        execution = json.loads(MODULE._execution_path(
            self.root, 401).read_text(encoding="utf-8"))
        backup = json.loads(MODULE._prewrite_path(
            self.root, 401).read_text(encoding="utf-8"))
        archive = self.root / result["archive"]
        audit = json.loads((archive / "recovery.json").read_text(encoding="utf-8"))
        self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertEqual("prepared", execution["status"])
        self.assertNotIn("generated_excerpt", execution)
        self.assertEqual(backup["sha256"]["chinese_content"],
                         audit["current_chinese_content_sha256"])
        self.assertEqual("translation_failed", audit["old_execution_status"])
        self.assertEqual("旧摘要", audit["old_generated_excerpt"])
        self.assertTrue((archive / "chinese-401.execution.json").is_file())
        self.assertTrue((archive / "chinese-401.pre-write.json").is_file())
        self.assertEqual(old_execution_sha, MODULE._file_sha256(
            archive / "chinese-401.execution.json"))
        self.assertEqual(old_prewrite_sha, MODULE._file_sha256(
            archive / "chinese-401.pre-write.json"))
        self.assertEqual(other_before, other_path.read_bytes())
        run_plan = MODULE._run_items(self.root, post_id=401)[1]
        item = next(value for value in run_plan if value["post_id"] == 401)
        self.assertTrue(item["allowed"], item["blocking_reasons"])
        self.assertIn("--recovery-restart", MODULE._executor_command(
            self.root, state, preflight=True))
        batch_plan = MODULE.run_ready(self.root)
        recovery_item = next(
            item for item in batch_plan["items"] if item["post_id"] == 401)
        self.assertFalse(recovery_item["allowed"])
        self.assertTrue(any(
            "explicit matching --post-id" in reason
            for reason in recovery_item["blocking_reasons"]))
        single_plan = MODULE.run_ready(self.root, post_id=401)
        self.assertEqual(1, single_plan["selected_count"])
        self.assertTrue(single_plan["items"][0]["allowed"])

    def test_restart_from_current_fails_closed_on_english_drift(self):
        _, _, _ = self.prepare_source_restart()
        drifted, _, _ = self.restart_source(english_title="partially translated")
        before = self.snapshot()
        result = MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: drifted)
        self.assertFalse(result["eligible"])
        self.assertIn("english_title_unchanged", result["blocking_reasons"])
        self.assertEqual(before, self.snapshot())

    def test_excerpt_rejected_restart_preview_is_read_only(self):
        _, source, _, _ = self.prepare_rejected_restart()
        before = self.snapshot()
        result = MODULE.restart_from_current(
            self.root, 401, source_factory=lambda rows: source)
        self.assertTrue(result["eligible"], result["blocking_reasons"])
        self.assertEqual(
            "rejected_excerpt_regeneration",
            result["excerpt_observation"]["recovery_kind"])
        self.assertEqual(before, self.snapshot())

    def test_recover_excerpt_rejected_preview_selects_regeneration(self):
        _, source, _, _ = self.prepare_rejected_restart()
        before = self.snapshot()
        result = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        output = MODULE.render_recover_text(result)
        self.assertEqual("rejected_excerpt_generation", result["actual_error"])
        self.assertEqual("rejected_excerpt_regeneration", result["strategy"])
        self.assertEqual("unchanged", result["production_source"])
        self.assertNotIn("unknown", output)
        self.assertNotIn("恢复策略: blocked", output)
        self.assertIn("重新生成中文摘要，并继续安全执行", output)
        self.assertIn("生产中文源及英文源仍与 pre-write 基线一致", output)
        self.assertFalse(result["writes_performed"])
        self.assertEqual(before, self.snapshot())

    def test_recover_accepts_legacy_rejected_excerpt_generation_status(self):
        _, source, _, _ = self.prepare_rejected_restart()
        execution_path = MODULE._execution_path(self.root, 401)
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution["status"] = "rejected_excerpt_generation"
        MODULE._atomic_write_json(execution_path, execution)
        result = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("rejected_excerpt_regeneration", result["strategy"])
        self.assertEqual("rejected_excerpt_generation", result["actual_error"])

    def test_recover_excerpt_rejected_rejects_source_or_evidence_drift(self):
        _, source, _, _ = self.prepare_rejected_restart()
        changed_chinese, _, _ = self.restart_source(
            chinese_title="生产标题已变化")
        changed_english, _, _ = self.restart_source(
            english_content="production English changed")
        for changed, expected in (
                (changed_chinese, "chinese_title_unchanged"),
                (changed_english, "english_content_unchanged")):
            with self.subTest(expected=expected):
                result = MODULE.recover(
                    self.root, 401, source_factory=lambda rows, s=changed: s)
                self.assertEqual("blocked", result["strategy"])
                self.assertIn(expected, result["strategy_reasons"])
        execution = json.loads(MODULE._execution_path(
            self.root, 401).read_text(encoding="utf-8"))
        Path(execution["rejected_excerpt_paths"][0]).unlink()
        result = MODULE.recover(
            self.root, 401, source_factory=lambda rows: source)
        self.assertEqual("blocked", result["strategy"])
        self.assertTrue(any("invalid rejected excerpt evidence" in reason
                            for reason in result["strategy_reasons"]))

    def test_recover_excerpt_rejected_execute_completes_and_archives_evidence(self):
        _, source, _, rejected_paths = self.prepare_rejected_restart()
        old_hashes = {Path(path).name: MODULE._file_sha256(Path(path))
                      for path in rejected_paths}
        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="", stderr="")
        result = MODULE.recover(
            self.root, 401, execute=True,
            source_factory=lambda rows: source, runner=runner)
        self.assertEqual("completed", result["final_status"])
        archive = next((self.root / MODULE.RESTART_ARCHIVE_ROOT /
                        "chinese-401").iterdir())
        audit = json.loads((archive / "recovery.json").read_text(encoding="utf-8"))
        self.assertEqual(3, len(audit["rejected_excerpt_evidence"]))
        for evidence in audit["rejected_excerpt_evidence"]:
            self.assertEqual(old_hashes[Path(evidence["source_path"]).name],
                             evidence["archive_sha256"])

    def test_recover_excerpt_rejected_execute_can_return_to_excerpt_failed(self):
        _, source, _, _ = self.prepare_rejected_restart()
        def runner(command, **kwargs):
            if "--preflight-live" in command:
                return mock.Mock(returncode=0, stdout="{}", stderr="")
            self.write_execution(401, 1401, "excerpt_rejected")
            return mock.Mock(returncode=2, stdout="", stderr=
                "generated Chinese excerpt contains Markdown or a list")
        result = MODULE.recover(
            self.root, 401, execute=True,
            source_factory=lambda rows: source, runner=runner)
        self.assertEqual("excerpt_failed", result["final_status"])
        self.assertIn("rejected excerpt evidence", result["next_step"])

    def test_excerpt_rejected_restart_apply_archives_all_evidence(self):
        state_path, source, _, rejected_paths = self.prepare_rejected_restart()
        originals = {
            Path(path).name: MODULE._file_sha256(Path(path))
            for path in rejected_paths
        }
        execution_sha = MODULE._file_sha256(MODULE._execution_path(self.root, 401))
        prewrite_sha = MODULE._file_sha256(MODULE._prewrite_path(self.root, 401))
        result = MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)
        self.assertTrue(result["changed"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("ready_for_execution", state["workflow_status"])
        self.assertEqual(1, state["recovery_generation"])
        archive = self.root / result["archive"]
        audit = json.loads((archive / "recovery.json").read_text(encoding="utf-8"))
        self.assertEqual("excerpt_rejected", audit["old_execution_status"])
        self.assertEqual("rejected_excerpt_regeneration", audit["recovery_kind"])
        self.assertEqual(execution_sha, audit["old_execution_sha256"])
        self.assertEqual(prewrite_sha, audit["old_pre_write_sha256"])
        self.assertEqual(3, len(audit["rejected_excerpt_evidence"]))
        for evidence in audit["rejected_excerpt_evidence"]:
            self.assertEqual(originals[Path(evidence["source_path"]).name],
                             evidence["sha256"])
            self.assertEqual(evidence["sha256"], evidence["archive_sha256"])
            self.assertEqual(evidence["sha256"], MODULE._file_sha256(
                self.root / evidence["archive_path"]))
        execution = json.loads(MODULE._execution_path(
            self.root, 401).read_text(encoding="utf-8"))
        self.assertEqual("prepared", execution["status"])
        self.assertNotIn("generated_excerpt", execution)
        self.assertIn("--recovery-restart", MODULE._executor_command(
            self.root, state, preflight=True))

    def test_excerpt_rejected_restart_rejects_modified_chinese_source(self):
        state_path, source, _, _ = self.prepare_rejected_restart()
        prewrite_path = MODULE._prewrite_path(self.root, 401)
        prewrite = json.loads(prewrite_path.read_text(encoding="utf-8"))
        prewrite["sha256"]["chinese_title"] = "a" * 64
        prewrite["sha256"]["chinese_content"] = "b" * 64
        MODULE._atomic_write_json(prewrite_path, prewrite)

        preview = MODULE.restart_from_current(
            self.root, 401, source_factory=lambda rows: source)
        self.assertFalse(preview["eligible"])
        self.assertIn("chinese_title_unchanged", preview["blocking_reasons"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual("excerpt_failed", state["workflow_status"])

    def test_excerpt_rejected_restart_fails_closed_on_excerpt_or_english_drift(self):
        state_path, source, _, _ = self.prepare_rejected_restart()
        before = self.snapshot()
        for changed_source in (
                self.restart_source(excerpt="人工摘要")[0],
                self.restart_source(english_title="drifted English title")[0],
                self.restart_source(english_excerpt="drifted English excerpt")[0],
                self.restart_source(english_content="drifted English body")[0]):
            result = MODULE.restart_from_current(
                self.root, 401, apply=True, source_factory=lambda rows, s=changed_source: s)
            self.assertFalse(result["eligible"])
            self.assertEqual(before, self.snapshot())
        execution_path = MODULE._execution_path(self.root, 401)
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution["status"] = "prepared"
        MODULE._atomic_write_json(execution_path, execution)
        result = MODULE.restart_from_current(
            self.root, 401, source_factory=lambda rows: source)
        self.assertFalse(result["eligible"])
        self.assertTrue(any("neither" in reason for reason in result["blocking_reasons"]))

    def test_excerpt_rejected_restart_requires_parseable_complete_evidence(self):
        _, source, _, _ = self.prepare_rejected_restart(missing_rejected=True)
        result = MODULE.restart_from_current(
            self.root, 401, apply=True, source_factory=lambda rows: source)
        self.assertFalse(result["eligible"])
        self.assertTrue(any("invalid rejected excerpt evidence" in reason
                            for reason in result["blocking_reasons"]))
        MODULE._execution_path(self.root, 401).write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.ReadError, "invalid execution JSON"):
            MODULE.restart_from_current(
                self.root, 401, source_factory=lambda rows: source)

    def test_restart_from_current_rejects_completed_and_missing_evidence(self):
        state_path, source, _ = self.prepare_source_restart()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workflow_status"] = "completed"
        MODULE._atomic_write_json(state_path, state)
        self.assertFalse(MODULE.restart_from_current(
            self.root, 401, source_factory=lambda rows: source)["eligible"])
        state["workflow_status"] = "blocked"
        MODULE._atomic_write_json(state_path, state)
        MODULE._prewrite_path(self.root, 401).unlink()
        result = MODULE.restart_from_current(
            self.root, 401, source_factory=lambda rows: source)
        self.assertFalse(result["eligible"])
        self.assertIn("both execution and pre-write evidence are required",
                      result["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()
