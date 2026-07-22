#!/usr/bin/env python3
"""Validate and locally analyze one explicitly bounded WordPress JSONL export."""

from collections import Counter
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzer import analyze_content  # noqa: E402
from src.eligibility import evaluate_phase1_eligibility  # noqa: E402


MIN_EXPECTED_COUNT = 1
MAX_EXPECTED_COUNT = 100
REQUIRED_FIELDS = {
    "schema_version",
    "post_id",
    "post_type",
    "post_status",
    "language_source",
    "language",
    "published_at",
    "modified_at",
    "content",
    "content_sha256",
}
FORBIDDEN_OUTPUT_FIELDS = {
    "content", "excerpt", "title", "slug", "permalink", "categories", "tags",
}


class InputValidationError(ValueError):
    """Raised before analysis when the complete input contract is not satisfied."""


def _validate_record(record, line_number, seen_post_ids):
    if not isinstance(record, dict):
        raise InputValidationError(f"line {line_number}: JSON value must be an object")
    missing = sorted(REQUIRED_FIELDS - record.keys())
    if missing:
        raise InputValidationError(f"line {line_number}: missing required fields: {', '.join(missing)}")
    if record["schema_version"] != 1:
        raise InputValidationError(f"line {line_number}: schema_version must be 1")
    if record["post_type"] != "post":
        raise InputValidationError(f"line {line_number}: post_type must be post")
    if record["post_status"] != "publish":
        raise InputValidationError(f"line {line_number}: post_status must be publish")
    if record["language_source"] != "polylang":
        raise InputValidationError(f"line {line_number}: language_source must be polylang")
    if record["language"] != "zh":
        raise InputValidationError(f"line {line_number}: language must be zh")
    if not isinstance(record["post_id"], int) or isinstance(record["post_id"], bool):
        raise InputValidationError(f"line {line_number}: post_id must be an integer")
    if record["post_id"] in seen_post_ids:
        raise InputValidationError(f"line {line_number}: duplicate post_id")
    seen_post_ids.add(record["post_id"])
    if not isinstance(record["content"], str):
        raise InputValidationError(f"line {line_number}: content must be a string")
    if not isinstance(record["content_sha256"], str):
        raise InputValidationError(f"line {line_number}: content_sha256 must be a string")
    actual_hash = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
    if actual_hash != record["content_sha256"]:
        raise InputValidationError(f"line {line_number}: content_sha256 mismatch")


def validate_expected_count(expected_count):
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise InputValidationError("expected_count must be an integer")
    if not MIN_EXPECTED_COUNT <= expected_count <= MAX_EXPECTED_COUNT:
        raise InputValidationError(
            f"expected_count must be between {MIN_EXPECTED_COUNT} and {MAX_EXPECTED_COUNT}"
        )
    return expected_count


