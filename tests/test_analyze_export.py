from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "bin/analyze-export.py"
SPEC = importlib.util.spec_from_file_location("analyze_export", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_record(post_id, content="<p>人工最小内容</p>"):
    return {
        "schema_version": 1,
        "post_id": post_id,
        "post_type": "post",
        "post_status": "publish",
        "language_source": "polylang",
        "language": "zh",
        "published_at": "2020-01-01 00:00:00",
        "modified_at": "2020-01-02 00:00:00",
        "title": "不应输出",
        "excerpt": "不应输出",
        "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


class AnalyzeExportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.input_path = self.directory / "input.jsonl"
        self.output_path = self.directory / "output.analysis.jsonl"
        self.records = [make_record(1), make_record(2), make_record(3)]

    def tearDown(self):
        self.temporary.cleanup()

    def run_valid(self):
        write_jsonl(self.input_path, self.records)
        return MODULE.run_analysis(self.input_path, self.output_path, len(self.records))

    def assert_validation_fails(self):
        with self.assertRaises(MODULE.InputValidationError):
            MODULE.run_analysis(self.input_path, self.output_path, 3)
        self.assertFalse(self.output_path.exists())

    def assert_same_file_rejected(self, input_path, output_path):
        before = input_path.read_bytes()
        before_hash = hashlib.sha256(before).hexdigest()
        before_size = len(before)
        output_name = Path(output_path).name
        with self.assertRaisesRegex(
            MODULE.InputValidationError,
            "input and output must refer to different files",
        ):
            MODULE.run_analysis(input_path, output_path, len(self.records))
        after = input_path.read_bytes()
        self.assertEqual(before_hash, hashlib.sha256(after).hexdigest())
        self.assertEqual(before_size, len(after))
        self.assertEqual([], list(Path(output_path).parent.glob(f".{output_name}.*.tmp")))

    def test_normal_three_records(self):
        result = self.run_valid()
        self.assertEqual(3, len(result))
        self.assertEqual(3, len(self.output_path.read_text(encoding="utf-8").splitlines()))

    def test_one_expected_record(self):
        self.records = [make_record(1)]
        result = self.run_valid()
        self.assertEqual(1, len(result))

    def test_five_expected_records(self):
        self.records = [make_record(post_id) for post_id in range(1, 6)]
        result = self.run_valid()
        self.assertEqual(5, len(result))

    def test_expected_five_but_actual_three_fails(self):
        write_jsonl(self.input_path, self.records)
        with self.assertRaisesRegex(MODULE.InputValidationError, "expected 5, found 3"):
            MODULE.run_analysis(self.input_path, self.output_path, 5)
        self.assertFalse(self.output_path.exists())

    def test_expected_two_but_actual_three_fails(self):
        write_jsonl(self.input_path, self.records)
        with self.assertRaisesRegex(MODULE.InputValidationError, "expected 2, found 3"):
            MODULE.run_analysis(self.input_path, self.output_path, 2)
        self.assertFalse(self.output_path.exists())

    def test_expected_count_zero_is_rejected(self):
        write_jsonl(self.input_path, self.records)
        with self.assertRaisesRegex(MODULE.InputValidationError, "between 1 and 100"):
            MODULE.run_analysis(self.input_path, self.output_path, 0)

    def test_expected_count_101_is_rejected(self):
        write_jsonl(self.input_path, self.records)
        with self.assertRaisesRegex(MODULE.InputValidationError, "between 1 and 100"):
            MODULE.run_analysis(self.input_path, self.output_path, 101)

    def test_cli_non_integer_expected_count_is_rejected(self):
        with self.assertRaises(SystemExit) as raised:
            MODULE.main(["--expected-count", "three", str(self.input_path), str(self.output_path)])
        self.assertNotEqual(0, raised.exception.code)
        self.assertFalse(self.output_path.exists())

    def test_cli_requires_expected_count(self):
        with self.assertRaises(SystemExit) as raised:
            MODULE.main([str(self.input_path), str(self.output_path)])
        self.assertNotEqual(0, raised.exception.code)
        self.assertFalse(self.output_path.exists())

    def test_analysis_count_contract_is_explicitly_bounded(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertEqual(1, MODULE.MIN_EXPECTED_COUNT)
        self.assertEqual(100, MODULE.MAX_EXPECTED_COUNT)
        self.assertIn('"--expected-count"', source)
        self.assertIn("required=True", source)
        for forbidden_option in ("--all", "--unlimited", "--ignore-count"):
            self.assertNotIn(forbidden_option, source)

    def test_count_validation_uses_explicit_expected_count(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("def load_and_validate_input(input_path, expected_count):", source)
        self.assertIn("if len(records) != expected_count:", source)
        self.assertNotIn("EXPECTED_RECORD_COUNT", source)

    def test_count_mismatch_preserves_existing_output(self):
        write_jsonl(self.input_path, self.records)
        original = b"existing-safe-output\n"
        self.output_path.write_bytes(original)
        with self.assertRaisesRegex(MODULE.InputValidationError, "expected 5, found 3"):
            MODULE.run_analysis(self.input_path, self.output_path, 5)
        self.assertEqual(original, self.output_path.read_bytes())

    def test_empty_file(self):
        self.input_path.write_bytes(b"")
        self.assert_validation_fails()

    def test_blank_line(self):
        write_jsonl(self.input_path, self.records[:1])
        with self.input_path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
            handle.write(json.dumps(self.records[1], ensure_ascii=False) + "\n")
            handle.write(json.dumps(self.records[2], ensure_ascii=False) + "\n")
        self.assert_validation_fails()

    def test_invalid_json(self):
        self.input_path.write_text("{invalid}\n", encoding="utf-8")
        self.assert_validation_fails()

    def test_missing_required_field(self):
        del self.records[0]["content_sha256"]
        write_jsonl(self.input_path, self.records)
        self.assert_validation_fails()

    def test_content_sha256_mismatch(self):
        self.records[1]["content_sha256"] = "0" * 64
        write_jsonl(self.input_path, self.records)
        self.assert_validation_fails()

    def test_duplicate_post_id(self):
        self.records[2]["post_id"] = self.records[0]["post_id"]
        write_jsonl(self.input_path, self.records)
        self.assert_validation_fails()

    def test_non_chinese_language(self):
        self.records[0]["language"] = "en"
        write_jsonl(self.input_path, self.records)
        self.assert_validation_fails()

    def test_non_publish_status(self):
        self.records[0]["post_status"] = "draft"
        write_jsonl(self.input_path, self.records)
        self.assert_validation_fails()

    def test_output_excludes_sensitive_source_fields(self):
        result = self.run_valid()
        for record in result:
            self.assertFalse(MODULE.FORBIDDEN_OUTPUT_FIELDS & record.keys())
        serialized = self.output_path.read_text(encoding="utf-8")
        self.assertNotIn("不应输出", serialized)
        self.assertNotIn("人工最小内容", serialized)

    def test_output_order_matches_input(self):
        self.records = [make_record(30), make_record(10), make_record(20)]
        result = self.run_valid()
        self.assertEqual([30, 10, 20], [record["post_id"] for record in result])

    def test_input_file_is_not_modified(self):
        write_jsonl(self.input_path, self.records)
        before = self.input_path.read_bytes()
        MODULE.run_analysis(self.input_path, self.output_path, 3)
        self.assertEqual(before, self.input_path.read_bytes())

    def test_identical_input_output_path_is_rejected(self):
        write_jsonl(self.input_path, self.records)
        self.assert_same_file_rejected(self.input_path, self.input_path)

    def test_dot_equivalent_output_path_is_rejected(self):
        write_jsonl(self.input_path, self.records)
        output = f"{self.directory}/./{self.input_path.name}"
        self.assert_same_file_rejected(self.input_path, output)

    def test_dotdot_equivalent_output_path_is_rejected(self):
        write_jsonl(self.input_path, self.records)
        output = self.directory / "analysis" / ".." / self.input_path.name
        self.assert_same_file_rejected(self.input_path, output)

    def test_symlink_output_to_input_is_rejected(self):
        write_jsonl(self.input_path, self.records)
        try:
            self.output_path.symlink_to(self.input_path)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"symbolic links are unavailable: {error}")
        self.assert_same_file_rejected(self.input_path, self.output_path)

    def test_hardlink_output_to_input_is_rejected(self):
        write_jsonl(self.input_path, self.records)
        try:
            os.link(self.input_path, self.output_path)
        except OSError as error:
            self.skipTest(f"hard links are unavailable: {error}")
        self.assert_same_file_rejected(self.input_path, self.output_path)

    def test_distinct_nonexistent_output_still_succeeds(self):
        write_jsonl(self.input_path, self.records)
        self.assertFalse(self.output_path.exists())
        result = MODULE.run_analysis(self.input_path, self.output_path, len(self.records))
        self.assertEqual(len(self.records), len(result))
        self.assertTrue(self.output_path.is_file())

    def test_cli_rejects_identical_input_output_with_nonzero_exit(self):
        write_jsonl(self.input_path, self.records)
        before = self.input_path.read_bytes()
        with self.assertRaises(SystemExit) as raised:
            MODULE.main([
                "--expected-count", str(len(self.records)),
                str(self.input_path), str(self.input_path),
            ])
        self.assertNotEqual(0, raised.exception.code)
        self.assertEqual(before, self.input_path.read_bytes())
        self.assertEqual([], list(self.directory.glob(f".{self.input_path.name}.*.tmp")))

    def test_analyzer_error_leaves_no_partial_formal_output(self):
        write_jsonl(self.input_path, self.records)
        with mock.patch.object(MODULE, "analyze_content", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                MODULE.run_analysis(self.input_path, self.output_path, 3)
        self.assertFalse(self.output_path.exists())

    def test_atomic_writer_uses_temporary_file_then_replace(self):
        records = [{"post_id": 1}]
        with mock.patch.object(MODULE.os, "replace", wraps=MODULE.os.replace) as replace:
            MODULE.write_jsonl_atomically(self.output_path, records)
        replace.assert_called_once()
        source, destination = replace.call_args.args
        self.assertNotEqual(Path(source), self.output_path)
        self.assertEqual(Path(destination), self.output_path)
        self.assertTrue(self.output_path.exists())


if __name__ == "__main__":
    unittest.main()
