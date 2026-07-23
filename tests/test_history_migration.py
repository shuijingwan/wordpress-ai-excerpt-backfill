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

    def write_batch(self, name, pairs, sequence=1, fields=None):
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
        self.write_csv(path, fields or SYNTAX_FIELDS, values)
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
        })
        MODULE._atomic_write_csv(
            path, MODULE.EXECUTION_MANIFEST_FIELDS, [row])
        return path

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

    def test_mark_converted_requires_language_and_matching_counts(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        with self.assertRaisesRegex(MODULE.ReadError, "language-review-confirmed"):
            MODULE.mark_converted(self.root, 401, 1, 1, False)
        with self.assertRaisesRegex(MODULE.ReadError, "must equal"):
            MODULE.mark_converted(self.root, 401, 1, 2, True)

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

    def test_record_validation_failure_is_isolated(self):
        self.prepare_init_fixture()
        MODULE.init_state(self.root, apply=True)
        MODULE.mark_converted(self.root, 401, 1, 1, True)
        path = self.write_record_validation(
            validation_status="abnormal",
            validation_reasons="code-block-pro-count-mismatch",
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
            validation_reasons="code-block-pro-count-mismatch",
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
            self.assertIn("--execute", command)
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        state = json.loads(MODULE._state_path(
            self.root, "syntaxhighlighter-20260723-01", 401
        ).read_text(encoding="utf-8"))
        self.assertEqual("completed", state["workflow_status"])
        self.assertEqual("completed", result["results"][0]["result"])
        self.assertEqual([], MODULE.run_ready(self.root)["items"])

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
            status = "excerpt_rejected" if post_id == 401 else "completed"
            self.write_execution(post_id, post_id + 1000, status)
            return mock.Mock(returncode=2 if post_id == 401 else 0,
                             stdout="", stderr="")

        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        self.assertEqual(2, len(result["results"]))
        states = {
            post_id: json.loads(MODULE._state_path(
                self.root, "syntaxhighlighter-20260723-01", post_id
            ).read_text(encoding="utf-8"))["workflow_status"]
            for post_id in (401, 402)
        }
        self.assertEqual("excerpt_failed", states[401])
        self.assertEqual("completed", states[402])

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
            self.write_execution(402, 1402, "completed")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        result = MODULE.run_ready(self.root, execute=True, runner=runner)
        self.assertEqual(["blocked", "completed"],
                         [item["result"] for item in result["results"]])
        states = [
            json.loads(MODULE._state_path(
                self.root, "syntaxhighlighter-20260723-01", post_id
            ).read_text(encoding="utf-8"))["workflow_status"]
            for post_id in (401, 402)
        ]
        self.assertEqual(["blocked", "completed"], states)

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
        preview = MODULE.resume(self.root)
        self.assertEqual([401], [item["post_id"] for item in preview["items"]])
        self.assertTrue(preview["items"][0]["allowed"])

        def runner(command, **kwargs):
            self.assertIn("--resume", command)
            self.write_execution(401, 1401, "completed")
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        MODULE.resume(self.root, execute=True, runner=runner)
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


if __name__ == "__main__":
    unittest.main()