def load_and_validate_input(input_path, expected_count):
    expected_count = validate_expected_count(expected_count)
    path = Path(input_path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise InputValidationError(f"input file does not exist: {path}") from error
    if not stat.S_ISREG(mode):
        raise InputValidationError(f"input path must be a regular file: {path}")

    records = []
    seen_post_ids = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise InputValidationError(f"line {line_number}: blank JSONL records are not allowed")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise InputValidationError(f"line {line_number}: invalid JSON: {error.msg}") from error
            _validate_record(record, line_number, seen_post_ids)
            records.append(record)
    if len(records) != expected_count:
        raise InputValidationError(
            f"record count mismatch: expected {expected_count}, found {len(records)}"
        )
    return records


def load_config(config_path=None):
    path = Path(config_path) if config_path else PROJECT_ROOT / "config/classification.json"
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("classification config must be a JSON object")
    return config


def analyze_records(records, config):
    output = []
    for record in records:
        analysis = analyze_content(record["content"], config)
        eligibility = evaluate_phase1_eligibility(record, analysis)
        content_bytes = len(record["content"].encode("utf-8"))
        result = {
            "schema_version": 1,
            "post_id": record["post_id"],
            "published_at": record["published_at"],
            "modified_at": record["modified_at"],
            "content_bytes": content_bytes,
            "content_characters": len(record["content"]),
            "content_sha256": record["content_sha256"],
            "editor_format": analysis["editor_format"],
            "code_format": analysis["code_format"],
            "primary_format": analysis["primary_format"],
            "syntaxhighlighter_count": analysis["syntaxhighlighter_count"],
            "syntaxhighlighter_languages": analysis["syntaxhighlighter_languages"],
            "syntaxhighlighter_balanced": analysis["syntaxhighlighter_balanced"],
            "syntaxhighlighter_attributes_valid": analysis["syntaxhighlighter_attributes_valid"],
            "code_block_pro_count": analysis["code_block_pro_count"],
            "mixed_code_formats": analysis["mixed_code_formats"],
            "matched_rule_ids": analysis["matched_rule_ids"],
            "risk_level": analysis["risk_level"],
            "risk_reasons": analysis["risk_reasons"],
            "manual_review": analysis["risk_level"] == "manual-review",
            "phase1_status": eligibility["status"],
            "phase1_eligible": eligibility["eligible"],
            "phase1_exclusion_reasons": eligibility["exclusion_reasons"],
        }
        if FORBIDDEN_OUTPUT_FIELDS & result.keys():
            raise RuntimeError("analysis output contains forbidden source fields")
        output.append(result)
    return output


def validate_distinct_input_output(input_path, output_path):
    input_path = Path(input_path)
    output_path = Path(output_path)
    try:
        resolved_input = input_path.resolve(strict=False)
        resolved_output = output_path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise InputValidationError(f"unable to resolve input or output path: {error}") from error
    if resolved_input == resolved_output:
        raise InputValidationError("input and output must refer to different files")
    if os.path.lexists(output_path):
        try:
            if os.path.samefile(input_path, output_path):
                raise InputValidationError("input and output must refer to different files")
        except FileNotFoundError:
            pass
        except OSError as error:
            raise InputValidationError(f"unable to compare input and output paths: {error}") from error


def write_jsonl_atomically(output_path, records):
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_analysis(input_path, output_path, expected_count, config_path=None):
    validate_distinct_input_output(input_path, output_path)
    records = load_and_validate_input(input_path, expected_count)
    config = load_config(config_path)
    analyzed = analyze_records(records, config)
    write_jsonl_atomically(output_path, analyzed)
    return analyzed


def _print_summary(input_path, output_path, records):
    output = Path(output_path)
    print(f"Input file: {input_path}")
    print(f"Input records: {len(records)}")
    print(f"Output file: {output}")
    print(f"Output records: {len(records)}")
    print(f"Output bytes: {output.stat().st_size}")
    print(f"Output SHA-256: {hashlib.sha256(output.read_bytes()).hexdigest()}")
    for field in ("editor_format", "code_format", "primary_format", "risk_level"):
        counts = Counter(record[field] for record in records)
        print(f"{field} counts: {json.dumps(dict(sorted(counts.items())), ensure_ascii=False)}")
    print(f"phase1_eligible=true: {sum(record['phase1_eligible'] for record in records)}")
    print(f"manual_review=true: {sum(record['manual_review'] for record in records)}")
    for record in records:
        safe = {
            "post_id": record["post_id"],
            "published_at": record["published_at"],
            "editor_format": record["editor_format"],
            "code_format": record["code_format"],
            "primary_format": record["primary_format"],
            "risk_level": record["risk_level"],
            "phase1_status": record["phase1_status"],
            "phase1_exclusion_reasons": record["phase1_exclusion_reasons"],
        }
        print(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyze one validated, explicitly bounded JSONL export")
    parser.add_argument(
        "--expected-count",
        required=True,
        type=int,
        help=f"required input record count ({MIN_EXPECTED_COUNT}-{MAX_EXPECTED_COUNT})",
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", type=Path)
    arguments = parser.parse_args(argv)
    try:
        validate_expected_count(arguments.expected_count)
    except InputValidationError as error:
        parser.error(str(error))
    try:
        records = run_analysis(
            arguments.input,
            arguments.output,
            arguments.expected_count,
            arguments.config,
        )
    except InputValidationError as error:
        parser.error(str(error))
    _print_summary(arguments.input, arguments.output, records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
