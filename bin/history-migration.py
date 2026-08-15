#!/usr/bin/env python3
"""Read-only status view for fixed historical-article migration batches."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.candidate_execution import SafetyError


SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_INTEGRITY_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_LOCK_CONFLICT = 3
EXIT_WRITE_ERROR = 4

STATE_ROOT = Path("data/state/history-migration")
STATE_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
INIT_EVENT_TYPE = "coordination_state_initialized"
COORDINATION_STATUSES = {
    "awaiting_manual_conversion", "awaiting_manual_review", "awaiting_validation",
    "awaiting_readonly_validation", "ready_for_execution",
    "execution_in_progress",
    "validation_failed", "ready_for_excerpt", "excerpt_failed",
    "ready_for_translation_resume", "translation_failed", "completed", "blocked",
    "paused",
}
MAX_RUN_ATTEMPTS = 2
MAX_RESUME_ATTEMPTS = 3
MAX_ARTICLE_ATTEMPTS = 3
ARTICLE_RETRY_DELAY = 5
SUBPROCESS_SUMMARY_LIMIT = 4000
VALIDATION_ROOT = Path("data/analysis/history-migration-validation")
EXECUTION_MANIFEST_FIELDS = (
    "chinese_post_id", "chinese_title", "chinese_content_sha256",
    "chinese_excerpt_empty", "english_post_id", "english_post_status",
    "english_title_sha256", "english_excerpt_sha256", "english_content_sha256",
    "candidate_reason", "execution_status", "chinese_post_status",
    "chinese_language", "source_migration_type",
    "expected_code_block_pro_count", "expected_syntaxhighlighter_count",
)

LEGACY_BATCH = {
    "batch_id": "gutenberg-cbp-fixed-42",
    "relative_path": "data/analysis/gutenberg-cbp-empty-excerpt-candidates.csv",
    "source_type": "gutenberg_code_block_pro",
    "expected_count": 42,
}
PILOT_BATCH = {
    "batch_id": "syntaxhighlighter-pilot-17586",
    "relative_path":
        "data/analysis/gutenberg-syntaxhighlighter-migration-pilot-candidates.csv",
    "source_type": "syntaxhighlighter_pilot",
    "expected_count": 1,
}
SYNTAX_GLOB = "syntaxhighlighter-migration-batch-*.csv"
MIXED_SYNTAX_GLOB = "mixed-syntaxhighlighter-migration-batch-*.csv"
SPECIAL_MIXED_SYNTAX_GLOB = "mixed-syntaxhighlighter-special-batch-*.csv"
DEFAULT_SYNTAX_BATCH_EXPECTED_COUNT = 20
SYNTAX_BATCH_EXPECTED_COUNTS = {
    "syntaxhighlighter-20260722-01": 20,
    "syntaxhighlighter-20260723-01": 20,
    "syntaxhighlighter-priority-20260724-01": 5,
    "syntaxhighlighter-20260728-01": 12,
}
SYNTAX_FIXED_FIELDS = {
    "schema_version", "batch_id", "batch_sequence", "allocated_at",
    "chinese_post_id", "english_post_id", "chinese_title", "published_at",
    "before_content_sha256", "before_syntaxhighlighter_count",
    "before_code_block_pro_count", "migration_status", "validation_status",
}
SYNTAX_BATCH_EXPECTED_COUNT_FIELD = "batch_expected_count"
MIXED_SYNTAX_FIXED_FIELDS = (
    SYNTAX_FIXED_FIELDS
    - {"before_content_sha256"}
    | {
        "source_editor_format", "target_editor_format",
        "source_migration_type", "source_type", "content_sha256",
        "snapshot_id", "snapshot_generated_at",
    }
)
SYNTAX_SOURCE_TYPES = {
    "syntaxhighlighter_daily", "mixed_syntaxhighlighter_daily",
    "mixed_syntaxhighlighter_special",
}
MIXED_SYNTAX_SOURCE_TYPES = {
    "mixed_syntaxhighlighter_daily", "mixed_syntaxhighlighter_special",
}
MIXED_SOURCE_MIGRATION_TYPE = (
    "mixed-syntaxhighlighter-to-gutenberg-code-block-pro"
)
MANIFEST_FIELDS = {
    "chinese_post_id", "english_post_id", "chinese_title", "execution_status",
}
VALIDATION_FIELDS = {
    "batch_id", "chinese_post_id", "english_post_id", "validation_status",
}
RECORD_VALIDATION_FIELDS = {
    "batch_id", "chinese_post_id", "english_post_id",
    "before_content_sha256", "after_content_sha256",
    "before_syntaxhighlighter_count", "after_syntaxhighlighter_count",
    "before_code_block_pro_count", "expected_code_block_pro_count_after",
    "after_code_block_pro_count", "chinese_excerpt_empty", "chinese_status",
    "chinese_language", "english_status", "polylang_relation_status",
    "gutenberg_balanced", "validation_status", "validation_reasons",
}
EXECUTION_CANDIDATE_FIELDS = {
    "chinese_post_id", "english_post_id", "chinese_title", "execution_status",
}
KNOWN_EXECUTION_STATUSES = {
    "prepared", "excerpt_rejected", "rejected_excerpt_generation",
    "excerpt_generation_failed", "excerpt_generated", "chinese_excerpt_saved",
    "translation_started", "translation_failed", "completed", "failed", "pending",
}
DERIVED_SUFFIXES = ("-validation.csv", "-execution-candidates.csv")
EXECUTION_NAME = re.compile(r"^chinese-(?P<post_id>[1-9][0-9]*)\.execution\.json$")
RESTART_ARCHIVE_ROOT = Path("data/backups/single-candidate/recovery-history")


class ReadError(ValueError):
    pass


def repository_root():
    return Path(__file__).resolve().parents[1]


def _relative(path, root):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _read_csv(path):
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ReadError(f"{path}: CSV header is missing")
            return list(reader), set(reader.fieldnames)
    except (OSError, UnicodeError, csv.Error) as error:
        raise ReadError(f"{path}: cannot read CSV: {error}") from error


def _positive_id(row, field, path, position):
    value = row.get(field)
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ReadError(f"{path}: row {position}: invalid {field}: {value!r}") from error
    if result < 1 or str(value).strip() != str(result):
        raise ReadError(f"{path}: row {position}: invalid {field}: {value!r}")
    return result


def _required(fields, required, path):
    missing = sorted(required - fields)
    if missing:
        raise ReadError(f"{path}: missing required fields: {', '.join(missing)}")


def _article(row, position, path, root):
    chinese_id = _positive_id(row, "chinese_post_id", path, position)
    english_id = _positive_id(row, "english_post_id", path, position)
    return {
        "chinese_post_id": chinese_id,
        "english_post_id": english_id,
        "title": row.get("chinese_title", ""),
        "published_at": row.get("published_at") or None,
        "batch_position": position,
        "source_file": _relative(path, root),
        "source_row": dict(row),
    }


def _load_manifest_batch(root, definition):
    path = root / definition["relative_path"]
    rows, fields = _read_csv(path)
    _required(fields, MANIFEST_FIELDS, path)
    articles = [_article(row, position, path, root)
                for position, row in enumerate(rows, 1)]
    return {
        "batch_id": definition["batch_id"],
        "source_file": definition["relative_path"],
        "source_type": definition["source_type"],
        "expected_count": definition["expected_count"],
        "batch_sequence": None,
        "allocated_at": None,
        "articles": articles,
        "errors": [],
    }


def _mixed_batch_filename_contract(path, source_type):
    if source_type == "mixed_syntaxhighlighter_daily":
        prefix = "mixed-syntaxhighlighter-migration-batch-"
        batch_prefix = "mixed-syntaxhighlighter-"
    elif source_type == "mixed_syntaxhighlighter_special":
        prefix = "mixed-syntaxhighlighter-special-batch-"
        batch_prefix = "mixed-syntaxhighlighter-special-"
    else:
        raise ReadError(f"unsupported mixed source type: {source_type}")
    suffix = path.name[len(prefix):-len(".csv")]
    return batch_prefix + suffix


def _load_syntax_batch(path, root, source_type="syntaxhighlighter_daily"):
    rows, fields = _read_csv(path)
    required = (
        MIXED_SYNTAX_FIXED_FIELDS
        if source_type in MIXED_SYNTAX_SOURCE_TYPES
        else SYNTAX_FIXED_FIELDS
    )
    _required(fields, required, path)
    if not rows:
        raise ReadError(f"{path}: fixed batch is empty")
    if source_type in MIXED_SYNTAX_SOURCE_TYPES:
        expected_values = {
            "source_type": source_type,
            "target_editor_format": "gutenberg",
            "source_migration_type": MIXED_SOURCE_MIGRATION_TYPE,
        }
        for field, expected in expected_values.items():
            if any(row.get(field, "").strip() != expected for row in rows):
                raise ReadError(
                    f"{path}: {field} must be {expected!r} in every row")
        allowed_source_editors = {"classic", "gutenberg", "mixed", "unknown"}
        if any(row.get("source_editor_format", "").strip()
               not in allowed_source_editors for row in rows):
            raise ReadError(
                f"{path}: source_editor_format must be a known editor format")
        for position, row in enumerate(rows, 1):
            content_sha256 = row.get("content_sha256", "").strip()
            if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
                raise ReadError(
                    f"{path}: row {position}: content_sha256 must be lowercase SHA-256")
            if not row.get("snapshot_id", "").strip():
                raise ReadError(f"{path}: row {position}: snapshot_id is required")
            generated = row.get("snapshot_generated_at", "").strip()
            try:
                datetime.fromisoformat(generated.replace("Z", "+00:00"))
            except ValueError as error:
                raise ReadError(
                    f"{path}: row {position}: invalid snapshot_generated_at"
                ) from error
            row["before_content_sha256"] = content_sha256
    batch_ids = {row.get("batch_id", "").strip() for row in rows}
    sequences = {row.get("batch_sequence", "").strip() for row in rows}
    allocated = {row.get("allocated_at", "").strip() for row in rows}
    if len(batch_ids) != 1 or not next(iter(batch_ids)):
        raise ReadError(f"{path}: batch_id must be non-empty and identical in every row")
    if source_type in MIXED_SYNTAX_SOURCE_TYPES:
        expected_batch_id = _mixed_batch_filename_contract(path, source_type)
        if next(iter(batch_ids)) != expected_batch_id:
            raise ReadError(
                f"{path}: batch_id must match filename: {expected_batch_id}")
        snapshot_ids = {row["snapshot_id"].strip() for row in rows}
        snapshot_times = {
            row["snapshot_generated_at"].strip() for row in rows
        }
        if len(snapshot_ids) != 1 or len(snapshot_times) != 1:
            raise ReadError(
                f"{path}: snapshot identity must be identical in every row")
    if len(sequences) != 1:
        raise ReadError(f"{path}: batch_sequence must be identical in every row")
    try:
        sequence = int(next(iter(sequences)))
    except ValueError as error:
        raise ReadError(f"{path}: invalid batch_sequence") from error
    if sequence < 1:
        raise ReadError(f"{path}: invalid batch_sequence")
    if len(allocated) != 1 or not next(iter(allocated)):
        raise ReadError(f"{path}: allocated_at must be non-empty and identical in every row")
    articles = [_article(row, position, path, root)
                for position, row in enumerate(rows, 1)]
    batch_id = next(iter(batch_ids))
    if SYNTAX_BATCH_EXPECTED_COUNT_FIELD in fields:
        expected_values = {
            row.get(SYNTAX_BATCH_EXPECTED_COUNT_FIELD, "").strip() for row in rows
        }
        if len(expected_values) != 1:
            raise ReadError(
                f"{path}: {SYNTAX_BATCH_EXPECTED_COUNT_FIELD} must be identical "
                "in every row"
            )
        expected_value = next(iter(expected_values))
        try:
            expected_count = int(expected_value)
        except ValueError as error:
            raise ReadError(
                f"{path}: invalid {SYNTAX_BATCH_EXPECTED_COUNT_FIELD}: "
                f"{expected_value!r}"
            ) from error
        if expected_count < 1 or str(expected_count) != expected_value:
            raise ReadError(
                f"{path}: invalid {SYNTAX_BATCH_EXPECTED_COUNT_FIELD}: "
                f"{expected_value!r}"
            )
    else:
        expected_count = SYNTAX_BATCH_EXPECTED_COUNTS.get(
            batch_id, DEFAULT_SYNTAX_BATCH_EXPECTED_COUNT)
    return {
        "batch_id": batch_id,
        "source_file": _relative(path, root),
        "source_type": source_type,
        "expected_count": expected_count,
        "batch_sequence": sequence,
        "allocated_at": next(iter(allocated)),
        "articles": articles,
        "errors": [],
    }


def discover_batches(root, errors):
    batches = []
    for definition in (LEGACY_BATCH, PILOT_BATCH):
        try:
            batches.append(_load_manifest_batch(root, definition))
        except ReadError as error:
            errors.append(str(error))
    analysis = root / "data/analysis"
    if not analysis.is_dir():
        errors.append(f"{analysis}: analysis directory is missing")
        return batches
    for pattern, source_type in (
            (SYNTAX_GLOB, "syntaxhighlighter_daily"),
            (MIXED_SYNTAX_GLOB, "mixed_syntaxhighlighter_daily"),
            (SPECIAL_MIXED_SYNTAX_GLOB, "mixed_syntaxhighlighter_special")):
        for path in sorted(analysis.glob(pattern), key=lambda item: item.name):
            if path.name.endswith(DERIVED_SUFFIXES):
                continue
            try:
                batches.append(_load_syntax_batch(path, root, source_type))
            except ReadError as error:
                errors.append(str(error))
    batches.sort(key=lambda batch: (
        batch["batch_sequence"] is not None,
        batch["batch_sequence"] if batch["batch_sequence"] is not None else 0,
        batch["batch_id"],
        batch["source_file"],
    ))
    return batches


def validate_batch_index(batches, conflicts, errors):
    global_index = {}
    batch_ids = {}
    for batch in batches:
        if batch["batch_id"] in batch_ids:
            conflicts.append({
                "type": "duplicate_batch_id",
                "batch_id": batch["batch_id"],
                "source_files": [batch_ids[batch["batch_id"]], batch["source_file"]],
            })
        else:
            batch_ids[batch["batch_id"]] = batch["source_file"]
        if len(batch["articles"]) != batch["expected_count"]:
            message = (
                f"{batch['source_file']}: expected {batch['expected_count']} fixed articles, "
                f"found {len(batch['articles'])}"
            )
            batch["errors"].append(message)
            errors.append(message)
        local = {}
        for article in batch["articles"]:
            post_id = article["chinese_post_id"]
            if post_id in local:
                conflicts.append({
                    "type": "duplicate_chinese_post_id_within_batch",
                    "chinese_post_id": post_id,
                    "batch_id": batch["batch_id"],
                    "positions": [local[post_id]["batch_position"],
                                  article["batch_position"]],
                })
            else:
                local[post_id] = article
            if post_id in global_index:
                previous = global_index[post_id]
                conflict = {
                    "type": "duplicate_chinese_post_id_across_batches",
                    "chinese_post_id": post_id,
                    "assignments": [
                        {
                            "batch_id": previous["batch_id"],
                            "source_file": previous["source_file"],
                            "english_post_id": previous["english_post_id"],
                        },
                        {
                            "batch_id": batch["batch_id"],
                            "source_file": article["source_file"],
                            "english_post_id": article["english_post_id"],
                        },
                    ],
                }
                if previous["english_post_id"] != article["english_post_id"]:
                    conflict["english_mapping_conflict"] = True
                conflicts.append(conflict)
            else:
                global_index[post_id] = {
                    **article,
                    "batch_id": batch["batch_id"],
                }
    return global_index


def read_execution_states(root, errors):
    directory = root / "data/backups/single-candidate"
    states = {}
    if not directory.exists():
        return states
    for path in sorted(directory.glob("chinese-*.execution.json"), key=lambda item: item.name):
        match = EXECUTION_NAME.fullmatch(path.name)
        if not match:
            continue
        filename_id = int(match.group("post_id"))
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(f"{path}: invalid execution JSON: {error}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{path}: execution JSON must be an object")
            continue
        try:
            chinese_id = _positive_id(value, "chinese_post_id", path, 1)
            english_id = _positive_id(value, "english_post_id", path, 1)
        except ReadError as error:
            errors.append(str(error))
            continue
        status = value.get("status")
        if chinese_id != filename_id:
            errors.append(
                f"{path}: chinese_post_id {chinese_id} does not match filename {filename_id}")
            continue
        if not isinstance(status, str) or not status.strip():
            errors.append(f"{path}: execution status is missing or invalid")
            continue
        states[chinese_id] = {
            "chinese_post_id": chinese_id,
            "english_post_id": english_id,
            "status": status.strip(),
            "known_status": status.strip() in KNOWN_EXECUTION_STATUSES,
            "source_file": _relative(path, root),
        }
    return states


def _derived_batch_id(path, suffix):
    stem = path.name[:-len(suffix)]
    for prefix, batch_prefix in (
            ("syntaxhighlighter-migration-batch-", "syntaxhighlighter-"),
            ("mixed-syntaxhighlighter-migration-batch-",
             "mixed-syntaxhighlighter-"),
            ("mixed-syntaxhighlighter-special-batch-",
             "mixed-syntaxhighlighter-special-")):
        if stem.startswith(prefix):
            return batch_prefix + stem[len(prefix):]
    return None


def read_validation_evidence(root, batches, errors):
    by_batch = {batch["batch_id"]: batch for batch in batches}
    evidence = {}
    analysis = root / "data/analysis"
    for path in sorted(analysis.glob("*-validation.csv"), key=lambda item: item.name):
        inferred = _derived_batch_id(path, "-validation.csv")
        if inferred is None:
            continue
        try:
            rows, fields = _read_csv(path)
            _required(fields, VALIDATION_FIELDS, path)
            row_batch_ids = {row.get("batch_id", "").strip() for row in rows}
            if row_batch_ids != {inferred}:
                raise ReadError(
                    f"{path}: validation batch_id does not match filename: "
                    f"{sorted(row_batch_ids)!r}")
            seen = set()
            statuses = Counter()
            for position, row in enumerate(rows, 1):
                chinese_id = _positive_id(row, "chinese_post_id", path, position)
                _positive_id(row, "english_post_id", path, position)
                if chinese_id in seen:
                    raise ReadError(
                        f"{path}: duplicate validation chinese_post_id: {chinese_id}")
                seen.add(chinese_id)
                status = row.get("validation_status", "").strip() or "unknown"
                statuses[status] += 1
            if inferred not in by_batch:
                raise ReadError(f"{path}: validation references unknown fixed batch {inferred}")
            fixed_ids = {
                article["chinese_post_id"] for article in by_batch[inferred]["articles"]
            }
            if seen != fixed_ids:
                raise ReadError(f"{path}: validation article IDs do not match fixed batch")
            evidence[inferred] = {
                "source_file": _relative(path, root),
                "count": len(rows),
                "counts": dict(sorted(statuses.items())),
            }
        except ReadError as error:
            errors.append(str(error))
    return evidence


def read_execution_candidates(root, batches, errors):
    by_batch = {batch["batch_id"]: batch for batch in batches}
    evidence = {}
    analysis = root / "data/analysis"
    for path in sorted(
            analysis.glob("*-execution-candidates.csv"), key=lambda item: item.name):
        inferred = _derived_batch_id(path, "-execution-candidates.csv")
        if inferred is None:
            continue
        try:
            rows, fields = _read_csv(path)
            _required(fields, EXECUTION_CANDIDATE_FIELDS, path)
            seen = set()
            for position, row in enumerate(rows, 1):
                chinese_id = _positive_id(row, "chinese_post_id", path, position)
                _positive_id(row, "english_post_id", path, position)
                if chinese_id in seen:
                    raise ReadError(
                        f"{path}: duplicate execution-candidate chinese_post_id: {chinese_id}")
                seen.add(chinese_id)
            if inferred not in by_batch:
                raise ReadError(
                    f"{path}: execution candidates reference unknown fixed batch {inferred}")
            fixed_ids = {
                article["chinese_post_id"] for article in by_batch[inferred]["articles"]
            }
            if seen != fixed_ids:
                raise ReadError(
                    f"{path}: execution-candidate article IDs do not match fixed batch")
            evidence[inferred] = {
                "source_file": _relative(path, root),
                "count": len(rows),
            }
        except ReadError as error:
            errors.append(str(error))
    return evidence


def _execution_bucket(status):
    if status == "completed":
        return "completed"
    if status == "translation_started":
        return "translation_started"
    if status == "pending":
        return "pending"
    if status == "failed" or status.endswith("_failed"):
        return "failed"
    return "other"


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path):
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ReadError(f"{path}: cannot calculate SHA-256: {error}") from error
    return digest.hexdigest()


def _row_sha256(row):
    encoded = json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _state_path(root, batch_id, chinese_post_id):
    return root / STATE_ROOT / batch_id / f"chinese-{chinese_post_id}.json"


def _events_path(root, batch_id):
    return root / STATE_ROOT / batch_id / "events.jsonl"


def _execution_reference(root, state):
    if state is None:
        return None
    path = root / state["source_file"]
    return {
        "source_file": state["source_file"],
        "sha256": _file_sha256(path),
        "status": state["status"],
    }


def _validation_rows(root, errors):
    result = {}
    analysis = root / "data/analysis"
    if not analysis.is_dir():
        return result
    for path in sorted(analysis.glob("*-validation.csv"), key=lambda item: item.name):
        inferred = _derived_batch_id(path, "-validation.csv")
        if inferred is None:
            continue
        try:
            rows, fields = _read_csv(path)
            _required(fields, VALIDATION_FIELDS, path)
            digest = _file_sha256(path)
            mapped = {}
            for position, row in enumerate(rows, 1):
                post_id = _positive_id(row, "chinese_post_id", path, position)
                if post_id in mapped:
                    raise ReadError(
                        f"{path}: duplicate validation chinese_post_id: {post_id}")
                mapped[post_id] = {
                    "source_file": _relative(path, root),
                    "sha256": digest,
                    "status": row.get("validation_status", "").strip() or "unknown",
                    "validated_at": row.get("validated_at") or None,
                }
            result[inferred] = mapped
        except ReadError as error:
            errors.append(str(error))
    return result


def _workflow_mapping(batch, execution, validation):
    """Map only facts represented by current repository evidence."""
    if execution is not None:
        status = execution["status"]
        mapping = {
            "completed": "completed",
            "translation_started": "ready_for_translation_resume",
            "chinese_excerpt_saved": "ready_for_translation_resume",
            "translation_failed": "translation_failed",
            "excerpt_rejected": "excerpt_failed",
            "rejected_excerpt_generation": "excerpt_failed",
            "excerpt_generation_failed": "excerpt_failed",
            "prepared": "blocked",
            "excerpt_generated": "blocked",
            "failed": "blocked",
            "pending": "blocked",
        }
        if status not in mapping:
            raise ReadError(
                f"cannot safely map execution status {status!r} for "
                f"Chinese post {execution['chinese_post_id']}")
        return mapping[status], True, (
            f"imported from existing execution evidence with status={status}")
    if batch["source_type"] in SYNTAX_SOURCE_TYPES and validation is None:
        return "awaiting_manual_conversion", False, (
            "fixed SyntaxHighlighter article has no validation or execution evidence")
    raise ReadError(
        f"cannot safely initialize Chinese post without execution evidence: "
        f"{batch['batch_id']}")


def _manual_evidence(batch, legacy_import):
    if not legacy_import:
        evidence = {
            "manual_conversion": {"status": "not_recorded"},
            "language_review": {"status": "not_recorded"},
        }
        if batch["source_type"] in MIXED_SYNTAX_SOURCE_TYPES:
            evidence["gutenberg_normalization"] = {"status": "not_recorded"}
        return evidence
    if batch["source_type"] == "gutenberg_code_block_pro":
        conversion = "not_applicable"
    else:
        conversion = "historical_unrecorded"
    return {
        "manual_conversion": {"status": conversion},
        "language_review": {"status": "historical_unrecorded"},
    }


def _event_id(batch_id, post_id, batch_sha256, row_sha256):
    identity = (
        f"init-state-v1|{batch_id}|{post_id}|{batch_sha256}|{row_sha256}"
    ).encode("utf-8")
    return _sha256_bytes(identity)


def _build_expected_states(root, batches, executions, validation_by_post,
                           initialized_at, errors):
    expected = []
    for batch in batches:
        source = root / batch["source_file"]
        try:
            batch_sha256 = _file_sha256(source)
        except ReadError as error:
            errors.append(str(error))
            continue
        for article in batch["articles"]:
            post_id = article["chinese_post_id"]
            execution = executions.get(post_id)
            validation = validation_by_post.get(batch["batch_id"], {}).get(post_id)
            try:
                workflow_status, legacy_import, reason = _workflow_mapping(
                    batch, execution, validation)
            except ReadError as error:
                errors.append(str(error))
                continue
            row_digest = _row_sha256(article["source_row"])
            evidence = {
                "execution": _execution_reference(root, execution),
                "validation": validation,
            }
            state = {
                "schema_version": STATE_SCHEMA_VERSION,
                "batch_id": batch["batch_id"],
                "chinese_post_id": post_id,
                "english_post_id": article["english_post_id"],
                "batch_position": article["batch_position"],
                "source_batch_file": batch["source_file"],
                "source_batch_sha256": batch_sha256,
                "source_row_sha256": row_digest,
                "workflow_status": workflow_status,
                "legacy_import": legacy_import,
                **_manual_evidence(batch, legacy_import),
                "validation_evidence": evidence["validation"],
                "execution_evidence": evidence["execution"],
                "blocked_reasons": (
                    [reason] if workflow_status == "blocked" else []),
                "retry_counts": {},
                "initialization_reason": reason,
                "initialized_at": initialized_at,
                "updated_at": initialized_at,
            }
            event = {
                "schema_version": EVENT_SCHEMA_VERSION,
                "event_id": _event_id(
                    batch["batch_id"], post_id, batch_sha256, row_digest),
                "event_type": INIT_EVENT_TYPE,
                "occurred_at": initialized_at,
                "batch_id": batch["batch_id"],
                "chinese_post_id": post_id,
                "previous_status": None,
                "new_status": workflow_status,
                "reason": reason,
                "evidence": evidence,
                "legacy_import": legacy_import,
            }
            expected.append({
                "batch_id": batch["batch_id"],
                "article": article,
                "state": state,
                "event": event,
                "path": _state_path(root, batch["batch_id"], post_id),
            })
    return expected


def _read_state_file(path):
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReadError(f"{path}: invalid coordination state JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReadError(f"{path}: coordination state must be an object")
    return value


def _read_events(path):
    if not path.exists():
        return []
    events = []
    seen = set()
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise ReadError(f"{path}: blank event at line {line_number}")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ReadError(
                        f"{path}: invalid event JSON at line {line_number}: {error}"
                    ) from error
                if not isinstance(event, dict) or not isinstance(
                        event.get("event_id"), str):
                    raise ReadError(
                        f"{path}: invalid event object at line {line_number}")
                if event["event_id"] in seen:
                    raise ReadError(
                        f"{path}: duplicate event_id {event['event_id']}")
                seen.add(event["event_id"])
                events.append(event)
    except (OSError, UnicodeError) as error:
        raise ReadError(f"{path}: cannot read events: {error}") from error
    return events


STATE_IDENTITY_FIELDS = (
    "schema_version", "batch_id", "chinese_post_id", "english_post_id",
    "batch_position", "source_batch_file", "source_batch_sha256",
    "source_row_sha256",
)


def _state_identity_conflicts(existing, expected):
    return [
        field for field in STATE_IDENTITY_FIELDS
        if existing.get(field) != expected.get(field)
    ]


def _atomic_write(path, payload, *, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_write_json(path, value):
    _atomic_write(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _atomic_write_events(path, events):
    payload = "".join(
        json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        for event in events
    )
    _atomic_write(path, payload)


class InitLock:
    def __init__(self, root):
        self.path = root / STATE_ROOT / ".init-state.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.handle.close()
            self.handle = None
            raise ReadError(f"{self.path}: init-state lock is already held") from error
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _plan_init(root, initialized_at=None):
    root = Path(root).resolve()
    errors = []
    conflicts = []
    batches = discover_batches(root, errors)
    fixed_index = validate_batch_index(batches, conflicts, errors)
    executions = read_execution_states(root, errors)
    read_validation_evidence(root, batches, errors)
    read_execution_candidates(root, batches, errors)
    validation_by_post = _validation_rows(root, errors)
    timestamp = initialized_at or datetime.now(timezone.utc).isoformat()
    expected = _build_expected_states(
        root, batches, executions, validation_by_post, timestamp, errors)
    batch_counts = {
        batch["batch_id"]: {
            "batch_id": batch["batch_id"],
            "source_file": batch["source_file"],
            "planned_count": 0,
            "created_count": 0,
            "unchanged_count": 0,
            "legacy_import_count": 0,
            "awaiting_manual_conversion_count": 0,
        }
        for batch in batches
    }
    actions = []
    for item in expected:
        state = item["state"]
        summary = batch_counts[item["batch_id"]]
        summary["planned_count"] += 1
        summary["legacy_import_count"] += int(state["legacy_import"])
        summary["awaiting_manual_conversion_count"] += int(
            state["workflow_status"] == "awaiting_manual_conversion")
        path = item["path"]
        if not path.exists():
            action = "create"
        else:
            try:
                existing = _read_state_file(path)
                differing = _state_identity_conflicts(existing, state)
                if differing:
                    conflicts.append({
                        "type": "coordination_state_identity_conflict",
                        "source_file": _relative(path, root),
                        "chinese_post_id": state["chinese_post_id"],
                        "fields": differing,
                    })
                    action = "conflict"
                else:
                    action = "unchanged"
            except ReadError as error:
                errors.append(str(error))
                action = "error"
        actions.append({**item, "action": action})
    if len(expected) != len(fixed_index):
        errors.append(
            f"initializable article count mismatch: fixed={len(fixed_index)} "
            f"planned={len(expected)}")
    for batch_id, summary in batch_counts.items():
        event_path = _events_path(root, batch_id)
        try:
            events = _read_events(event_path)
            event_ids = {event["event_id"] for event in events}
            for item in (value for value in actions if value["batch_id"] == batch_id):
                if item["action"] == "unchanged" and (
                        item["event"]["event_id"] not in event_ids):
                    errors.append(
                        f"{event_path}: initialization event missing for "
                        f"Chinese post {item['state']['chinese_post_id']}")
        except ReadError as error:
            errors.append(str(error))
    return {
        "root": root,
        "batches_raw": batches,
        "actions": actions,
        "batch_summaries": list(batch_counts.values()),
        "fixed_article_count": len(fixed_index),
        "errors": errors,
        "conflicts": conflicts,
        "timestamp": timestamp,
    }


def _init_result(plan, mode, writes_performed=False):
    actions = plan["actions"]
    created = sum(item["action"] == "create" for item in actions)
    unchanged = sum(item["action"] == "unchanged" for item in actions)
    batches = []
    for summary in plan["batch_summaries"]:
        batch_actions = [
            item for item in actions if item["batch_id"] == summary["batch_id"]]
        value = dict(summary)
        value["created_count"] = (
            sum(item["action"] == "create" for item in batch_actions)
            if mode == "apply" else 0
        )
        value["would_create_count"] = sum(
            item["action"] == "create" for item in batch_actions)
        value["unchanged_count"] = sum(
            item["action"] == "unchanged" for item in batch_actions)
        batches.append(value)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "repository_root": str(plan["root"]),
        "fixed_batch_count": len(plan["batches_raw"]),
        "fixed_article_count": plan["fixed_article_count"],
        "planned_count": len(actions),
        "created_count": created if mode == "apply" else 0,
        "unchanged_count": unchanged,
        "would_create_count": created,
        "legacy_import_count": sum(
            item["state"]["legacy_import"] for item in actions),
        "awaiting_manual_conversion_count": sum(
            item["state"]["workflow_status"] == "awaiting_manual_conversion"
            for item in actions),
        "conflicts": plan["conflicts"],
        "errors": plan["errors"],
        "batches": batches,
        "writes_performed": writes_performed,
        "integrity_ok": not plan["errors"] and not plan["conflicts"],
    }


def init_state(root, apply=False):
    root = Path(root).resolve()
    if not apply:
        return _init_result(_plan_init(root), "preview")
    try:
        with InitLock(root):
            plan = _plan_init(root)
            if plan["errors"] or plan["conflicts"]:
                return _init_result(plan, "apply")
            created_items = [
                item for item in plan["actions"] if item["action"] == "create"]
            preexisting_unchanged = sum(
                item["action"] == "unchanged" for item in plan["actions"])
            events_by_batch = {}
            for batch in plan["batches_raw"]:
                event_path = _events_path(root, batch["batch_id"])
                events_by_batch[batch["batch_id"]] = _read_events(event_path)
            for batch_id, events in events_by_batch.items():
                existing_ids = {event["event_id"] for event in events}
                additions = [
                    item["event"] for item in created_items
                    if item["batch_id"] == batch_id
                    and item["event"]["event_id"] not in existing_ids
                ]
                if additions:
                    _atomic_write_events(
                        _events_path(root, batch_id), events + additions)
            for item in created_items:
                _atomic_write_json(item["path"], item["state"])
            final_plan = _plan_init(root, initialized_at=plan["timestamp"])
            result = _init_result(
                final_plan, "apply", writes_performed=bool(created_items))
            result["created_count"] = len(created_items)
            result["unchanged_count"] = preexisting_unchanged
            result["would_create_count"] = 0
            for batch in result["batches"]:
                batch_created = sum(
                    item["batch_id"] == batch["batch_id"]
                    for item in created_items)
                batch["created_count"] = batch_created
                batch["unchanged_count"] = sum(
                    item["batch_id"] == batch["batch_id"]
                    and item["action"] == "unchanged"
                    for item in plan["actions"])
            return result
    except ReadError as error:
        plan = _plan_init(root)
        plan["errors"].append(str(error))
        result = _init_result(plan, "apply")
        result["exit_code"] = EXIT_LOCK_CONFLICT
        return result
    except (OSError, UnicodeError) as error:
        plan = _plan_init(root)
        plan["errors"].append(f"init-state write failed: {error}")
        result = _init_result(plan, "apply")
        result["exit_code"] = EXIT_WRITE_ERROR
        return result


def read_coordination_states(root, batches, errors):
    by_fixed_id = {}
    batch_by_id = {batch["batch_id"]: batch for batch in batches}
    current_hashes = {}
    for batch in batches:
        try:
            current_hashes[batch["batch_id"]] = _file_sha256(
                root / batch["source_file"])
        except ReadError as error:
            errors.append(str(error))
    state_root = root / STATE_ROOT
    status_counts = Counter()
    legacy_count = 0
    drift = []
    if state_root.exists():
        for path in sorted(state_root.glob("*/chinese-*.json")):
            try:
                state = _read_state_file(path)
                for field in STATE_IDENTITY_FIELDS + (
                        "workflow_status", "legacy_import"):
                    if field not in state:
                        raise ReadError(
                            f"{path}: coordination state missing field {field}")
                post_id = _positive_id(state, "chinese_post_id", path, 1)
                english_id = _positive_id(state, "english_post_id", path, 1)
                batch_id = state["batch_id"]
                if batch_id not in batch_by_id:
                    raise ReadError(
                        f"{path}: coordination state references unknown batch {batch_id}")
                if post_id in by_fixed_id:
                    raise ReadError(
                        f"{path}: duplicate coordination state for Chinese post {post_id}")
                fixed = next(
                    (article for article in batch_by_id[batch_id]["articles"]
                     if article["chinese_post_id"] == post_id), None)
                if fixed is None:
                    raise ReadError(
                        f"{path}: Chinese post is absent from fixed batch")
                checks = {
                    "english_post_id": english_id == fixed["english_post_id"],
                    "batch_position": state["batch_position"] == fixed["batch_position"],
                    "source_batch_file":
                        state["source_batch_file"] == fixed["source_file"],
                    "source_row_sha256":
                        state["source_row_sha256"] == _row_sha256(fixed["source_row"]),
                }
                differing = [field for field, passed in checks.items() if not passed]
                if differing:
                    raise ReadError(
                        f"{path}: coordination identity mismatch: "
                        + ",".join(differing))
                current_hash = current_hashes.get(batch_id)
                if state["source_batch_sha256"] != current_hash:
                    drift.append({
                        "batch_id": batch_id,
                        "source_file": fixed["source_file"],
                        "chinese_post_id": post_id,
                        "expected_sha256": state["source_batch_sha256"],
                        "actual_sha256": current_hash,
                    })
                workflow = state["workflow_status"]
                if workflow not in COORDINATION_STATUSES:
                    raise ReadError(
                        f"{path}: unknown workflow_status {workflow!r}")
                if type(state["legacy_import"]) is not bool:
                    raise ReadError(f"{path}: legacy_import must be boolean")
                status_counts[workflow] += 1
                legacy_count += int(state["legacy_import"])
                by_fixed_id[post_id] = state
            except ReadError as error:
                errors.append(str(error))
        for batch in batches:
            try:
                _read_events(_events_path(root, batch["batch_id"]))
            except ReadError as error:
                errors.append(str(error))
    return {
        "states": by_fixed_id,
        "coordination_status_counts": dict(sorted(status_counts.items())),
        "legacy_import_count": legacy_count,
        "batch_drift": drift,
    }


def _latest_incomplete(batches):
    incomplete = [batch for batch in batches if batch["incomplete_count"] > 0]
    if not incomplete:
        return None
    if any(batch["batch_sequence"] is None or not batch["allocated_at"]
           for batch in incomplete):
        return {
            "status": "undetermined",
            "reason": "one or more incomplete batches lack comparable allocation metadata",
            "batch_id": None,
        }
    latest = max(
        incomplete,
        key=lambda batch: (batch["allocated_at"], batch["batch_sequence"],
                           batch["batch_id"]),
    )
    return {
        "status": "determined",
        "reason": None,
        "batch_id": latest["batch_id"],
        "source_file": latest["source_file"],
    }


def build_status(root):
    root = Path(root).resolve()
    errors = []
    conflicts = []
    batches = discover_batches(root, errors)
    fixed_index = validate_batch_index(batches, conflicts, errors)
    executions = read_execution_states(root, errors)
    validation = read_validation_evidence(root, batches, errors)
    execution_candidates = read_execution_candidates(root, batches, errors)
    state_errors = []
    coordination = read_coordination_states(root, batches, state_errors)
    errors.extend(state_errors)
    coordination_states = coordination["states"]
    execution_counts = Counter({
        "completed": 0, "failed": 0, "pending": 0, "translation_started": 0,
        "other": 0, "no_execution_evidence": 0,
    })
    execution_status_counts = Counter()
    validation_counts = Counter()
    for batch in batches:
        batch_counts = Counter()
        for article in batch["articles"]:
            state = executions.get(article["chinese_post_id"])
            if state is None:
                bucket = "no_execution_evidence"
            else:
                bucket = _execution_bucket(state["status"])
                execution_status_counts[state["status"]] += 1
                if state["english_post_id"] != article["english_post_id"]:
                    conflicts.append({
                        "type": "execution_english_post_id_mismatch",
                        "chinese_post_id": article["chinese_post_id"],
                        "batch_id": batch["batch_id"],
                        "fixed_english_post_id": article["english_post_id"],
                        "execution_english_post_id": state["english_post_id"],
                    })
            batch_counts[bucket] += 1
            execution_counts[bucket] += 1
        batch["execution_counts"] = dict(sorted(batch_counts.items()))
        batch["fixed_article_count"] = len(batch["articles"])
        batch["completed_count"] = batch_counts["completed"]
        batch["incomplete_count"] = len(batch["articles"]) - batch_counts["completed"]
        item = validation.get(batch["batch_id"])
        batch["validation_evidence_count"] = item["count"] if item else 0
        batch["validation_counts"] = item["counts"] if item else {}
        batch["validation_source_file"] = item["source_file"] if item else None
        batch["execution_candidate_count"] = (
            execution_candidates.get(batch["batch_id"], {}).get("count", 0))
        batch_state_counts = Counter(
            coordination_states[article["chinese_post_id"]]["workflow_status"]
            for article in batch["articles"]
            if article["chinese_post_id"] in coordination_states
        )
        batch["coordination_state_count"] = sum(batch_state_counts.values())
        batch["coordination_status_counts"] = dict(sorted(batch_state_counts.items()))
        batch["uninitialized_count"] = (
            len(batch["articles"]) - batch["coordination_state_count"])
        batch["integrity_ok"] = not batch["errors"] and not any(
            item["batch_id"] == batch["batch_id"]
            for item in coordination["batch_drift"])
        for status, count in batch["validation_counts"].items():
            validation_counts[status] += count
        del batch["articles"]
    unassigned_execution_ids = sorted(set(executions) - set(fixed_index))
    if unassigned_execution_ids:
        errors.append(
            "execution states exist outside recognized fixed batches: "
            + ",".join(str(value) for value in unassigned_execution_ids)
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "repository_root": str(root),
        "integrity_ok": not errors and not conflicts,
        "batches": batches,
        "fixed_article_count": sum(
            batch["fixed_article_count"] for batch in batches),
        "execution_counts": dict(execution_counts),
        "execution_status_counts": dict(sorted(execution_status_counts.items())),
        "validation_counts": {
            "total": sum(validation_counts.values()),
            "passed": validation_counts["ready"],
            "failed": validation_counts["pending"] + validation_counts["abnormal"],
            "unknown": sum(
                count for status, count in validation_counts.items()
                if status not in {"ready", "pending", "abnormal"}
            ),
            "by_status": dict(sorted(validation_counts.items())),
        },
        "conflicts": conflicts,
        "latest_incomplete_batch": _latest_incomplete(batches),
        "coordination_state_count": len(coordination_states),
        "coordination_status_counts":
            coordination["coordination_status_counts"],
        "legacy_import_count": coordination["legacy_import_count"],
        "uninitialized_count": (
            sum(batch["fixed_article_count"] for batch in batches)
            - len(coordination_states)
        ),
        "awaiting_manual_conversion_count":
            coordination["coordination_status_counts"].get(
                "awaiting_manual_conversion", 0),
        "state_integrity": not state_errors and not coordination["batch_drift"],
        "batch_drift": coordination["batch_drift"],
        "state_errors": state_errors,
        "errors": errors,
    }
    result["integrity_ok"] = (
        result["integrity_ok"] and result["state_integrity"])
    workflow_counts = result["coordination_status_counts"]
    for status in (
            "ready_for_execution", "execution_in_progress",
            "ready_for_translation_resume", "excerpt_failed",
            "translation_failed", "validation_failed", "blocked", "completed"):
        result[f"{status}_count"] = workflow_counts.get(status, 0)
    result["remaining_count"] = (
        result["fixed_article_count"] - workflow_counts.get("completed", 0))
    result["retry_exhausted_count"] = sum(
        any(int(value) >= (
            MAX_RESUME_ATTEMPTS if key == "resume" else MAX_RUN_ATTEMPTS)
            for key, value in (state.get("retry_counts") or {}).items())
        for state in coordination_states.values()
    )
    result["next_action"] = _next_action({
        **{key: workflow_counts.get(key, 0) for key in SUMMARY_KEYS},
        "total": result["fixed_article_count"],
    })
    return result


def _context(root):
    root = Path(root).resolve()
    errors = []
    conflicts = []
    batches = discover_batches(root, errors)
    fixed = validate_batch_index(batches, conflicts, errors)
    executions = read_execution_states(root, errors)
    coordination = read_coordination_states(root, batches, errors)
    if errors or conflicts or coordination["batch_drift"]:
        details = list(errors)
        details.extend(
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in conflicts)
        details.extend(
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in coordination["batch_drift"])
        raise ReadError(
            "repository integrity check failed: "
            + "; ".join(details or ["conflict or fixed batch drift detected"])
        )
    return root, batches, fixed, executions, coordination["states"]


def _latest_coordination_batch(batches, states):
    incomplete = []
    for batch in batches:
        values = [states.get(item["chinese_post_id"]) for item in batch["articles"]]
        if any(value is None or value["workflow_status"] != "completed"
               for value in values):
            incomplete.append(batch)
    if not incomplete:
        return None
    if any(item["batch_sequence"] is None or not item["allocated_at"]
           for item in incomplete):
        raise ReadError("latest incomplete batch cannot be determined reliably")
    return max(incomplete, key=lambda item: (
        item["allocated_at"], item["batch_sequence"], item["batch_id"]))


def show_current(root):
    root, batches, _, executions, states = _context(root)
    batch = _latest_coordination_batch(batches, states)
    if batch is None:
        return {
            "schema_version": SCHEMA_VERSION, "repository_root": str(root),
            "all_completed": True, "batch_id": None, "articles": [],
            "integrity_ok": True,
        }
    articles = []
    for article in batch["articles"]:
        state = states.get(article["chinese_post_id"])
        execution = executions.get(article["chinese_post_id"])
        articles.append({
            "position": article["batch_position"],
            "chinese_post_id": article["chinese_post_id"],
            "english_post_id": article["english_post_id"],
            "title": article["title"],
            "published_at": article["published_at"],
            "workflow_status": state["workflow_status"] if state else "uninitialized",
            "syntax_count_before": int(
                article["source_row"].get("before_syntaxhighlighter_count") or 0),
            "manual_conversion_confirmed": bool(
                state and state.get("manual_conversion", {}).get("status")
                == "confirmed"),
            "language_review_confirmed": bool(
                state and state.get("language_review", {}).get("status")
                == "confirmed"),
            "validation_status": (
                state.get("validation_evidence", {}).get("status")
                if state and state.get("validation_evidence")
                else "not_recorded"),
            "execution_status": execution["status"] if execution else "not_recorded",
        })
    return {
        "schema_version": SCHEMA_VERSION, "repository_root": str(root),
        "all_completed": False, "batch_id": batch["batch_id"],
        "source_file": batch["source_file"], "articles": articles,
        "integrity_ok": True,
    }


def _transition_event(event_type, state, previous, new, reason, evidence,
                      timestamp, identity):
    raw = (
        f"{event_type}|{state['batch_id']}|{state['chinese_post_id']}|{identity}"
    ).encode("utf-8")
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": _sha256_bytes(raw),
        "event_type": event_type,
        "occurred_at": timestamp,
        "batch_id": state["batch_id"],
        "chinese_post_id": state["chinese_post_id"],
        "previous_status": previous,
        "new_status": new,
        "reason": reason,
        "evidence": evidence,
        "legacy_import": state["legacy_import"],
    }


def _persist_transition(root, state_path, state, event):
    events_path = _events_path(root, state["batch_id"])
    events = _read_events(events_path)
    if event["event_id"] not in {item["event_id"] for item in events}:
        _atomic_write_events(events_path, events + [event])
    _atomic_write_json(state_path, state)


def mark_converted(root, post_id, syntax_count_before, cbp_count_after,
                   language_review_confirmed,
                   gutenberg_normalization_confirmed=False):
    if not language_review_confirmed:
        raise ReadError("--language-review-confirmed is required")
    if syntax_count_before < 0 or cbp_count_after < 0:
        raise ReadError(
            "SyntaxHighlighter count and Code Block Pro "
            "count must be non-negative")
    with InitLock(Path(root).resolve()):
        root, batches, fixed, _, states = _context(root)
        article = fixed.get(int(post_id))
        if article is None:
            raise ReadError(f"Chinese post {post_id} is outside fixed batches")
        batch = next(item for item in batches if item["batch_id"] == article["batch_id"])
        if batch["source_type"] not in SYNTAX_SOURCE_TYPES:
            raise ReadError("mark-converted only accepts SyntaxHighlighter daily batches")
        mixed_stage = batch["source_type"] in MIXED_SYNTAX_SOURCE_TYPES
        if mixed_stage and not gutenberg_normalization_confirmed:
            raise ReadError(
                "--gutenberg-normalization-confirmed is required for mixed "
                "SyntaxHighlighter batches")
        state = states.get(int(post_id))
        if state is None:
            raise ReadError(f"coordination state is missing for Chinese post {post_id}")
        expected = int(article["source_row"]["before_syntaxhighlighter_count"])
        if syntax_count_before != expected:
            raise ReadError(
                f"syntax-count-before mismatch: expected {expected}, got "
                f"{syntax_count_before}")
        if state["workflow_status"] == "awaiting_readonly_validation":
            same = (
                state.get("manual_conversion", {}).get("status") == "confirmed"
                and state["manual_conversion"].get("syntax_count_before")
                == syntax_count_before
                and state["manual_conversion"].get("cbp_count_after")
                == cbp_count_after
                and state.get("language_review", {}).get("status") == "confirmed"
                and (
                    not mixed_stage
                    or state.get("gutenberg_normalization", {}).get("status")
                    == "confirmed"
                )
            )
            if same:
                return {
                    "schema_version": SCHEMA_VERSION, "changed": False,
                    "workflow_status": state["workflow_status"],
                    "chinese_post_id": int(post_id), "integrity_ok": True,
                }
        if state["workflow_status"] != "awaiting_manual_conversion":
            raise ReadError(f"cannot mark converted from {state['workflow_status']}")
        timestamp = datetime.now(timezone.utc).isoformat()
        previous = state["workflow_status"]
        state["workflow_status"] = "awaiting_readonly_validation"
        state["manual_conversion"] = {
            "status": "confirmed", "confirmed_at": timestamp,
            "syntax_count_before": syntax_count_before,
            "cbp_count_after": cbp_count_after,
        }
        state["language_review"] = {
            "status": "confirmed", "confirmed_at": timestamp,
        }
        if mixed_stage:
            state["gutenberg_normalization"] = {
                "status": "confirmed", "confirmed_at": timestamp,
            }
        state["updated_at"] = timestamp
        evidence = {
            "syntax_count_before": syntax_count_before,
            "cbp_count_after": cbp_count_after,
            "language_review_confirmed": True,
            "gutenberg_normalization_confirmed": mixed_stage,
        }
        event = _transition_event(
            "manual_conversion_and_language_review_confirmed", state,
            previous, state["workflow_status"],
            "manual conversion and per-block language review explicitly confirmed",
            evidence, timestamp,
            f"{syntax_count_before}|{cbp_count_after}|language-confirmed|"
            f"gutenberg-normalized={mixed_stage}",
        )
        _persist_transition(
            root, _state_path(root, state["batch_id"], int(post_id)), state, event)
        return {
            "schema_version": SCHEMA_VERSION, "changed": True,
            "workflow_status": state["workflow_status"],
            "chinese_post_id": int(post_id), "integrity_ok": True,
        }


def _safe_repository_path(root, value):
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReadError("validation-file must be a safe repository-relative path")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ReadError("validation-file escapes repository root") from error
    if not resolved.is_file():
        raise ReadError(f"validation file does not exist: {value}")
    return resolved


def _true_field(row, field):
    value = str(row.get(field, "")).strip().lower()
    if value not in {"true", "false"}:
        raise ReadError(f"validation field {field} must be True or False")
    return value == "true"


def _valid_sha256(value):
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validation_int(row, field, path):
    try:
        return int(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise ReadError(
            f"{path}: validation field {field} must be an integer") from error


def _validation_text(row, field, path):
    value = row.get(field)
    if not isinstance(value, str):
        raise ReadError(f"{path}: validation field {field} must be text")
    return value.strip()


def _validation_result(path, batch, article, state):
    rows, fields = _read_csv(path)
    _required(fields, RECORD_VALIDATION_FIELDS, path)
    matching = [
        row for row in rows
        if isinstance(row.get("chinese_post_id"), str)
        and row["chinese_post_id"].strip()
        == str(article["chinese_post_id"])
    ]
    if len(matching) != 1:
        raise ReadError(
            f"{path}: expected exactly one row for Chinese post "
            f"{article['chinese_post_id']}")
    row = matching[0]
    if _validation_text(row, "batch_id", path) != batch["batch_id"]:
        raise ReadError(f"{path}: validation batch_id mismatch")
    if _positive_id(row, "english_post_id", path, 1) != article["english_post_id"]:
        raise ReadError(f"{path}: validation English post ID mismatch")
    if (_validation_text(row, "before_content_sha256", path)
            != article["source_row"]["before_content_sha256"]):
        raise ReadError(f"{path}: validation before content SHA-256 mismatch")
    after_sha256 = _validation_text(row, "after_content_sha256", path)
    if not _valid_sha256(after_sha256):
        raise ReadError(f"{path}: invalid after_content_sha256")
    source_row = article["source_row"]
    expected_cbp = int(source_row.get(
        "expected_code_block_pro_count_after",
        int(source_row["before_code_block_pro_count"])
        + int(source_row["before_syntaxhighlighter_count"]),
    ))
    if _validation_int(
            row, "expected_code_block_pro_count_after", path) != expected_cbp:
        raise ReadError(f"{path}: validation expected Code Block Pro count mismatch")
    validation_reasons = _validation_text(
        row, "validation_reasons", path)
    manual_cbp = state.get("manual_conversion", {}).get("cbp_count_after")
    checks = {
        "syntaxhighlighter_zero":
            _validation_int(row, "after_syntaxhighlighter_count", path) == 0,
        "code_block_pro_count":
            _validation_int(row, "after_code_block_pro_count", path)
            >= expected_cbp,
        "unknown_code_formats_zero":
            "unexpected-code-format:" not in validation_reasons,
        "polylang_relation_normal":
            _validation_text(row, "polylang_relation_status", path) == "normal",
        "chinese_excerpt_empty": _true_field(row, "chinese_excerpt_empty"),
        "chinese_publish":
            _validation_text(row, "chinese_status", path) == "publish",
        "chinese_language":
            _validation_text(row, "chinese_language", path) == "zh",
        "english_publish":
            _validation_text(row, "english_status", path) == "publish",
        "gutenberg_balanced": _true_field(row, "gutenberg_balanced"),
        "before_syntax_count":
            _validation_int(row, "before_syntaxhighlighter_count", path)
            == int(article["source_row"]["before_syntaxhighlighter_count"]),
        "before_cbp_count":
            _validation_int(row, "before_code_block_pro_count", path)
            == int(article["source_row"]["before_code_block_pro_count"]),
        "manual_cbp_count":
            isinstance(manual_cbp, int)
            and not isinstance(manual_cbp, bool)
            and _validation_int(row, "after_code_block_pro_count", path)
            >= manual_cbp,
    }
    if batch["source_type"] in MIXED_SYNTAX_SOURCE_TYPES:
        checks.update({
            "before_editor_format":
                _validation_text(row, "before_editor_format", path)
                == article["source_row"].get("source_editor_format", ""),
            "after_editor_format":
                _validation_text(row, "after_editor_format", path) == "gutenberg",
            "classic_outside_blocks_after":
                not _true_field(row, "classic_outside_blocks_after"),
            "gutenberg_normalization_confirmed":
                state.get("gutenberg_normalization", {}).get("status")
                == "confirmed",
        })
    status = _validation_text(row, "validation_status", path)
    if status not in {"ready", "pending", "abnormal"}:
        raise ReadError(f"{path}: unknown validation_status {status!r}")
    failure_reasons = [name for name, value in checks.items() if not value]
    if status != "ready":
        failure_reasons.extend(
            item for item in validation_reasons.split("|") if item)
    passed = status == "ready" and not failure_reasons
    return row, passed, sorted(set(failure_reasons)), checks


def _record_validation_locked(root, post_id, validation_file, refresh=False):
    root, batches, fixed, _, states = _context(root)
    article = fixed.get(int(post_id))
    if article is None:
        raise ReadError(f"Chinese post {post_id} is outside fixed batches")
    state = states.get(int(post_id))
    if state is None:
        raise ReadError(f"coordination state is missing for Chinese post {post_id}")
    path = _safe_repository_path(root, validation_file)
    digest = _file_sha256(path)
    existing = state.get("validation_evidence")
    if state["workflow_status"] in {"ready_for_execution", "validation_failed"}:
        if existing and existing.get("sha256") == digest:
            return {
                "schema_version": SCHEMA_VERSION, "changed": False,
                "workflow_status": state["workflow_status"],
                "chinese_post_id": int(post_id), "integrity_ok": True,
            }
    allowed_statuses = {"awaiting_readonly_validation"}
    if refresh:
        allowed_statuses.add("validation_failed")
    if state["workflow_status"] not in allowed_statuses:
        raise ReadError(
            f"cannot record validation from {state['workflow_status']}")
    batch = next(item for item in batches if item["batch_id"] == article["batch_id"])
    row, passed, reasons, checks = _validation_result(
        path, batch, article, state)
    timestamp = datetime.now(timezone.utc).isoformat()
    previous = state["workflow_status"]
    new_status = "ready_for_execution" if passed else "validation_failed"
    evidence = {
        "source_file": _relative(path, root),
        "sha256": digest,
        "status": row["validation_status"],
        "validated_at": row.get("validated_at") or None,
        "after_content_sha256": row["after_content_sha256"],
        "checks": checks,
        "failure_reasons": reasons,
    }
    state["workflow_status"] = new_status
    state["validation_evidence"] = evidence
    state["validation_failure_reasons"] = reasons
    state["updated_at"] = timestamp
    event = _transition_event(
        "readonly_validation_recorded", state, previous, new_status,
        "read-only validation passed" if passed
        else "read-only validation failed",
        evidence, timestamp, digest,
    )
    _persist_transition(
        root, _state_path(root, state["batch_id"], int(post_id)), state, event)
    return {
        "schema_version": SCHEMA_VERSION, "changed": True,
        "workflow_status": new_status, "validation_passed": passed,
        "failure_reasons": reasons, "chinese_post_id": int(post_id),
        "integrity_ok": True,
    }


def record_validation(root, post_id, validation_file):
    with InitLock(Path(root).resolve()):
        return _record_validation_locked(root, post_id, validation_file)


def _validation_paths(root, batch_id, post_id):
    directory = root / VALIDATION_ROOT / batch_id
    return (
        directory / f"chinese-{int(post_id)}.csv",
        directory / f"chinese-{int(post_id)}.snapshot.jsonl",
        directory / f"chinese-{int(post_id)}.execution-candidate.csv",
    )


def _atomic_write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _execution_manifest_row(article, validation_row, source,
                            source_type="syntaxhighlighter_daily"):
    from src.candidate_execution import sha256_text
    from src.single_candidate_flow import raw_field

    chinese = source.get_post(article["chinese_post_id"])
    english = source.get_post(article["english_post_id"])
    return {
        "chinese_post_id": article["chinese_post_id"],
        "chinese_title": article["title"],
        "chinese_content_sha256": validation_row["after_content_sha256"],
        "chinese_excerpt_empty": validation_row["chinese_excerpt_empty"],
        "english_post_id": article["english_post_id"],
        "english_post_status": validation_row["english_status"],
        "english_title_sha256": sha256_text(raw_field(english, "title")),
        "english_excerpt_sha256": sha256_text(raw_field(english, "excerpt")),
        "english_content_sha256": sha256_text(raw_field(english, "content")),
        "candidate_reason":
            "fixed SyntaxHighlighter article; manual language review confirmed; "
            "production read-only validation ready",
        "execution_status": "pending",
        "chinese_post_status": validation_row["chinese_status"],
        "chinese_language": validation_row["chinese_language"],
        "source_migration_type": (
            MIXED_SOURCE_MIGRATION_TYPE
            if source_type in MIXED_SYNTAX_SOURCE_TYPES
            else "syntaxhighlighter-to-code-block-pro"
        ),
        "expected_code_block_pro_count":
            validation_row["after_code_block_pro_count"],
        "expected_syntaxhighlighter_count": 0,
    }


def validate_live(root, post_id, source_factory=None, refresh=False):
    """Reuse the batch read-only source and validator for exactly one fixed row."""
    root = Path(root).resolve()
    with InitLock(root):
        root, batches, fixed, _, states = _context(root)
        article = fixed.get(int(post_id))
        if article is None:
            raise ReadError(f"Chinese post {post_id} is outside fixed batches")
        batch = next(
            item for item in batches if item["batch_id"] == article["batch_id"])
        if batch["source_type"] not in SYNTAX_SOURCE_TYPES:
            raise ReadError("validate-live only accepts SyntaxHighlighter daily batches")
        state = states.get(int(post_id))
        if state is None:
            raise ReadError(f"coordination state is missing for Chinese post {post_id}")
        csv_path, snapshot_path, manifest_path = _validation_paths(
            root, batch["batch_id"], post_id)
        relative_csv = _relative(csv_path, root)
        if not refresh and state["workflow_status"] in {
                "ready_for_execution", "validation_failed"}:
            existing = state.get("validation_evidence")
            if existing and existing.get("source_file") == relative_csv:
                result = _record_validation_locked(root, post_id, relative_csv)
                return {
                    **result, "mode": "already-recorded",
                    "validation_file": relative_csv,
                    "wordpress_writes": 0, "glm_calls": 0,
                    "translation_calls": 0,
                }
        allowed_statuses = {"awaiting_readonly_validation"}
        if refresh:
            allowed_statuses.add("validation_failed")
        if state["workflow_status"] not in allowed_statuses:
            raise ReadError(
                f"cannot validate live from {state['workflow_status']}")
        if csv_path.exists() and not refresh:
            result = _record_validation_locked(root, post_id, relative_csv)
            return {
                **result, "mode": "import-existing",
                "validation_file": relative_csv,
                "wordpress_writes": 0, "glm_calls": 0,
                "translation_calls": 0,
            }
        if not refresh and (snapshot_path.exists() or manifest_path.exists()):
            raise ReadError(
                f"partial validation evidence already exists for Chinese post {post_id}")
        try:
            from src.batch_readonly_ssh import BatchReadonlySshSource
            from src.syntaxhighlighter_batch_validation import (
                VALIDATION_FIELDS as LIVE_VALIDATION_FIELDS,
                validate_batch, write_outputs)

            config = json.loads(
                (root / "config/classification.json").read_text(encoding="utf-8"))
            source = (
                source_factory([article["source_row"]])
                if source_factory else
                BatchReadonlySshSource.fetch([article["source_row"]])
            )
            rows = validate_batch(
                [article["source_row"]], source, source, config)
            if len(rows) != 1:
                raise ReadError("read-only validator returned an unexpected row count")
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            output_directory = Path(tempfile.mkdtemp(
                prefix=f".validation-{int(post_id)}-", dir=csv_path.parent))
            temporary_csv = output_directory / csv_path.name
            temporary_snapshot = output_directory / snapshot_path.name
            temporary_manifest = output_directory / manifest_path.name
            write_outputs(rows, temporary_csv, temporary_snapshot)
            # The shared writer intentionally owns validation semantics; rewrite
            # only its CSV line endings atomically for stable Git text evidence.
            _atomic_write_csv(temporary_csv, LIVE_VALIDATION_FIELDS, rows)
            if rows[0]["validation_status"] == "ready":
                _atomic_write_csv(
                    temporary_manifest, EXECUTION_MANIFEST_FIELDS,
                    [_execution_manifest_row(
                        article, rows[0], source, batch["source_type"])])
            # Prove the new CSV can form state evidence before replacing any
            # previously recorded validation files.
            _validation_result(
                temporary_csv, batch, article, state)
            os.replace(temporary_csv, csv_path)
            os.replace(temporary_snapshot, snapshot_path)
            if temporary_manifest.exists():
                os.replace(temporary_manifest, manifest_path)
            output_directory.rmdir()
        except (SafetyError, OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReadError(f"validate-live operation failed: {error}") from error
        result = _record_validation_locked(
            root, post_id, relative_csv, refresh=refresh)
        return {
            **result, "mode": "live-readonly",
            "validation_file": relative_csv,
            "snapshot_file": _relative(snapshot_path, root),
            "execution_manifest": (
                _relative(manifest_path, root) if manifest_path.exists() else None),
            "wordpress_writes": 0, "glm_calls": 0, "translation_calls": 0,
        }


def _manifest_for(root, state):
    path = _validation_paths(
        root, state["batch_id"], state["chinese_post_id"])[2]
    if not path.is_file():
        return None
    return _relative(path, root)


def plan_run(root):
    root, batches, _, executions, states = _context(root)
    plans = []
    for batch in batches:
        for article in batch["articles"]:
            state = states.get(article["chinese_post_id"])
            if not state or state["workflow_status"] != "ready_for_execution":
                continue
            execution = executions.get(article["chinese_post_id"])
            reasons = []
            if execution is not None and not _source_restart_prepared(state, execution):
                reasons.append(
                    "completed execution evidence already exists"
                    if execution["status"] == "completed"
                    else f"unsafe existing execution status: {execution['status']}")
            manifest = _manifest_for(root, state)
            plans.append({
                "batch_id": batch["batch_id"],
                "post_id": article["chinese_post_id"],
                "english_post_id": article["english_post_id"],
                "execution_candidate_path": manifest,
                "future_arguments": [
                    "--post-id", str(article["chinese_post_id"]), "--execute"],
                "validation_evidence": state.get("validation_evidence"),
                "execution_evidence": execution,
                "allowed": execution is None or _source_restart_prepared(state, execution),
                "blocking_reasons": reasons,
            })
    return {
        "schema_version": SCHEMA_VERSION, "repository_root": str(root),
        "planned_count": len(plans),
        "allowed_count": sum(item["allowed"] for item in plans),
        "items": plans, "writes_performed": False, "integrity_ok": True,
    }


EXECUTION_STATUS_MAP = {
    "completed": "completed",
    "excerpt_rejected": "excerpt_failed",
    "rejected_excerpt_generation": "excerpt_failed",
    "excerpt_generation_failed": "excerpt_failed",
    "chinese_excerpt_saved": "ready_for_translation_resume",
    "translation_started": "ready_for_translation_resume",
    "translation_failed": "translation_failed",
    "prepared": "blocked",
    "excerpt_generated": "blocked",
    "failed": "blocked",
    "pending": "blocked",
}


def _execution_path(root, post_id):
    return root / "data/backups/single-candidate" / (
        f"chinese-{int(post_id)}.execution.json")


def _prewrite_path(root, post_id):
    return root / "data/backups/single-candidate" / (
        f"chinese-{int(post_id)}.pre-write.json")


def _source_restart_prepared(state, execution):
    """Recognize only a SHA-bound prepared execution from this restart generation.

    Older coordination records predate ``execution_evidence.recovery_generation``.
    Its absence is compatible only when the execution file itself, the restart
    record, and the SHA-bound evidence identify the current generation.  A
    present coordination generation is never treated as optional.
    """
    recovery = state.get("source_restart_recovery") or {}
    evidence = state.get("execution_evidence") or {}
    return bool(
        execution and execution["status"] == "prepared"
        and recovery.get("status") == "applied"
        and recovery.get("generation") == state.get("recovery_generation")
        and execution.get("recovery_generation") == state.get("recovery_generation")
        and evidence.get("status") == "prepared"
        and evidence.get("sha256") == execution.get("sha256")
        and (
            "recovery_generation" not in evidence
            or evidence["recovery_generation"] == state.get("recovery_generation")
        )
    )


def _legacy_prepared_excerpt_http_failure(state, execution_sha):
    """Recognize only the pre-fix prepared evidence caused by GLM HTTP 400.

    Earlier executors did not persist a terminal excerpt-generation status.
    The coordination failure event is sufficient only when it is SHA-bound to
    the still-prepared execution file and explicitly records the HTTP 400.
    """
    evidence = state.get("execution_evidence") or {}
    recovery = state.get("recovery") or {}
    # A pre-fix recovery attempt could overwrite ``last_failure`` after the
    # original evidence had already been preserved in ``recovery``.  Trust it
    # only when that recovery remains bound to this exact prepared file.
    preserved = recovery.get("preserved_failure")
    failure = (
        preserved if (_excerpt_generation_recovery_active(state, execution_sha)
                      and isinstance(preserved, dict))
        else state.get("last_failure") or {})
    text = "\n".join(str(failure.get(key, "")) for key in (
        "stderr_summary", "stdout_summary", "error_summary"))
    return bool(
        state.get("workflow_status") == "blocked"
        and failure.get("stage") == "run"
        and failure.get("reason") == "transient_network_error"
        and re.search(r"\b(?:http(?: request)? (?:failed )?with status|http error) 400\b",
                      text, re.IGNORECASE)
        and evidence.get("status") == "prepared"
        and evidence.get("sha256") == execution_sha
    )


def _excerpt_generation_recovery_active(state, execution_sha):
    recovery = state.get("recovery") or {}
    return bool(
        recovery.get("status") == "applied"
        and recovery.get("action") == "retry_excerpt_generation"
        and recovery.get("execution_sha256") == execution_sha
    )


def _execution_details(root, article):
    path = _execution_path(root, article["chinese_post_id"])
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReadError(f"{path}: invalid execution JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReadError(f"{path}: execution JSON must be an object")
    chinese_id = _positive_id(value, "chinese_post_id", path, 1)
    english_id = _positive_id(value, "english_post_id", path, 1)
    if chinese_id != article["chinese_post_id"]:
        raise ReadError(f"{path}: execution Chinese post ID mismatch")
    if english_id != article["english_post_id"]:
        raise ReadError(f"{path}: execution English post ID mismatch")
    status = value.get("status")
    if status not in EXECUTION_STATUS_MAP:
        raise ReadError(f"{path}: unsupported execution status {status!r}")
    error_response = value.get("error_response")
    if not isinstance(error_response, dict):
        error_response = None
    return {
        "chinese_post_id": chinese_id, "english_post_id": english_id,
        "status": status, "source_file": _relative(path, root),
        "sha256": _file_sha256(path),
        "mtime_ns": path.stat().st_mtime_ns,
        "error_response": error_response,
        "error_response_excerpt": _safe_subprocess_summary(
            value.get("error_response_excerpt", "")),
        "excerpt_generation_attempts": (
            value.get("excerpt_generation_attempts")
            if isinstance(value.get("excerpt_generation_attempts"), int)
            else value.get("attempts")
            if status in {"excerpt_rejected", "rejected_excerpt_generation"}
            and isinstance(value.get("attempts"), int)
            else None
        ),
        "recovery_generation": value.get("recovery_generation"),
        "restart_excerpt_state": value.get("restart_excerpt_state"),
        "expected_pre_run_excerpt_sha256": value.get(
            "expected_pre_run_excerpt_sha256"),
    }


def _normalized_execution_status(status):
    return ({
        "excerpt_rejected": "excerpt_rejected",
        "rejected_excerpt_generation": "excerpt_rejected",
    }).get(status, status)


def _structured_execution_failure(execution):
    """Prefer structured execution evidence over stderr text heuristics."""
    status = _normalized_execution_status((execution or {}).get("status"))
    if status == "excerpt_rejected":
        return {"category": "rejected_excerpt_generation"}

    response = (execution or {}).get("error_response") or {}
    code = response.get("code")
    message = response.get("message")
    if status == "excerpt_generation_failed" and code:
        category = "glm_api_error"
    elif code == "swq_full_article_token_validation_failed":
        category = "protected_token_validation_error"
    elif code:
        category = "wordpress_api_error"
    else:
        return None
    readable = ": ".join(str(value) for value in (code, message) if value)
    return {"category": category, "wordpress_code": code,
            "wordpress_message": message, "error_summary": readable}


def _apply_execution_state(root, state, execution, reason):
    new_status = EXECUTION_STATUS_MAP[execution["status"]]
    if state["workflow_status"] == "completed":
        return False
    if state["workflow_status"] == new_status:
        evidence = state.get("execution_evidence") or {}
        if evidence.get("sha256") == execution["sha256"]:
            return False
    previous = state["workflow_status"]
    timestamp = datetime.now(timezone.utc).isoformat()
    state["workflow_status"] = new_status
    state["execution_evidence"] = {
        "source_file": execution["source_file"],
        "sha256": execution["sha256"],
        "status": execution["status"],
    }
    if execution.get("recovery_generation") is not None:
        state["execution_evidence"]["recovery_generation"] = (
            execution["recovery_generation"])
    state["updated_at"] = timestamp
    event = _transition_event(
        "execution_state_synchronized", state, previous, new_status, reason,
        state["execution_evidence"], timestamp,
        f"{execution['sha256']}|{new_status}",
    )
    _persist_transition(
        root, _state_path(root, state["batch_id"], state["chinese_post_id"]),
        state, event)
    return True


def sync_execution(root, apply=False):
    root, batches, fixed, _, states = _context(root)
    actions = []
    for batch in batches:
        for article in batch["articles"]:
            state = states.get(article["chinese_post_id"])
            execution = _execution_details(root, article)
            if state is None or execution is None:
                continue
            if state["workflow_status"] == "completed":
                continue
            if _source_restart_prepared(state, execution):
                continue
            actions.append({
                "batch_id": batch["batch_id"],
                "post_id": article["chinese_post_id"],
                "previous_status": state["workflow_status"],
                "execution_status": execution["status"],
                "new_status": EXECUTION_STATUS_MAP[execution["status"]],
                "execution": execution,
            })
    if not apply:
        return {
            "schema_version": SCHEMA_VERSION, "mode": "preview",
            "repository_root": str(root), "planned_count": len(actions),
            "changed_count": 0, "items": actions, "writes_performed": False,
            "integrity_ok": True,
        }
    changed = 0
    with InitLock(root):
        root, _, fixed, _, states = _context(root)
        for action in actions:
            article = fixed[action["post_id"]]
            execution = _execution_details(root, article)
            if execution is None:
                continue
            changed += int(_apply_execution_state(
                root, states[action["post_id"]], execution,
                "synchronized from existing single-candidate execution evidence"))
    return {
        "schema_version": SCHEMA_VERSION, "mode": "apply",
        "repository_root": str(root), "planned_count": len(actions),
        "changed_count": changed, "items": actions,
        "writes_performed": bool(changed), "integrity_ok": True,
    }


def _validation_still_valid(root, state):
    evidence = state.get("validation_evidence")
    if not isinstance(evidence, dict):
        raise ReadError("validation evidence is missing")
    path = _safe_repository_path(root, evidence.get("source_file", ""))
    if _file_sha256(path) != evidence.get("sha256"):
        raise ReadError(f"{path}: validation evidence SHA-256 drift")


def _record_attempt_start(root, state, stage):
    attempts = dict(state.get("retry_counts") or {})
    attempts[stage] = int(attempts.get(stage, 0)) + 1
    prior_attempts = _resume_attempt_numbers(
        _read_events(_events_path(root, state["batch_id"])),
        state["chinese_post_id"],
        {f"{stage}_attempt_started", f"{stage}_attempt_completed",
         f"{stage}_attempt_failed"},
        stage=stage,
    )
    attempt = max(prior_attempts, default=0) + 1
    timestamp = datetime.now(timezone.utc).isoformat()
    previous = state["workflow_status"]
    state["retry_counts"] = attempts
    state["workflow_status"] = "execution_in_progress"
    state["updated_at"] = timestamp
    event = _transition_event(
        f"{stage}_attempt_started", state, previous, "execution_in_progress",
        f"{stage} attempt {attempt} started",
        {"stage": stage, "attempt": attempt,
         "recovery_generation": state.get("recovery_generation")}, timestamp,
        f"{stage}|{attempt}|{timestamp}")
    _persist_transition(
        root, _state_path(root, state["batch_id"], state["chinese_post_id"]),
        state, event)
    return attempt


def _safe_subprocess_summary(value):
    text = value if isinstance(value, str) else ""
    patterns = (
        (r"(?i)\b(Bearer)\s+[^\s,;]+", r"\1 [REDACTED]"),
        (
            r"(?i)\b(Authorization|Cookie|WP_ADMIN_COOKIE|WP_REST_NONCE|"
            r"ZHIPU_API_KEY|API[_-]?KEY|password|secret|token)"
            r"(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
        ),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text[:SUBPROCESS_SUMMARY_LIMIT]


def _classify_subprocess_failure(completed, phase):
    stderr = _safe_subprocess_summary(getattr(completed, "stderr", ""))
    stdout = _safe_subprocess_summary(getattr(completed, "stdout", ""))
    combined = f"{stderr}\n{stdout}".lower()
    readonly_markers = (
        "read-only polylang ssh check exited with 255",
        "read-only polylang ssh check timed out",
        "read-only polylang ssh check failed after",
        "batch read-only ssh query exited with 255",
        "production_readonly_unavailable",
    )
    transient_markers = (
        "network request failed", "urlerror", "ssleoferror",
        "unexpected_eof_while_reading", "unexpected eof",
        "timed out", "timeout", "temporary failure in name resolution",
        "name or service not known", "connection reset", "connection refused",
        "remote end closed connection", "remotedisconnected",
        "http error 502", "http error 503", "bad gateway",
    )
    authentication_markers = (
        "http error 401", "http error 403", "unauthorized",
        "authentication failed",
    )
    if any(marker in combined for marker in readonly_markers):
        category = "production_readonly_unavailable"
    elif any(marker in combined for marker in authentication_markers):
        category = "authentication_error"
    elif re.search(r"\b(?:http(?: request)? (?:failed )?with status|http error) 4(?:0[0-9]|1[0-9])\b", combined):
        # A non-authentication HTTP 4xx is a deterministic client/API request
        # failure, not a transport interruption.  408 and 429 are handled as
        # retryable below when explicitly reported by the service.
        category = "transient_network_error" if re.search(
            r"\b(?:408|429)\b", combined) else "http_client_error"
    elif any(marker in combined for marker in transient_markers):
        category = "transient_network_error"
    elif phase == "preflight":
        category = "preflight_failed"
    else:
        category = "executor_failed_without_state"
    return {
        "category": category,
        "returncode": int(getattr(completed, "returncode", -1)),
        "stderr_summary": stderr,
        "stdout_summary": stdout,
    }


def _exception_failure(error, phase):
    completed = type("FailedProcess", (), {
        "returncode": -1, "stdout": "", "stderr":
            f"{type(error).__name__}: {error}",
    })()
    return _classify_subprocess_failure(completed, phase)


def _block_after_operation_error(root, state, stage, attempt, failure):
    previous = state["workflow_status"]
    timestamp = datetime.now(timezone.utc).isoformat()
    state["workflow_status"] = "blocked"
    state["last_failure"] = {
        "stage": stage, "attempt": attempt,
        "reason": failure["category"], "occurred_at": timestamp,
        "returncode": failure.get("returncode"),
        "stderr_summary": failure.get("stderr_summary", ""),
        "stdout_summary": failure.get("stdout_summary", ""),
    }
    state["updated_at"] = timestamp
    event = _transition_event(
        f"{stage}_attempt_failed", state, previous, "blocked",
        "execution subprocess failed without usable execution evidence",
        {"stage": stage, "attempt": attempt,
         "recovery_generation": state.get("recovery_generation"), **failure},
        timestamp, f"{stage}|{attempt}|blocked|{timestamp}")
    _persist_transition(
        root, _state_path(root, state["batch_id"], state["chinese_post_id"]),
        state, event)


def _record_attempt_outcome(root, state, stage, attempt, completed, failure=None):
    timestamp = datetime.now(timezone.utc).isoformat()
    succeeded = failure is None
    event_type = f"{stage}_attempt_completed" if succeeded else f"{stage}_attempt_failed"
    evidence = {
        "stage": stage,
        "attempt": attempt,
        "recovery_generation": state.get("recovery_generation"),
        "result": completed["result"],
        "category": completed["category"],
        "returncode": completed["returncode"],
        "error": completed["error"],
    }
    if failure:
        evidence.update(failure)
        state["last_failure"] = {
            "stage": stage, "attempt": attempt,
            "reason": failure["category"], "occurred_at": timestamp,
            "returncode": failure["returncode"],
            "error_summary": failure.get("error_summary", ""),
            "wordpress_code": failure.get("wordpress_code"),
            "wordpress_message": failure.get("wordpress_message"),
            "stderr_summary": failure.get("stderr_summary", ""),
            "stdout_summary": failure.get("stdout_summary", ""),
        }
    state["updated_at"] = timestamp
    event = _transition_event(
        event_type, state, state["workflow_status"], state["workflow_status"],
        (
            f"{stage} attempt {attempt} completed"
            if succeeded else f"{stage} attempt {attempt} failed"
        ),
        evidence, timestamp, f"{stage}|{attempt}|terminal|{timestamp}")
    _persist_transition(
        root, _state_path(root, state["batch_id"], state["chinese_post_id"]),
        state, event)


def _operation_result(item, result, category, returncode, error, **values):
    return {
        **item, "result": result, "category": category,
        "returncode": returncode, "error": error, **values,
    }


def _failure_error(failure):
    return (
        failure.get("error_summary")
        or
        failure.get("stderr_summary")
        or failure.get("stdout_summary")
        or failure["category"]
    )


def _failure_details(failure):
    return {
        key: value for key, value in failure.items()
        if key not in {"category", "returncode"}
    }


def _block_retry_exhausted(root, state, stage):
    previous = state["workflow_status"]
    timestamp = datetime.now(timezone.utc).isoformat()
    attempt = int((state.get("retry_counts") or {}).get(stage, 0))
    state["workflow_status"] = "blocked"
    state["last_failure"] = {
        "stage": stage, "attempt": attempt,
        "reason": f"{stage} retry limit exhausted", "occurred_at": timestamp,
    }
    state["updated_at"] = timestamp
    event = _transition_event(
        f"{stage}_retry_exhausted", state, previous, "blocked",
        f"{stage} retry limit exhausted",
        {"stage": stage, "attempt": attempt},
        timestamp, f"{stage}|retry-exhausted|{attempt}")
    _persist_transition(
        root, _state_path(root, state["batch_id"], state["chinese_post_id"]),
        state, event)


def _executor_command(root, state, *, resume=False, preflight=False,
                      special_validated_mixed=False):
    manifest = _manifest_for(root, state)
    if manifest is None:
        raise ReadError("single-candidate execution manifest is missing")
    command = [
        sys.executable, str(root / "bin/execute-single-candidate.py"),
        "--post-id", str(state["chinese_post_id"]),
        "--manifest", str(root / manifest),
        "--expected-candidate-count", "1",
        "--backup-dir", str(root / "data/backups/single-candidate"),
    ]
    if preflight:
        command.append("--preflight-live")
    else:
        command.append("--execute")
    if resume:
        command.append("--resume")
    if (not resume and state.get("source_restart_recovery", {}).get("status")
            == "applied"):
        command.append("--recovery-restart")
    if special_validated_mixed:
        command.append("--special-validated-mixed")
    return command


def _execution_artifacts(root, post_id):
    backup_root = root / "data/backups"
    if not backup_root.exists():
        return []
    return sorted(
        _relative(path, root)
        for path in backup_root.rglob(f"chinese-{int(post_id)}*")
        if path.is_file()
    )


def _restore_ready_after_transient(root, state, attempt, failure):
    previous = state["workflow_status"]
    timestamp = datetime.now(timezone.utc).isoformat()
    state["workflow_status"] = "ready_for_execution"
    state["last_failure"] = {
        "stage": "run", "attempt": attempt,
        "reason": failure["category"], "occurred_at": timestamp,
        "returncode": failure["returncode"],
        "stderr_summary": failure["stderr_summary"],
        "stdout_summary": failure["stdout_summary"],
    }
    state["updated_at"] = timestamp
    event = _transition_event(
        "run_attempt_failed", state, previous,
        "ready_for_execution",
        "transient network failure occurred before any local write evidence",
        {"stage": "run", "attempt": attempt, **failure},
        timestamp, f"run|{attempt}|transient-ready")
    _persist_transition(
        root, _state_path(root, state["batch_id"], state["chinese_post_id"]),
        state, event)


def _record_recovery_preflight_failure(root, state, failure):
    """Keep a recovery preflight failure separate from its proven root cause."""
    timestamp = datetime.now(timezone.utc).isoformat()
    previous = state["workflow_status"]
    recovery = dict(state.get("recovery") or {})
    attempts = int(recovery.get("preflight_attempts", 0)) + 1
    evidence = {
        "attempt": attempts, "occurred_at": timestamp,
        "category": failure["category"],
        "returncode": failure.get("returncode"),
        "stderr_summary": failure.get("stderr_summary", ""),
        "stdout_summary": failure.get("stdout_summary", ""),
    }
    recovery["preflight_attempts"] = attempts
    recovery["last_preflight_failure"] = evidence
    # ``preserved_failure`` remains the SHA-bound GLM failure that made this
    # narrowly-authorized recovery safe.  Do not replace it with a read error.
    state["workflow_status"] = "blocked"
    state["recovery"] = recovery
    state["updated_at"] = timestamp
    event = _transition_event(
        "recovery_preflight_attempt_failed", state, previous, "blocked",
        "recovery preflight failed before a candidate execution was started",
        {"recovery": recovery, "preflight_failure": evidence}, timestamp,
        f"recovery-preflight|{attempts}|{timestamp}")
    _persist_transition(
        root, _state_path(root, state["batch_id"], state["chinese_post_id"]),
        state, event)


def _record_interrupted_execution(root, state, attempt, artifacts):
    """Preserve an ambiguous interruption without claiming it is safe to retry."""
    timestamp = datetime.now(timezone.utc).isoformat()
    state["last_failure"] = {
        "stage": "run", "attempt": attempt,
        "reason": "execution_interrupted_write_status_unknown",
        "occurred_at": timestamp, "artifacts": artifacts,
    }
    state["updated_at"] = timestamp
    event = _transition_event(
        "run_attempt_interrupted", state, "execution_in_progress",
        "execution_in_progress",
        "user interrupted execution; write status requires evidence-based recovery",
        {"stage": "run", "attempt": attempt, "artifacts": artifacts,
         "write_status": "unknown"}, timestamp,
        f"run|{attempt}|interrupted|{timestamp}")
    _persist_transition(
        root, _state_path(root, state["batch_id"], state["chinese_post_id"]),
        state, event)


def _select_batch(batches, batch_id):
    if batch_id is None:
        return batches
    selected = [item for item in batches if item["batch_id"] == batch_id]
    if not selected:
        raise ReadError(f"unknown batch_id: {batch_id}")
    return selected


def _special_validated_mixed_execution_allowed(batch, state):
    """Gate the exception on the special batch's completed read-only workflow."""
    evidence = state.get("validation_evidence") or {}
    return bool(
        batch.get("source_type") in MIXED_SYNTAX_SOURCE_TYPES
        and state.get("workflow_status") == "ready_for_execution"
        and state.get("manual_conversion", {}).get("status") == "confirmed"
        and state.get("gutenberg_normalization", {}).get("status") == "confirmed"
        and state.get("language_review", {}).get("status") == "confirmed"
        and evidence.get("status") == "ready"
        and not evidence.get("failure_reasons")
    )


def _special_validated_mixed_recovery_allowed(batch, state):
    """Keep the current validated-mixed safety boundary during resume/recovery."""
    evidence = state.get("validation_evidence") or {}
    return bool(
        batch.get("source_type") in MIXED_SYNTAX_SOURCE_TYPES
        and state.get("manual_conversion", {}).get("status") == "confirmed"
        and state.get("gutenberg_normalization", {}).get("status") == "confirmed"
        and state.get("language_review", {}).get("status") == "confirmed"
        and evidence.get("status") == "ready"
        and not evidence.get("failure_reasons")
    )


def _run_items(root, batch_id=None, post_id=None):
    root, batches, _, executions, states = _context(root)
    items = []
    for batch in _select_batch(batches, batch_id):
        for article in batch["articles"]:
            state = states.get(article["chinese_post_id"])
            if not state or state["workflow_status"] != "ready_for_execution":
                continue
            reasons = []
            execution = executions.get(article["chinese_post_id"])
            recovery = state.get("recovery") or {}
            execution_sha256 = (
                _execution_details(root, article)["sha256"]
                if execution and recovery.get("status") == "applied"
                else None
            )
            recovered_restart = bool(
                execution
                and execution["status"] in {
                    "prepared", "excerpt_generation_failed", "excerpt_generated"}
                and recovery.get("status") == "applied"
                and recovery.get("stage") == "run"
                and recovery.get("action") in {
                    "restart", "observe", "retry_excerpt_generation"}
                and recovery.get("execution_sha256") == execution_sha256
            )
            if execution and not recovered_restart and not _source_restart_prepared(
                    state, _execution_details(root, article)):
                reasons.append("execution evidence already exists")
            if (state.get("source_restart_recovery", {}).get("status") == "applied"
                    and post_id != article["chinese_post_id"]):
                reasons.append("recovery generation requires explicit matching --post-id")
            if _manifest_for(root, state) is None:
                reasons.append("single-candidate execution manifest is missing")
            if (batch.get("source_type") in MIXED_SYNTAX_SOURCE_TYPES
                    and not _special_validated_mixed_execution_allowed(batch, state)):
                reasons.append("special batch requires passed read-only validation")
            try:
                _validation_still_valid(root, state)
            except ReadError as error:
                reasons.append(str(error))
            items.append({
                "batch_id": batch["batch_id"], "post_id": article["chinese_post_id"],
                "english_post_id": article["english_post_id"],
                "allowed": not reasons, "blocking_reasons": reasons,
            })
    return root, items


def _run_ready_once(root, execute=False, batch_id=None, post_id=None,
                    runner=subprocess.run, max_run_attempts=MAX_RUN_ATTEMPTS,
                    recovery_retry=False):
    root, items = _run_items(root, batch_id, post_id)
    if post_id is not None:
        items = [item for item in items if item["post_id"] == int(post_id)]
    if not execute:
        return {
            "schema_version": SCHEMA_VERSION, "mode": "preview",
            "repository_root": str(root), "selected_count": len(items),
            "allowed_count": sum(item["allowed"] for item in items),
            "items": items, "writes_performed": False, "integrity_ok": True,
        }
    results = []
    with InitLock(root):
        root, batches, fixed, _, states = _context(root)
        batches_by_id = {batch["batch_id"]: batch for batch in batches}
        total = len(items)
        for index, item in enumerate(items, 1):
            state = states.get(item["post_id"])
            attempt = None
            def finish(result):
                results.append(result)

            try:
                if not item["allowed"] or state["workflow_status"] != "ready_for_execution":
                    raise ReadError("; ".join(item["blocking_reasons"])
                                    or "article is no longer ready")
                _validation_still_valid(root, state)
                special_validated_mixed = _special_validated_mixed_execution_allowed(
                    batches_by_id[item["batch_id"]], state)
                if (not recovery_retry and int((state.get("retry_counts") or {}).get(
                        "run", 0)) >= max_run_attempts):
                    _block_retry_exhausted(root, state, "run")
                    raise ReadError("run retry limit exhausted")
                try:
                    preflight = runner(
                        _executor_command(
                            root, state, preflight=True,
                            special_validated_mixed=special_validated_mixed),
                        cwd=root, text=True, capture_output=True, check=False,
                        timeout=180)
                except (OSError, subprocess.SubprocessError) as error:
                    failure = _exception_failure(error, "preflight")
                    if recovery_retry:
                        _record_recovery_preflight_failure(root, state, failure)
                    finish(_operation_result(
                        item, ("production_readonly_unavailable"
                               if failure["category"] ==
                               "production_readonly_unavailable"
                               else "operation_error"), failure["category"],
                        failure["returncode"], _failure_error(failure),
                        phase="preflight", production_writes=False,
                        **_failure_details(failure)))
                    continue
                if preflight.returncode != 0:
                    failure = _classify_subprocess_failure(
                        preflight, "preflight")
                    if recovery_retry:
                        _record_recovery_preflight_failure(root, state, failure)
                    finish(_operation_result(
                        item, ("production_readonly_unavailable"
                               if failure["category"] ==
                               "production_readonly_unavailable"
                               else "operation_error"), failure["category"],
                        failure["returncode"], _failure_error(failure),
                        phase="preflight", production_writes=False,
                        **_failure_details(failure)))
                    continue
                before = _execution_details(root, fixed[item["post_id"]])
                attempt = _record_attempt_start(root, state, "run")
                completed = runner(
                    _executor_command(
                        root, state, resume=False,
                        special_validated_mixed=special_validated_mixed),
                    cwd=root, text=True, capture_output=True, check=False,
                    timeout=900)
                try:
                    execution = _execution_details(
                        root, fixed[item["post_id"]])
                except ReadError as error:
                    failure = {
                        "category": "executor_state_invalid",
                        "returncode": int(completed.returncode),
                        "stderr_summary": _safe_subprocess_summary(
                            f"{completed.stderr}\n{error}"),
                        "stdout_summary": _safe_subprocess_summary(
                            completed.stdout),
                        "artifacts": _execution_artifacts(
                            root, item["post_id"]),
                    }
                    _block_after_operation_error(
                        root, state, "run", attempt, failure)
                    finish(_operation_result(
                        item, "blocked", failure["category"],
                        failure["returncode"], _failure_error(failure),
                        phase="execute", attempt=attempt,
                        **_failure_details(failure)))
                    continue
                if execution is None:
                    failure = _classify_subprocess_failure(completed, "execute")
                    artifacts = _execution_artifacts(root, item["post_id"])
                    if (
                            failure["category"] in {
                                "transient_network_error",
                                "production_readonly_unavailable"}
                            and not artifacts):
                        _restore_ready_after_transient(
                            root, state, attempt, failure)
                        finish(_operation_result(
                            item, "operation_error", failure["category"],
                            failure["returncode"], _failure_error(failure),
                            phase="execute", attempt=attempt,
                            recovered_to_ready=True, production_writes=False,
                            **_failure_details(failure)))
                        continue
                    failure["artifacts"] = artifacts
                    _block_after_operation_error(
                        root, state, "run", attempt, failure)
                    finish(_operation_result(
                        item, "blocked", failure["category"],
                        failure["returncode"], _failure_error(failure),
                        phase="execute", attempt=attempt,
                        **_failure_details(failure)))
                    continue
                fresh = (
                    before is None
                    or execution["sha256"] != before["sha256"]
                    or execution["mtime_ns"] != before["mtime_ns"]
                )
                if not fresh:
                    failure = _classify_subprocess_failure(completed, "execute")
                    failure["category"] = "stale_execution_state"
                    _block_after_operation_error(
                        root, state, "run", attempt, failure)
                    finish(_operation_result(
                        item, "blocked", failure["category"],
                        failure["returncode"],
                        "single-candidate executor did not update execution state",
                        phase="execute", attempt=attempt,
                        **_failure_details(failure)))
                    continue
                _apply_execution_state(
                    root, state, execution,
                    f"single-candidate executor exited {completed.returncode}")
                if completed.returncode == 0 and execution["status"] == "completed":
                    result = _operation_result(
                        item, "completed", "completed", 0, "",
                        phase="execute", attempt=attempt)
                    _record_attempt_outcome(
                        root, state, "run", attempt, result)
                else:
                    failure = _classify_subprocess_failure(completed, "execute")
                    structured = _structured_execution_failure(execution)
                    if structured:
                        failure.update(structured)
                    if completed.returncode == 0:
                        failure["category"] = "incomplete_execution_state"
                    elif failure["category"] == "executor_failed_without_state":
                        failure["category"] = "executor_failed_with_state"
                    error = (
                        f"single-candidate executor ended in {execution['status']}"
                        if completed.returncode == 0 else _failure_error(failure))
                    result = _operation_result(
                        item, execution["status"], failure["category"],
                        failure["returncode"], error,
                        phase="execute", attempt=attempt,
                        **_failure_details(failure))
                    _record_attempt_outcome(
                        root, state, "run", attempt, result, failure)
                finish(result)
            except KeyboardInterrupt:
                artifacts = _execution_artifacts(root, item["post_id"])
                if (state and state["workflow_status"] == "execution_in_progress"
                        and not artifacts):
                    failure = {
                        "category": "execution_interrupted_before_write",
                        "returncode": 130, "stderr_summary": "user interrupted",
                        "stdout_summary": ""}
                    _restore_ready_after_transient(root, state, attempt, failure)
                    recovered = True
                elif state and state["workflow_status"] == "execution_in_progress":
                    _record_interrupted_execution(root, state, attempt, artifacts)
                    recovered = False
                else:
                    recovered = True
                finish(_operation_result(
                    item, "interrupted", "execution_interrupted", 130,
                    "user interrupted; child process terminated",
                    phase="preflight" if attempt is None else "execute",
                    attempt=attempt, artifacts=artifacts,
                    recovered_to_ready=recovered, production_writes=False))
            except (OSError, subprocess.SubprocessError, ReadError) as error:
                if attempt is None:
                    attempt = int((state.get("retry_counts") or {}).get("run", 0))
                failure = _exception_failure(error, "execute")
                if (recovery_retry and state
                        and state["workflow_status"] == "ready_for_execution"):
                    failure["category"] = "recovery_preflight_failed"
                    _record_recovery_preflight_failure(root, state, failure)
                elif state and state["workflow_status"] == "execution_in_progress":
                    failure["artifacts"] = _execution_artifacts(
                        root, item["post_id"])
                    _block_after_operation_error(
                        root, state, "run", attempt, failure)
                finish(_operation_result(
                    item, "blocked", failure["category"],
                    failure["returncode"], _safe_subprocess_summary(str(error)),
                    phase="execute", attempt=attempt,
                    **_failure_details(failure)))
    return {
        "schema_version": SCHEMA_VERSION, "mode": "execute",
        "repository_root": str(root), "selected_count": len(items),
        "results": results, "writes_performed": bool(items),
        "integrity_ok": True,
    }


RESUME_STATUSES = {
    "excerpt_failed", "translation_failed", "ready_for_translation_resume",
}


def _resume_items(root, batch_id=None, post_id=None, allow_blocked=False):
    root, batches, _, _, states = _context(root)
    batches_by_id = {batch["batch_id"]: batch for batch in batches}
    resumable_statuses = RESUME_STATUSES | ({"blocked"} if allow_blocked else set())
    items = []
    for batch in _select_batch(batches, batch_id):
        for article in batch["articles"]:
            if post_id is not None and article["chinese_post_id"] != int(post_id):
                continue
            state = states.get(article["chinese_post_id"])
            if not state or state["workflow_status"] not in resumable_statuses:
                continue
            execution = _execution_details(root, article)
            reasons = []
            resume_mode = state["workflow_status"] != "excerpt_failed"
            if execution is None:
                reasons.append("execution evidence is missing")
            elif resume_mode and execution["status"] not in {
                    "excerpt_generated", "chinese_excerpt_saved", "translation_started",
                    "translation_failed"}:
                reasons.append(
                    f"execution status cannot resume: {execution['status']}")
            if not resume_mode:
                reasons.append(
                    "existing executor cannot safely restart excerpt after backup creation")
            attempts = int((state.get("retry_counts") or {}).get("resume", 0))
            if attempts >= MAX_RESUME_ATTEMPTS:
                reasons.append("resume retry limit exhausted")
            items.append({
                "batch_id": batch["batch_id"], "post_id": article["chinese_post_id"],
                "english_post_id": article["english_post_id"],
                "resume_mode": resume_mode, "attempts": attempts,
                "special_validated_mixed": _special_validated_mixed_recovery_allowed(
                    batches_by_id[batch["batch_id"]], state),
                "allowed": not reasons, "blocking_reasons": reasons,
            })
    return root, items


def resume(root, execute=False, batch_id=None, post_id=None,
           runner=subprocess.run, allow_blocked=False):
    root, items = _resume_items(root, batch_id, post_id, allow_blocked=allow_blocked)
    if not execute:
        return {
            "schema_version": SCHEMA_VERSION, "mode": "preview",
            "repository_root": str(root), "selected_count": len(items),
            "allowed_count": sum(item["allowed"] for item in items),
            "items": items, "writes_performed": False, "integrity_ok": True,
        }
    results = []
    with InitLock(root):
        root, _, fixed, _, states = _context(root)
        for item in items:
            state = states.get(item["post_id"])
            attempt = None
            try:
                resumable_statuses = RESUME_STATUSES | (
                    {"blocked"} if allow_blocked else set())
                if (not item["allowed"]
                        or state["workflow_status"] not in resumable_statuses):
                    if "resume retry limit exhausted" in item["blocking_reasons"]:
                        _block_retry_exhausted(root, state, "resume")
                    raise ReadError("; ".join(item["blocking_reasons"])
                                    or "article is no longer resumable")
                before = _execution_details(root, fixed[item["post_id"]])
                attempt = _record_attempt_start(root, state, "resume")
                preflight = runner(
                    _executor_command(
                        root, state, resume=True, preflight=True,
                        special_validated_mixed=item["special_validated_mixed"]),
                    cwd=root, text=True, capture_output=True, check=False,
                    timeout=180)
                if preflight.returncode != 0:
                    failure = _classify_subprocess_failure(preflight, "preflight")
                    _block_after_operation_error(
                        root, state, "resume", attempt, failure)
                    results.append(_operation_result(
                        item, "blocked", failure["category"],
                        failure["returncode"], _failure_error(failure),
                        phase="preflight", attempt=attempt,
                        **_failure_details(failure)))
                    continue
                completed = runner(
                    _executor_command(
                        root, state, resume=True,
                        special_validated_mixed=item["special_validated_mixed"]),
                    cwd=root, text=True, capture_output=True, check=False,
                    timeout=900)
                execution = _execution_details(root, fixed[item["post_id"]])
                if execution is None:
                    raise ReadError(
                        f"executor exited {completed.returncode} without execution state")
                fresh = (
                    before is None
                    or execution["sha256"] != before["sha256"]
                    or execution["mtime_ns"] != before["mtime_ns"]
                )
                if not fresh:
                    failure = _classify_subprocess_failure(completed, "execute")
                    failure["category"] = "stale_execution_state"
                    failure["stderr_summary"] = _safe_subprocess_summary(
                        completed.stderr)
                    failure["stdout_summary"] = _safe_subprocess_summary(
                        completed.stdout)
                    _block_after_operation_error(
                        root, state, "resume", attempt, failure)
                    results.append(_operation_result(
                        item, "blocked", failure["category"],
                        failure["returncode"],
                        "single-candidate executor did not update execution state",
                        phase="execute", attempt=attempt,
                        **_failure_details(failure)))
                    continue
                _apply_execution_state(
                    root, state, execution,
                    f"single-candidate resume exited {completed.returncode}")
                if completed.returncode == 0:
                    result = _operation_result(
                        item, execution["status"], "completed", 0, "",
                        phase="execute", attempt=attempt)
                    _record_attempt_outcome(
                        root, state, "resume", attempt, result)
                else:
                    failure = _classify_subprocess_failure(completed, "execute")
                    structured = _structured_execution_failure(execution)
                    if structured:
                        failure.update(structured)
                    if failure["category"] == "executor_failed_without_state":
                        failure["category"] = "executor_failed_with_state"
                    result = _operation_result(
                        item, execution["status"], failure["category"],
                        failure["returncode"], _failure_error(failure),
                        phase="execute", attempt=attempt,
                        **_failure_details(failure))
                    _record_attempt_outcome(
                        root, state, "resume", attempt, result, failure)
                results.append(result)
            except (OSError, subprocess.SubprocessError, ReadError) as error:
                if attempt is None:
                    attempt = int(
                        (state.get("retry_counts") or {}).get("resume", 0))
                failure = _exception_failure(error, "execute")
                if state and state["workflow_status"] == "execution_in_progress":
                    failure["artifacts"] = _execution_artifacts(
                        root, item["post_id"])
                    _block_after_operation_error(
                        root, state, "resume", attempt, failure)
                results.append(_operation_result(
                    item, "blocked", failure["category"],
                    failure["returncode"], _safe_subprocess_summary(str(error)),
                    phase="execute", attempt=attempt,
                    **_failure_details(failure)))
    return {
        "schema_version": SCHEMA_VERSION, "mode": "execute",
        "repository_root": str(root), "selected_count": len(items),
        "results": results, "writes_performed": bool(items),
        "integrity_ok": True,
    }


def _live_chinese_excerpt_empty(article, source_factory=None):
    from src.batch_readonly_ssh import BatchReadonlySshSource
    from src.single_candidate_flow import raw_field

    source = (
        source_factory([article["source_row"]])
        if source_factory else
        BatchReadonlySshSource.fetch([article["source_row"]])
    )
    chinese = source.get_post(article["chinese_post_id"])
    if not isinstance(chinese, dict) or chinese.get("id") != article["chinese_post_id"]:
        raise SafetyError("read-only excerpt observation returned an unexpected post")
    excerpt = chinese.get("excerpt")
    if not isinstance(excerpt, dict) or not isinstance(excerpt.get("raw"), str):
        raise SafetyError("read-only excerpt observation returned an invalid excerpt")
    return not raw_field(chinese, "excerpt").strip()


def _preserve_excerpt_observation_retry(root, post_id):
    """Keep excerpt_generated eligible without guessing run versus resume."""
    root, _, fixed, _, states = _context(root)
    article = fixed[int(post_id)]
    state = states[int(post_id)]
    execution = _execution_details(root, article)
    if execution is None or execution["status"] != "excerpt_generated":
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    previous = state["workflow_status"]
    state["workflow_status"] = "ready_for_execution"
    state["recovery"] = {
        "status": "applied", "stage": "run", "action": "observe",
        "execution_status": execution["status"],
        "execution_sha256": execution["sha256"],
        "recovered_at": timestamp,
    }
    state["updated_at"] = timestamp
    event = _transition_event(
        "excerpt_observation_retry_preserved", state, previous,
        "ready_for_execution",
        "live Chinese excerpt state could not be confirmed; retry preserved",
        {
            "execution_status": execution["status"],
            "execution_sha256": execution["sha256"],
        },
        timestamp, f"excerpt-observation|{execution['sha256']}|{timestamp}")
    _persist_transition(
        root, _state_path(root, state["batch_id"], int(post_id)), state, event)


def _prepare_article_retry(root, post_id, source_factory=None):
    """Use existing execution evidence to choose and audit the next attempt."""
    root, _, fixed, _, states = _context(root)
    article = fixed[int(post_id)]
    state = states[int(post_id)]
    execution = _execution_details(root, article)
    if state["workflow_status"] == "completed" or (
            execution and execution["status"] == "completed"):
        if execution and state["workflow_status"] != "completed":
            _apply_execution_state(root, state, execution,
                                   "completed while preparing batch retry")
        return "completed"
    if execution is None:
        return (
            "run"
            if state["workflow_status"] == "ready_for_execution"
            and not _execution_artifacts(root, post_id)
            else None
        )
    if execution["status"] == "prepared":
        mode = "run"
        target = "ready_for_execution"
        action = "restart"
    elif execution["status"] == "excerpt_generation_failed":
        mode = "run"
        target = "ready_for_execution"
        action = "retry_excerpt_generation"
    elif execution["status"] == "excerpt_generated":
        if _live_chinese_excerpt_empty(article, source_factory):
            mode = "run"
            target = "ready_for_execution"
            action = "restart"
        else:
            mode = "resume"
            target = "ready_for_translation_resume"
            action = "resume"
    elif execution["status"] in {
            "chinese_excerpt_saved", "translation_started",
            "translation_failed"}:
        mode = "resume"
        target = "ready_for_translation_resume"
        action = "resume"
    else:
        return None
    timestamp = datetime.now(timezone.utc).isoformat()
    previous = state["workflow_status"]
    state["workflow_status"] = target
    if mode == "run":
        state["recovery"] = {
            "status": "applied", "stage": "run", "action": action,
            "execution_status": execution["status"],
            "execution_sha256": execution["sha256"],
            "recovered_at": timestamp,
        }
    state["updated_at"] = timestamp
    event = _transition_event(
        "run_attempt_retry_prepared", state, previous, target,
        "prepared next finite whole-article batch attempt",
        {
            "next_mode": mode, "execution_status": execution["status"],
            "execution_sha256": execution["sha256"],
        },
        timestamp, f"batch-retry|{mode}|{execution['sha256']}|{timestamp}")
    _persist_transition(
        root, _state_path(root, state["batch_id"], int(post_id)), state, event)
    return mode


def run_ready(root, execute=False, batch_id=None, post_id=None, runner=subprocess.run,
              progress=None, sleeper=time.sleep,
              max_attempts=MAX_ARTICLE_ATTEMPTS,
              retry_delay=ARTICLE_RETRY_DELAY, source_factory=None):
    root, items = _run_items(root, batch_id, post_id)
    if post_id is not None:
        items = [item for item in items if item["post_id"] == int(post_id)]
    if not execute:
        return {
            "schema_version": SCHEMA_VERSION, "mode": "preview",
            "repository_root": str(root), "selected_count": len(items),
            "allowed_count": sum(item["allowed"] for item in items),
            "items": items, "writes_performed": False, "integrity_ok": True,
        }
    # Preserve the command's fail-fast lock contract before per-article calls.
    with InitLock(root):
        pass
    results = []
    total = len(items)
    for index, item in enumerate(items, 1):
        if progress:
            progress("start", index, total, item, None)
        final = None
        _, _, _, current_executions, current_states = _context(root)
        mode = (
            None if item["post_id"] in current_executions else "run")
        run_limit = (
            int((current_states[item["post_id"]].get("retry_counts") or {}).get(
                "run", 0)) + max_attempts)
        for article_attempt in range(1, max_attempts + 1):
            if mode is None:
                try:
                    mode = _prepare_article_retry(
                        root, item["post_id"], source_factory=source_factory)
                except ReadError as error:
                    if final is None:
                        failure = _exception_failure(error, "preflight")
                        final = _operation_result(
                            item, "retry_not_allowed", failure["category"],
                            failure["returncode"], _failure_error(failure),
                            attempts=article_attempt,
                            **_failure_details(failure))
                    break
                except (OSError, subprocess.SubprocessError,
                        SafetyError) as error:
                    if article_attempt > 1 and progress:
                        progress("attempt", index, total, item, {
                            "attempts": article_attempt,
                            "mode": "excerpt-observation"})
                    _preserve_excerpt_observation_retry(
                        root, item["post_id"])
                    failure = _exception_failure(error, "preflight")
                    final = _operation_result(
                        item, "observation_failed", failure["category"],
                        failure["returncode"],
                        "cannot confirm live Chinese excerpt state: "
                        + _failure_error(failure),
                        phase="excerpt_observation",
                        attempts=article_attempt,
                        **_failure_details(failure))
                    if progress:
                        progress("attempt_failed", index, total, item, final)
                    if article_attempt < max_attempts:
                        if progress:
                            progress("retry_wait", index, total, item, {
                                "attempts": article_attempt + 1,
                                "delay": retry_delay})
                        sleeper(retry_delay)
                    continue
            if mode is None:
                if final is None:
                    final = _operation_result(
                        item, "retry_not_allowed", "retry_not_allowed", -1,
                        "current execution state cannot be retried safely",
                        attempts=article_attempt)
                break
            if article_attempt > 1 and progress:
                progress("attempt", index, total, item, {
                    "attempts": article_attempt, "mode": mode})
            if mode == "completed":
                final = _operation_result(
                    item, "completed", "completed", 0, "",
                    attempts=article_attempt - 1)
            else:
                operation = (
                    _run_ready_once(
                        root, execute=True, batch_id=item["batch_id"],
                        post_id=item["post_id"], runner=runner,
                        max_run_attempts=run_limit)
                    if mode == "run" else
                    resume(
                        root, execute=True, batch_id=item["batch_id"],
                        post_id=item["post_id"], runner=runner)
                )
                if operation["results"]:
                    final = dict(operation["results"][0])
                else:
                    final = _operation_result(
                        item, "blocked", "retry_not_allowed", -1,
                        f"{mode} attempt was not eligible")
                final["attempts"] = article_attempt
            if final["result"] == "completed":
                break
            if final["result"] in {
                    "excerpt_rejected", "rejected_excerpt_generation"}:
                final["candidate_attempts"] = article_attempt
                execution = _execution_details(root, {
                    "chinese_post_id": item["post_id"],
                    "english_post_id": item["english_post_id"],
                })
                if execution and execution.get("excerpt_generation_attempts") is not None:
                    final["excerpt_generation_attempts"] = execution[
                        "excerpt_generation_attempts"]
                break
            if final.get("category") == "protected_token_validation_error":
                final["execution_evidence"] = _relative(
                    _execution_path(root, item["post_id"]), root)
                final["next_step"] = (
                    "fix the Chinese source if needed, then run recover "
                    f"--post-id {item['post_id']} --execute")
                break
            if final.get("category") in {
                    "production_readonly_unavailable", "execution_interrupted"}:
                final["next_step"] = (
                    "rerun the same run-ready --execute command later"
                    if final["category"] == "production_readonly_unavailable"
                    else "run recover for this post before continuing")
                break
            if final.get("phase") == "preflight":
                # A completed preflight is a deterministic safety decision;
                # retrying the same immutable candidate cannot make it pass.
                break
            if progress:
                progress("attempt_failed", index, total, item, final)
            if article_attempt == max_attempts:
                break
            mode = None
            if progress:
                progress("retry_wait", index, total, item, {
                    "attempts": article_attempt + 1,
                    "delay": retry_delay})
            sleeper(retry_delay)
        results.append(final)
        if progress:
            progress(
                "finish" if final["result"] == "completed" else "final_failed",
                index, total, item, final)
            if final["result"] != "completed" and index < total:
                if final.get("category") not in {
                        "production_readonly_unavailable", "execution_interrupted"}:
                    progress("continue", index, total, item, final)
        if final.get("category") in {
                "production_readonly_unavailable", "execution_interrupted"}:
            break
    completed_count = sum(item["result"] == "completed" for item in results)
    failed_count = sum(item["result"] != "completed" for item in results)
    stopped_early = len(results) < total
    infrastructure_unavailable = bool(results and results[-1].get("category") ==
                                      "production_readonly_unavailable")
    interrupted = bool(results and results[-1].get("category") ==
                       "execution_interrupted")
    return {
        "schema_version": SCHEMA_VERSION, "mode": "execute",
        "repository_root": str(root), "selected_count": len(items),
        "results": results, "completed_count": completed_count,
        "failed_count": failed_count, "pending_count": total - len(results),
        "processed_count": len(results), "stopped_early": stopped_early,
        "production_readonly_unavailable": infrastructure_unavailable,
        "interrupted": interrupted,
        "writes_performed": any(item.get("production_writes", True)
                                for item in results),
        "integrity_ok": not (infrastructure_unavailable or interrupted),
        "exit_code": EXIT_WRITE_ERROR,
    }


def _event_recovery_generation(events, index):
    """Return an event's explicit or restart-bound recovery generation."""
    event = events[index]
    evidence = event.get("evidence") or {}
    generation = evidence.get("recovery_generation")
    if generation is not None:
        try:
            return int(generation)
        except (TypeError, ValueError):
            return None
    active_generation = 0
    for prior in events[:index]:
        if (prior.get("chinese_post_id") != event.get("chinese_post_id")
                or prior.get("event_type") != "failed_execution_restarted"):
            continue
        try:
            active_generation = int((prior.get("evidence") or {})[
                "recovery_generation"])
        except (KeyError, TypeError, ValueError):
            continue
    return active_generation


def _resume_attempt_numbers(events, post_id, event_types, stage="resume",
                            recovery_generation=None):
    attempts = set()
    for index, event in enumerate(events):
        if (
                event.get("chinese_post_id") == int(post_id)
                and event.get("event_type") in event_types):
            evidence = event.get("evidence") or {}
            if (evidence.get("stage") == stage
                    and (recovery_generation is None
                         or _event_recovery_generation(events, index)
                         == int(recovery_generation))):
                try:
                    attempts.add(int(evidence["attempt"]))
                except (KeyError, TypeError, ValueError):
                    pass
    return attempts


def _reconciled_attempt_numbers(events, post_id, stage, recovery_generation=None):
    resolved = set()
    for index, event in enumerate(events):
        if (
                event.get("chinese_post_id") == int(post_id)
                and event.get("event_type")
                == f"{stage}_orphaned_attempts_reconciled"
                and (recovery_generation is None
                     or _event_recovery_generation(events, index)
                     == int(recovery_generation))):
            for attempt in (event.get("evidence") or {}).get(
                    "orphaned_attempts", []):
                try:
                    resolved.add(int(attempt))
                except (TypeError, ValueError):
                    pass
    return resolved


def reconcile_attempts(root, post_id, apply=False, stage="resume",
                       chinese_excerpt_empty=None):
    if stage not in {"run", "resume"}:
        raise ReadError(f"unsupported attempt stage: {stage}")
    root, _, fixed, _, states = _context(root)
    article = fixed.get(int(post_id))
    if article is None:
        raise ReadError(f"Chinese post {post_id} is outside fixed batches")
    state = states.get(int(post_id))
    if state is None:
        raise ReadError(f"coordination state is missing for Chinese post {post_id}")
    events = _read_events(_events_path(root, state["batch_id"]))
    recovery_generation = state.get("recovery_generation")
    started = _resume_attempt_numbers(
        events, post_id, {f"{stage}_attempt_started"}, stage=stage,
        recovery_generation=recovery_generation)
    terminated = _resume_attempt_numbers(
        events, post_id,
        {f"{stage}_attempt_completed", f"{stage}_attempt_failed"},
        stage=stage, recovery_generation=recovery_generation)
    reconciled = _reconciled_attempt_numbers(
        events, post_id, stage, recovery_generation=recovery_generation)
    orphaned = sorted(started - terminated - reconciled)
    valid_count = len(terminated)
    current_count = int((state.get("retry_counts") or {}).get(stage, 0))
    counter_drift = current_count != valid_count
    execution = _execution_details(root, article)
    recover_failed_run = bool(
        stage == "run" and state["workflow_status"] == "blocked"
        and terminated and not orphaned and execution
        and execution["status"] in {
            "prepared", "excerpt_generated", "chinese_excerpt_saved",
            "translation_started", "translation_failed"})
    reasons = []
    target_status = state["workflow_status"]
    recovery_action = None
    if state["workflow_status"] == "completed":
        reasons.append("completed article is not eligible")
    if not orphaned and not recover_failed_run and not counter_drift:
        reasons.append(f"no orphaned {stage} attempts")
    if stage == "run" and state["workflow_status"] != "completed":
        if state["workflow_status"] != "blocked":
            reasons.append(
                f"workflow_status is {state['workflow_status']}, not blocked")
        if execution is None:
            reasons.append("execution evidence is missing")
        elif execution["status"] == "prepared":
            target_status = "ready_for_execution"
            recovery_action = "restart"
        elif execution["status"] == "excerpt_generated":
            if chinese_excerpt_empty is None:
                reasons.append(
                    "excerpt_generated recovery requires explicit Chinese excerpt state")
            elif chinese_excerpt_empty:
                target_status = "ready_for_execution"
                recovery_action = "restart"
            else:
                target_status = "ready_for_translation_resume"
                recovery_action = "resume"
        elif execution["status"] in {
                "chinese_excerpt_saved", "translation_started",
                "translation_failed"}:
            target_status = "ready_for_translation_resume"
            recovery_action = "resume"
        elif execution["status"] != "completed":
            reasons.append(
                f"execution status cannot be recovered: {execution['status']}")
    item = {
        "batch_id": state["batch_id"], "post_id": int(post_id),
        "stage": stage, "execution_status": (
            execution["status"] if execution else None),
        "orphaned_attempts": orphaned, "terminated_attempts": sorted(terminated),
        "current_attempt_count": current_count,
        "corrected_attempt_count": valid_count,
        "counter_drift": counter_drift,
        "reconciliation_action": (
            "orphaned_attempt_reconciliation" if orphaned else
            "counter_drift_correction" if counter_drift else None),
        "target_workflow_status": target_status,
        "recovery_action": recovery_action,
    }
    if stage == "resume":
        item.update({
            "current_resume_count": current_count,
            "corrected_resume_count": valid_count,
        })
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "preview",
        "repository_root": str(root), "planned_count": int(not reasons),
        "changed_count": 0, "items": [item],
        "eligible": not reasons, "changed": False,
        "blocking_reasons": reasons, "writes_performed": False,
        "integrity_ok": True,
    }
    if not apply or reasons:
        return result
    with InitLock(root):
        root, _, fixed, _, states = _context(root)
        state = states[int(post_id)]
        events = _read_events(_events_path(root, state["batch_id"]))
        recovery_generation = state.get("recovery_generation")
        started = _resume_attempt_numbers(
            events, post_id, {f"{stage}_attempt_started"}, stage=stage,
            recovery_generation=recovery_generation)
        terminated = _resume_attempt_numbers(
            events, post_id,
            {f"{stage}_attempt_completed", f"{stage}_attempt_failed"},
            stage=stage, recovery_generation=recovery_generation)
        reconciled = _reconciled_attempt_numbers(
            events, post_id, stage, recovery_generation=recovery_generation)
        orphaned = sorted(started - terminated - reconciled)
        valid_count = len(terminated)
        current_count = int((state.get("retry_counts") or {}).get(stage, 0))
        counter_drift = current_count != valid_count
        recover_failed_run = bool(
            stage == "run" and state["workflow_status"] == "blocked"
            and terminated and not orphaned and execution
            and execution["status"] in {
                "prepared", "excerpt_generated", "chinese_excerpt_saved",
                "translation_started", "translation_failed"})
        if (
                (not orphaned and not recover_failed_run and not counter_drift)
                or state["workflow_status"] == "completed"):
            return result
        attempts = dict(state.get("retry_counts") or {})
        previous_count = int(attempts.get(stage, 0))
        attempts[stage] = (
            valid_count if (orphaned or counter_drift) else previous_count)
        state["retry_counts"] = attempts
        timestamp = datetime.now(timezone.utc).isoformat()
        previous_status = state["workflow_status"]
        state["workflow_status"] = target_status
        if stage == "run":
            state["recovery"] = {
                "status": "applied", "stage": "run",
                "action": recovery_action,
                "execution_status": execution["status"],
                "execution_sha256": execution["sha256"],
                "chinese_excerpt_empty": chinese_excerpt_empty,
                "recovered_at": timestamp,
            }
        state["updated_at"] = timestamp
        event_type = (
            f"{stage}_orphaned_attempts_reconciled"
            if orphaned else "run_failed_attempt_reconciled")
        if counter_drift and not orphaned:
            event_type = f"{stage}_attempt_counter_drift_reconciled"
        event = _transition_event(
            event_type, state,
            previous_status, target_status,
            (
                f"orphaned {stage} attempts removed from retry count"
                if orphaned else
                f"{stage} retry count corrected to the current recovery generation"
                if counter_drift else
                "failed run recovered without changing valid attempt count"
            ),
            {
                "stage": stage, "orphaned_attempts": orphaned,
                "terminated_attempts": sorted(terminated),
                "recovery_generation": recovery_generation,
                "previous_attempt_count": previous_count,
                "corrected_attempt_count": len(terminated),
                "execution_status": execution["status"] if execution else None,
                "recovery_action": recovery_action,
                "chinese_excerpt_empty": chinese_excerpt_empty,
            },
            timestamp,
            (
                f"{stage}|orphan-recovery|" + ",".join(map(str, orphaned))
                if orphaned else
                f"run|failed-recovery|{execution['sha256']}|{timestamp}"
            ),
        )
        _persist_transition(
            root, _state_path(root, state["batch_id"], int(post_id)),
            state, event)
    result.update({
        "changed_count": 1, "changed": True, "writes_performed": True,
    })
    return result


def _blocked_recovery_plan(root, post_id):
    root, _, fixed, _, states = _context(root)
    article = fixed.get(int(post_id))
    if article is None:
        raise ReadError(f"Chinese post {post_id} is outside fixed batches")
    state = states.get(int(post_id))
    if state is None:
        raise ReadError(f"coordination state is missing for Chinese post {post_id}")
    if (
            state["workflow_status"] == "ready_for_execution"
            and state.get("recovery", {}).get("status") == "applied"):
        return root, article, state, [], True
    reasons = []
    if state["workflow_status"] != "blocked":
        reasons.append(f"workflow_status is {state['workflow_status']}, not blocked")
    failure = state.get("last_failure")
    if not isinstance(failure, dict) or failure.get("stage") != "run":
        reasons.append("blocked state is not from a run operation error")
    events = _read_events(_events_path(root, state["batch_id"]))
    if not any(
            event.get("chinese_post_id") == int(post_id)
            and event.get("event_type") == "run_attempt_failed"
            for event in events):
        reasons.append("run failure event is missing")
    artifacts = _execution_artifacts(root, post_id)
    if artifacts:
        reasons.append("execution or write evidence exists: " + ",".join(artifacts))
    try:
        _validation_still_valid(root, state)
    except ReadError as error:
        reasons.append(str(error))
    if state.get("manual_conversion", {}).get("status") != "confirmed":
        reasons.append("manual conversion is not confirmed")
    if state.get("language_review", {}).get("status") != "confirmed":
        reasons.append("language review is not confirmed")
    attempts = int((state.get("retry_counts") or {}).get("run", 0))
    if attempts >= MAX_RUN_ATTEMPTS:
        reasons.append("run retry limit exhausted")
    return root, article, state, reasons, False


def recover_blocked(root, post_id, apply=False):
    root, article, state, reasons, already = _blocked_recovery_plan(
        Path(root).resolve(), post_id)
    base = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "preview",
        "repository_root": str(root),
        "batch_id": state["batch_id"],
        "chinese_post_id": int(post_id),
        "eligible": not reasons,
        "blocking_reasons": reasons,
        "already_recovered": already,
        "previous_status": state["workflow_status"],
        "new_status": (
            "ready_for_execution" if not reasons else state["workflow_status"]),
        "retry_count_run": int((state.get("retry_counts") or {}).get("run", 0)),
        "changed": False, "writes_performed": False,
        "integrity_ok": True,
    }
    if not apply or already or reasons:
        return base
    with InitLock(root):
        root, article, state, reasons, already = _blocked_recovery_plan(
            root, post_id)
        if already:
            return {**base, "already_recovered": True}
        if reasons:
            return {
                **base, "eligible": False, "blocking_reasons": reasons,
                "new_status": state["workflow_status"],
            }
        timestamp = datetime.now(timezone.utc).isoformat()
        previous = state["workflow_status"]
        failure = dict(state["last_failure"])
        state["workflow_status"] = "ready_for_execution"
        state["recovery"] = {
            "status": "applied", "recovered_at": timestamp,
            "reason":
                "no execution, backup, or partial-write evidence; validation valid",
            "preserved_failure": failure,
        }
        state["updated_at"] = timestamp
        event = _transition_event(
            "blocked_run_operation_recovered", state, previous,
            "ready_for_execution",
            "explicitly recovered after confirming no execution or write evidence",
            {
                "retry_count_run":
                    int((state.get("retry_counts") or {}).get("run", 0)),
                "last_failure": failure,
                "validation_evidence": state["validation_evidence"],
            },
            timestamp,
            f"recover-blocked|{failure.get('occurred_at')}|"
            f"{failure.get('attempt')}",
        )
        _persist_transition(
            root, _state_path(root, state["batch_id"], int(post_id)),
            state, event)
        return {
            **base, "previous_status": previous,
            "new_status": "ready_for_execution", "changed": True,
            "writes_performed": True,
        }


def _manual_external_completion_plan(root, post_id, source_factory=None):
    """Read only the facts an operator confirms after external completion."""
    from src.batch_readonly_ssh import BatchReadonlySshSource

    root, _, fixed, _, states = _context(Path(root).resolve())
    article = fixed.get(int(post_id))
    if article is None:
        raise ReadError(f"Chinese post {post_id} is outside fixed batches")
    state = states.get(int(post_id))
    if state is None:
        raise ReadError(f"coordination state is missing for Chinese post {post_id}")
    reasons = []
    if state["workflow_status"] == "completed":
        reasons.append("article is already completed")
        return root, article, state, reasons
    if state["workflow_status"] not in {"translation_failed", "blocked"}:
        reasons.append(
            f"workflow_status {state['workflow_status']} is not eligible for manual completion")
    try:
        source = (source_factory([article["source_row"]]) if source_factory else
                  BatchReadonlySshSource.fetch([article["source_row"]]))
        chinese = source.get_post(article["chinese_post_id"])
        english = source.get_post(article["english_post_id"])
        relation = source.check(article["chinese_post_id"], article["english_post_id"])
    except (SafetyError, OSError) as error:
        reasons.append(f"production read-only check failed: {error}")
        return root, article, state, reasons
    checks = (
        (chinese.get("id") == article["chinese_post_id"], "chinese post ID mismatch"),
        (english.get("id") == article["english_post_id"], "english post ID mismatch"),
        (chinese.get("status") == "publish", "Chinese post is not publish"),
        (english.get("status") == "publish", "English post is not publish"),
        (isinstance(relation, dict)
         and relation.get("chinese_post_id") == article["chinese_post_id"]
         and relation.get("chinese_language") == "zh"
         and relation.get("linked_english_post_id") == article["english_post_id"]
         and relation.get("english_post_id") == article["english_post_id"]
         and relation.get("english_language") == "en"
         and relation.get("linked_chinese_post_id") == article["chinese_post_id"],
         "Polylang relation mismatch"),
        (bool(_raw_post_field(english, "title").strip()), "English title is empty"),
        (bool(_raw_post_field(english, "content").strip()), "English content is empty"),
    )
    reasons.extend(reason for passed, reason in checks if not passed)
    return root, article, state, reasons


def _raw_post_field(post, field):
    value = post.get(field)
    if isinstance(value, dict):
        value = value.get("raw")
    return value if isinstance(value, str) else ""


def mark_manual_completed(root, post_id, confirmed=False, source_factory=None):
    """Confirm an externally completed translation without altering execution evidence."""
    root, article, state, reasons = _manual_external_completion_plan(
        root, post_id, source_factory)
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "confirmed" if confirmed else "preview",
        "repository_root": str(root), "batch_id": state["batch_id"],
        "chinese_post_id": int(post_id), "english_post_id": article["english_post_id"],
        "previous_status": state["workflow_status"], "new_status": "completed",
        "eligible": not reasons, "blocking_reasons": reasons,
        "changed": False, "writes_performed": False, "integrity_ok": True,
    }
    if not confirmed or reasons:
        return result
    with InitLock(root):
        root, article, state, reasons = _manual_external_completion_plan(
            root, post_id, source_factory)
        if reasons:
            return {**result, "eligible": False, "blocking_reasons": reasons}
        timestamp = datetime.now(timezone.utc).isoformat()
        previous = state["workflow_status"]
        state["workflow_status"] = "completed"
        state["manual_completion"] = {
            "status": "confirmed", "method": "manual_external",
            "confirmed_at": timestamp,
        }
        state["updated_at"] = timestamp
        event = _transition_event(
            "manual_external_completion_confirmed", state, previous, "completed",
            "operator confirmed external manual English translation",
            {"chinese_post_id": int(post_id),
             "english_post_id": article["english_post_id"],
             "method": "manual_external"},
            timestamp, f"manual-external-completion|{post_id}|{timestamp}")
        _persist_transition(
            root, _state_path(root, state["batch_id"], int(post_id)), state, event)
    return {**result, "changed": True, "writes_performed": True}


def _read_json_object(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReadError(f"{path}: invalid {label} JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReadError(f"{path}: {label} JSON must be an object")
    return value


def _restart_manifest_row(root, state, article):
    path = _validation_paths(
        root, state["batch_id"], article["chinese_post_id"])[2]
    rows, fields = _read_csv(path)
    _required(fields, set(EXECUTION_MANIFEST_FIELDS), path)
    if len(rows) != 1:
        raise ReadError(f"{path}: expected exactly one execution candidate")
    row = rows[0]
    if (_positive_id(row, "chinese_post_id", path, 1)
            != article["chinese_post_id"]
            or _positive_id(row, "english_post_id", path, 1)
            != article["english_post_id"]):
        raise ReadError(f"{path}: execution candidate post ID mismatch")
    return path, row


def _restart_from_current_plan(root, post_id, source_factory=None,
                               allow_prepared_source_restart_retry=False):
    """Read production and prove an old failed execution can be discarded."""
    from src.candidate_execution import sha256_text
    from src.single_candidate_flow import build_live
    from src.batch_readonly_ssh import BatchReadonlySshSource

    root, batches, fixed, _, states = _context(Path(root).resolve())
    article = fixed.get(int(post_id))
    if article is None:
        raise ReadError(f"Chinese post {post_id} is outside fixed batches")
    state = states.get(int(post_id))
    if state is None:
        raise ReadError(f"coordination state is missing for Chinese post {post_id}")
    batch = next(item for item in batches if item["batch_id"] == article["batch_id"])
    special_validated_mixed = _special_validated_mixed_recovery_allowed(
        batch, state)
    execution_path = _execution_path(root, post_id)
    prewrite_path = _prewrite_path(root, post_id)
    reasons = []
    if state["workflow_status"] == "completed":
        reasons.append("completed article cannot restart from current production")
    if not execution_path.is_file() or not prewrite_path.is_file():
        reasons.append("both execution and pre-write evidence are required")
        return root, article, state, None, None, None, reasons
    execution = _read_json_object(execution_path, "execution")
    prewrite = _read_json_object(prewrite_path, "pre-write")
    for value, name, expected in (
            (execution.get("chinese_post_id"), "execution Chinese", int(post_id)),
            (execution.get("english_post_id"), "execution English", article["english_post_id"]),
            (prewrite.get("chinese_post_id"), "pre-write Chinese", int(post_id)),
            (prewrite.get("english_post_id"), "pre-write English", article["english_post_id"])):
        if value != expected:
            reasons.append(f"{name} post ID mismatch")
    execution_status = _normalized_execution_status(execution.get("status"))
    if execution_status == "translation_failed":
        recovery_kind = "source_changed_after_translation_failure"
        allowed_workflow_statuses = {"blocked", "translation_failed"}
    elif execution_status == "excerpt_generation_failed":
        recovery_kind = "excerpt_generation_retry"
        allowed_workflow_statuses = {"excerpt_failed"}
        if _excerpt_generation_recovery_active(
                state, _file_sha256(execution_path)):
            allowed_workflow_statuses.add("blocked")
    elif (execution_status == "prepared" and _legacy_prepared_excerpt_http_failure(
            state, _file_sha256(execution_path))):
        recovery_kind = "legacy_prepared_excerpt_generation_retry"
        allowed_workflow_statuses = {"blocked"}
    elif (allow_prepared_source_restart_retry
          and execution_status == "prepared"
          and _source_restart_prepared(state, {
              "status": execution_status,
              "sha256": _file_sha256(execution_path),
              "recovery_generation": execution.get("recovery_generation"),
          })):
        recovery_kind = "prepared_source_restart_retry"
        allowed_workflow_statuses = {"blocked"}
        if any(key in execution for key in (
                "generated_excerpt", "excerpt_attempts",
                "translated_post_id", "completed_at")):
            reasons.append(
                "prepared restart evidence contains post-GLM or translation fields")
    elif execution_status == "excerpt_rejected":
        recovery_kind = "rejected_excerpt_regeneration"
        allowed_workflow_statuses = {"excerpt_failed"}
    else:
        recovery_kind = None
        allowed_workflow_statuses = set()
        reasons.append(
            "execution status is neither translation_failed nor excerpt_rejected "
            "nor excerpt_generation_failed")
    if (state["workflow_status"] != "completed"
            and state["workflow_status"] not in allowed_workflow_statuses):
        reasons.append(
            f"workflow_status {state['workflow_status']} is incompatible with "
            f"execution status {execution_status}")

    rejected_evidence = []
    if recovery_kind == "rejected_excerpt_regeneration":
        paths = execution.get("rejected_excerpt_paths")
        if not isinstance(paths, list) or not paths:
            reasons.append("excerpt_rejected evidence paths are required")
        else:
            rejected_root = (
                root / "data/backups/single-candidate/rejected").resolve()
            for value in paths:
                try:
                    path = Path(value)
                    if not path.is_absolute():
                        path = root / path
                    path = path.resolve(strict=True)
                    path.relative_to(rejected_root)
                    if (not path.is_file()
                            or not path.name.startswith(f"chinese-{int(post_id)}-")):
                        raise ValueError("unexpected rejected excerpt file")
                    rejected_evidence.append({
                        "source_path": _relative(path, root),
                        "sha256": _file_sha256(path),
                    })
                except (OSError, ValueError) as error:
                    reasons.append(f"invalid rejected excerpt evidence: {value}: {error}")
    try:
        manifest_path, old_manifest = _restart_manifest_row(root, state, article)
        config = json.loads(
            (root / "config/classification.json").read_text(encoding="utf-8"))
        source = (source_factory([article["source_row"]]) if source_factory else
                  BatchReadonlySshSource.fetch([article["source_row"]]))
        chinese = source.get_post(article["chinese_post_id"])
        english = source.get_post(article["english_post_id"])
        live = build_live(
            old_manifest, chinese, english,
            source.check(article["chinese_post_id"], article["english_post_id"]),
            config, special_validated_mixed=special_validated_mixed)
    except (SafetyError, OSError, UnicodeError, json.JSONDecodeError) as error:
        reasons.append(f"production read-only check failed: {error}")
        return root, article, state, execution, prewrite, None, reasons
    from src.analyzer import analyze_content
    analysis = analyze_content(live["chinese_content"], config)
    counts = analysis["blocks"]["counts"]
    old_generated_excerpt = execution.get("generated_excerpt")
    old_generated_nonempty = bool(
        isinstance(old_generated_excerpt, str) and old_generated_excerpt.strip())
    current_excerpt = live["chinese_excerpt"]
    current_excerpt_empty = not current_excerpt.strip()
    current_matches_old_generated = bool(
        old_generated_nonempty and current_excerpt == old_generated_excerpt)
    excerpt_state = (
        "empty" if current_excerpt_empty else
        "known_previous_generated_excerpt" if current_matches_old_generated else
        "unknown_nonempty"
    )
    checks = {
        "chinese_id": chinese.get("id") == article["chinese_post_id"],
        "english_id": english.get("id") == article["english_post_id"],
        "chinese_publish": live["chinese_status"] == "publish",
        "english_publish": live["english_status"] == "publish",
        "polylang_english": live["linked_english_post_id"] == article["english_post_id"],
        "gutenberg": live["is_gutenberg"],
        "syntaxhighlighter_zero": analysis["syntaxhighlighter_count"] == 0,
        "chinese_excerpt_known_for_restart": (
            current_excerpt_empty if recovery_kind == "rejected_excerpt_regeneration"
            else current_excerpt_empty or current_matches_old_generated),
        "execution_eligibility": (
            live["special_mixed_structure_eligible"]
            if special_validated_mixed else live["phase1_eligible"]),
    }
    old_hashes = prewrite.get("sha256") if isinstance(prewrite.get("sha256"), dict) else {}
    current_title_sha = sha256_text(live["chinese_title"])
    source_changed = (
        current_title_sha != old_hashes.get("chinese_title")
        or live["chinese_content_sha256"] != old_hashes.get("chinese_content")
    )
    if recovery_kind == "rejected_excerpt_regeneration":
        checks["chinese_title_unchanged"] = (
            current_title_sha == old_hashes.get("chinese_title"))
        checks["chinese_content_unchanged"] = (
            live["chinese_content_sha256"] == old_hashes.get("chinese_content"))
        checks["old_generated_excerpt_absent"] = not old_generated_nonempty
        checks["rejected_excerpt_evidence_complete"] = bool(
            rejected_evidence) and len(rejected_evidence) == len(
                execution.get("rejected_excerpt_paths") or [])
    else:
        checks["chinese_source_unchanged"] = not source_changed
    checks.update({
        f"english_{field}_unchanged": live[f"english_{field}_sha256"] == old_hashes.get(f"english_{field}")
        for field in ("title", "excerpt", "content")
    })
    reasons.extend(
        name for name, passed in checks.items()
        if not passed and name != "chinese_source_unchanged")
    current = {
        "live": live, "checks": checks, "manifest_path": manifest_path,
        "old_manifest": old_manifest,
        "current_chinese_title_sha256": current_title_sha,
        "current_chinese_content_sha256": live["chinese_content_sha256"],
        "current_excerpt_sha256": sha256_text(current_excerpt),
        "old_generated_excerpt_sha256": (
            sha256_text(old_generated_excerpt) if old_generated_nonempty else None),
        "current_excerpt_matches_old_generated": current_matches_old_generated,
        "restart_excerpt_state": excerpt_state,
        "current_code_block_pro_count": counts.get(
            "kevinbatdorf/code-block-pro", 0),
        "current_syntaxhighlighter_count": analysis["syntaxhighlighter_count"],
        "recovery_kind": recovery_kind,
        "rejected_excerpt_evidence": rejected_evidence,
    }
    return root, article, state, execution, prewrite, current, reasons


def _prepared_source_restart_retry_plan(root, post_id, source_factory=None):
    """Prove a failed first run of a rebuilt generation is safe to rerun.

    A prepared execution state is deliberately not resumable: the single
    candidate flow writes it before asking GLM for an excerpt.  This narrow
    plan therefore authorizes a fresh run only after rechecking the rebuilt
    baseline and the transient failure evidence.
    """
    root, article, state, execution, prewrite, current, reasons = (
        _restart_from_current_plan(
            root, post_id, source_factory,
            allow_prepared_source_restart_retry=True))
    failure = state.get("last_failure") or {}
    if (failure.get("stage") != "run"
            or failure.get("reason") != "transient_network_error"):
        reasons.append("last_failure is not a transient_network_error from run")
    if not (current and current.get("recovery_kind")
            == "prepared_source_restart_retry"):
        reasons.append("execution is not a current prepared source-restart generation")
    restart = state.get("source_restart_recovery", {})
    if restart.get("recovery_kind") != (
            "source_changed_after_translation_failure"):
        reasons.append("source restart recovery kind is not translation failure")
    if (execution.get("restart_excerpt_state") != restart.get("restart_excerpt_state")
            or execution.get("expected_pre_run_excerpt_sha256")
            != restart.get("expected_pre_run_excerpt_sha256")
            or prewrite.get("recovery_generation")
            != state.get("recovery_generation")):
        reasons.append("prepared restart generation baseline evidence is incomplete")
    if (current and execution.get("expected_pre_run_excerpt_sha256")
            != current.get("current_excerpt_sha256")):
        reasons.append("current Chinese excerpt differs from prepared restart baseline")
    if current and current.get("restart_excerpt_state") != "empty":
        reasons.append("rebuilt Chinese excerpt is not empty")
    return root, article, state, execution, prewrite, current, reasons


def _restore_prepared_source_restart_for_retry(root, post_id, source_factory=None):
    """Move only a fully revalidated prepared restart back to ready-to-run."""
    root, article, state, _, _, current, reasons = (
        _prepared_source_restart_retry_plan(root, post_id, source_factory))
    base = {
        "eligible": not reasons, "blocking_reasons": reasons,
        "changed": False, "writes_performed": False,
    }
    if reasons:
        return base
    with InitLock(root):
        root, article, state, execution, _, current, reasons = (
            _prepared_source_restart_retry_plan(root, post_id, source_factory))
        if reasons:
            return {**base, "eligible": False, "blocking_reasons": reasons}
        timestamp = datetime.now(timezone.utc).isoformat()
        previous = state["workflow_status"]
        execution_sha = _file_sha256(_execution_path(root, post_id))
        state["workflow_status"] = "ready_for_execution"
        state["recovery"] = {
            "status": "applied", "action": "retry_prepared_source_restart",
            "recovered_at": timestamp, "preserved_failure": state["last_failure"],
            "recovery_generation": state["recovery_generation"],
            "execution_sha256": execution_sha,
        }
        state["updated_at"] = timestamp
        event = _transition_event(
            "prepared_source_restart_transient_recovered", state, previous,
            "ready_for_execution",
            "prepared rebuilt generation failed before excerpt generation; rerun authorized",
            {"recovery_generation": state["recovery_generation"],
             "execution_sha256": execution_sha,
             "last_failure": state["last_failure"],
             "safety_checks": current["checks"]},
            timestamp,
            f"prepared-restart-retry|{post_id}|{execution_sha}")
        _persist_transition(
            root, _state_path(root, state["batch_id"], int(post_id)), state, event)
    return {**base, "changed": True, "writes_performed": True}


def _restore_excerpt_generation_for_retry(root, post_id, source_factory=None):
    """Reauthorize a no-write GLM request failure after fresh read-only checks."""
    allowed_kinds = {
        "excerpt_generation_retry",
        "legacy_prepared_excerpt_generation_retry",
    }
    root, _, state, execution, _, current, reasons = (
        _restart_from_current_plan(root, post_id, source_factory))
    if not current or current.get("recovery_kind") not in allowed_kinds:
        reasons.append("execution is not a recoverable excerpt generation failure")
    base = {
        "eligible": not reasons, "blocking_reasons": reasons,
        "changed": False, "writes_performed": False,
    }
    if reasons:
        return base
    with InitLock(root):
        root, _, state, execution, _, current, reasons = (
            _restart_from_current_plan(root, post_id, source_factory))
        if not current or current.get("recovery_kind") not in allowed_kinds:
            reasons.append("execution is not a recoverable excerpt generation failure")
        if reasons:
            return {**base, "eligible": False, "blocking_reasons": reasons}
        timestamp = datetime.now(timezone.utc).isoformat()
        previous = state["workflow_status"]
        execution_sha = _file_sha256(_execution_path(root, post_id))
        preserved_failure = dict(state.get("last_failure") or {})
        state["workflow_status"] = "ready_for_execution"
        state["recovery"] = {
            "status": "applied", "stage": "run",
            "action": "retry_excerpt_generation",
            "recovered_at": timestamp, "preserved_failure": preserved_failure,
            "execution_status": execution["status"],
            "execution_sha256": execution_sha,
            "recovery_kind": current["recovery_kind"],
        }
        state["updated_at"] = timestamp
        event = _transition_event(
            "excerpt_generation_failure_recovered", state, previous,
            "ready_for_execution",
            "GLM request failed before any Chinese excerpt write; retry authorized",
            {"execution_status": execution["status"],
             "execution_sha256": execution_sha,
             "preserved_failure": preserved_failure,
             "recovery_kind": current["recovery_kind"],
             "safety_checks": current["checks"]}, timestamp,
            f"excerpt-generation-retry|{post_id}|{execution_sha}")
        _persist_transition(
            root, _state_path(root, state["batch_id"], int(post_id)), state, event)
    return {**base, "changed": True, "writes_performed": True}


def restart_from_current(root, post_id, apply=False, source_factory=None,
                         reason=None):
    root, article, state, execution, prewrite, current, reasons = (
        _restart_from_current_plan(root, post_id, source_factory))
    base = {
        "schema_version": SCHEMA_VERSION, "mode": "apply" if apply else "preview",
        "repository_root": str(root), "batch_id": state["batch_id"],
        "chinese_post_id": int(post_id), "english_post_id": article["english_post_id"],
        "eligible": not reasons, "blocking_reasons": reasons,
        "previous_status": state["workflow_status"],
        "new_status": "ready_for_execution" if not reasons else state["workflow_status"],
        "safety_checks": current["checks"] if current else {},
        "excerpt_observation": ({
            "current_excerpt_sha256": current["current_excerpt_sha256"],
            "old_generated_excerpt_sha256": current["old_generated_excerpt_sha256"],
            "current_excerpt_matches_old_generated":
                current["current_excerpt_matches_old_generated"],
            "restart_excerpt_state": current["restart_excerpt_state"],
            "recovery_kind": current["recovery_kind"],
        } if current else {}),
        "changed": False, "writes_performed": False, "integrity_ok": True,
    }
    if not apply or reasons:
        return base
    with InitLock(root):
        root, article, state, execution, prewrite, current, reasons = (
            _restart_from_current_plan(root, post_id, source_factory))
        if reasons:
            return {**base, "eligible": False, "blocking_reasons": reasons,
                    "new_status": state["workflow_status"]}
        from src.candidate_execution import backup_record
        timestamp = datetime.now(timezone.utc)
        timestamp_text = timestamp.isoformat()
        generation = int(state.get("recovery_generation", 0)) + 1
        archive = root / RESTART_ARCHIVE_ROOT / (
            f"chinese-{int(post_id)}") / timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
        archive.mkdir(parents=True, mode=0o700)
        execution_path = _execution_path(root, post_id)
        prewrite_path = _prewrite_path(root, post_id)
        old_execution_sha = _file_sha256(execution_path)
        old_prewrite_sha = _file_sha256(prewrite_path)
        shutil.copy2(execution_path, archive / execution_path.name)
        shutil.copy2(prewrite_path, archive / prewrite_path.name)
        if (_file_sha256(archive / execution_path.name) != old_execution_sha
                or _file_sha256(archive / prewrite_path.name) != old_prewrite_sha):
            raise ReadError("archived execution or pre-write SHA-256 mismatch")
        archived_rejected = []
        if current["rejected_excerpt_evidence"]:
            rejected_archive = archive / "rejected"
            rejected_archive.mkdir(mode=0o700)
            for evidence in current["rejected_excerpt_evidence"]:
                source_path = root / evidence["source_path"]
                archived_path = rejected_archive / source_path.name
                shutil.copy2(source_path, archived_path)
                archived_sha = _file_sha256(archived_path)
                if archived_sha != evidence["sha256"]:
                    raise ReadError("archived rejected excerpt SHA-256 mismatch")
                archived_rejected.append({
                    **evidence,
                    "archive_path": _relative(archived_path, root),
                    "archive_sha256": archived_sha,
                })
        recovery_reason = reason or (
            "Regenerate excerpts rejected by the corrected validator"
            if current["recovery_kind"] == "rejected_excerpt_regeneration"
            else "Chinese source edited after failed execution")
        recovery_snapshot = {
            "schema_version": 1, "batch_id": state["batch_id"],
            "chinese_post_id": int(post_id), "english_post_id": article["english_post_id"],
            "recovery_generation": generation, "recovered_at": timestamp_text,
            "reason": recovery_reason, "old_execution_sha256": old_execution_sha,
            "old_pre_write_sha256": old_prewrite_sha,
            "old_execution_status": execution.get("status"),
            "old_generated_excerpt": execution.get("generated_excerpt"),
            "current_chinese_title_sha256": current["current_chinese_title_sha256"],
            "current_chinese_content_sha256": current["current_chinese_content_sha256"],
            "current_excerpt_sha256": current["current_excerpt_sha256"],
            "old_generated_excerpt_sha256": current["old_generated_excerpt_sha256"],
            "current_excerpt_matches_old_generated":
                current["current_excerpt_matches_old_generated"],
            "restart_excerpt_state": current["restart_excerpt_state"],
            "recovery_kind": current["recovery_kind"],
            "rejected_excerpt_evidence": archived_rejected,
            "preserved_last_failure": state.get("last_failure"),
            "preserved_retry_counts": state.get("retry_counts", {}),
            "safety_checks": current["checks"],
        }
        _atomic_write_json(archive / "recovery.json", recovery_snapshot)
        live = current["live"]
        fresh_prewrite = backup_record(
            current["old_manifest"], live, executed_at=timestamp_text,
            model="glm-4.7", status="prepared")
        fresh_prewrite["recovery_generation"] = generation
        _atomic_write_json(prewrite_path, fresh_prewrite)
        fresh_execution = {
            "schema_version": 1, "chinese_post_id": int(post_id),
            "english_post_id": article["english_post_id"], "status": "prepared",
            "started_at": timestamp_text, "backup_path": str(prewrite_path),
            "recovery_generation": generation,
            "restart_excerpt_state": current["restart_excerpt_state"],
            "expected_pre_run_excerpt_sha256": current["current_excerpt_sha256"],
        }
        _atomic_write_json(execution_path, fresh_execution)
        manifest = dict(current["old_manifest"])
        manifest.update({
            "chinese_title": live["chinese_title"],
            "chinese_content_sha256": live["chinese_content_sha256"],
            "chinese_excerpt_empty": "True",
            "english_title_sha256": live["english_title_sha256"],
            "english_excerpt_sha256": live["english_excerpt_sha256"],
            "english_content_sha256": live["english_content_sha256"],
            # This is a new execution baseline.  Current content, rather than
            # the old migration validation, defines its Code Block Pro count.
            "expected_code_block_pro_count": str(
                current["current_code_block_pro_count"]),
            "expected_syntaxhighlighter_count": str(
                current["current_syntaxhighlighter_count"]),
            "execution_status": "pending",
        })
        _atomic_write_csv(current["manifest_path"], EXECUTION_MANIFEST_FIELDS, [manifest])
        previous = state["workflow_status"]
        prior_counts = dict(state.get("retry_counts") or {})
        lifetime = dict(state.get("lifetime_retry_counts") or {})
        for key, value in prior_counts.items():
            lifetime[key] = int(lifetime.get(key, 0)) + int(value)
        state.update({
            "workflow_status": "ready_for_execution", "updated_at": timestamp_text,
            "recovery_generation": generation, "lifetime_retry_counts": lifetime,
            "retry_counts": {"run": 0, "resume": 0},
            "execution_evidence": {
                "source_file": _relative(execution_path, root),
                "sha256": _file_sha256(execution_path), "status": "prepared",
                "recovery_generation": generation,
            },
            "source_restart_recovery": {
                "status": "applied", "generation": generation,
                "recovered_at": timestamp_text, "reason": recovery_reason,
                "archive": _relative(archive, root),
                "recovery_kind": current["recovery_kind"],
                "restart_excerpt_state": current["restart_excerpt_state"],
                "expected_pre_run_excerpt_sha256": current["current_excerpt_sha256"],
                "preserved_last_failure": state.get("last_failure"),
            },
        })
        event = _transition_event(
            "failed_execution_restarted", state, previous,
            "ready_for_execution", recovery_reason, recovery_snapshot, timestamp_text,
            f"restart-current|{generation}|{old_execution_sha}|{old_prewrite_sha}")
        _persist_transition(root, _state_path(root, state["batch_id"], int(post_id)), state, event)
        return {**base, "previous_status": previous,
                "new_status": "ready_for_execution", "changed": True,
                "writes_performed": True, "recovery_generation": generation,
                "archive": _relative(archive, root)}


def recover(root, post_id, execute=False, source_factory=None,
            runner=subprocess.run):
    """Select and, when requested, run one evidence-based recovery path."""
    # Terminal states are decided entirely from coordination evidence.  This
    # must precede every production observation so recovery remains a no-op
    # even when production read-only access is unavailable.
    root, _, fixed, _, states = _context(Path(root).resolve())
    article = fixed.get(int(post_id))
    if article is None:
        raise ReadError(f"Chinese post {post_id} is outside fixed batches")
    state = states.get(int(post_id))
    if state is None:
        raise ReadError(f"coordination state is missing for Chinese post {post_id}")
    if state["workflow_status"] == "completed":
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "execute" if execute else "preview",
            "repository_root": str(root), "batch_id": state["batch_id"],
            "post_id": int(post_id),
            "english_post_id": article["english_post_id"],
            "current_status": "completed", "actual_error": "none",
            "execution_evidence": _relative(_execution_path(root, post_id), root),
            "production_source": "not_required", "strategy": "none",
            "strategy_reasons": ["文章已经完成，无需恢复"],
            "will_execute": "none", "baseline_rebuild": "not_run",
            "reexecution": "not_run", "final_status": "completed",
            "writes_performed": False, "integrity_ok": True,
        }
    artifacts = _execution_artifacts(root, post_id)
    failure = state.get("last_failure") or {}
    failure_text = "\n".join(str(failure.get(key, "")) for key in (
        "reason", "stderr_summary", "stdout_summary"))
    failure_category = _classify_subprocess_failure(type("Failure", (), {
        "returncode": failure.get("returncode", -1),
        "stderr": failure_text, "stdout": ""})(), "preflight")["category"]
    safe_old_readonly_failure = bool(
        state["workflow_status"] == "blocked"
        and failure_category == "production_readonly_unavailable"
        and not artifacts)
    safe_orphaned_start = bool(
        state["workflow_status"] == "execution_in_progress"
        and not artifacts)
    if safe_old_readonly_failure or safe_orphaned_start:
        actual_error = (
            "production_readonly_unavailable" if safe_old_readonly_failure
            else "execution_interrupted_before_write")
        reason = (
            "旧预检只读基础设施失败且没有任何执行或写入 evidence"
            if safe_old_readonly_failure else
            "执行中断但没有 execution/pre-write evidence，可证明尚未进入写入流程")
        base = {
            "schema_version": SCHEMA_VERSION,
            "mode": "execute" if execute else "preview",
            "repository_root": str(root), "batch_id": state["batch_id"],
            "post_id": int(post_id),
            "english_post_id": article["english_post_id"],
            "current_status": state["workflow_status"],
            "actual_error": actual_error,
            "execution_evidence": _relative(_execution_path(root, post_id), root),
            "production_source": "not_required", "strategy": "retry_from_ready",
            "strategy_reasons": [reason],
            "will_execute": "retry_from_ready -> execute_single_candidate",
            "baseline_rebuild": "not_required", "reexecution": "not_run",
            "final_status": state["workflow_status"],
            "writes_performed": False, "integrity_ok": True,
        }
        if not execute:
            return base
        with InitLock(root):
            _, _, fixed_locked, _, states_locked = _context(root)
            current_state = states_locked[int(post_id)]
            if _execution_artifacts(root, post_id):
                return {**base, "strategy": "blocked",
                        "strategy_reasons": [
                            "execution or write evidence appeared during recovery"]}
            timestamp = datetime.now(timezone.utc).isoformat()
            previous = current_state["workflow_status"]
            current_state["workflow_status"] = "ready_for_execution"
            current_state["recovery"] = {
                "status": "applied", "action": "retry_from_ready",
                "recovered_at": timestamp, "preserved_failure": failure,
                "reason": reason}
            current_state["updated_at"] = timestamp
            event = _transition_event(
                "prewrite_interruption_recovered", current_state, previous,
                "ready_for_execution", reason,
                {"artifacts": [], "preserved_failure": failure}, timestamp,
                f"retry-ready|{post_id}|{timestamp}")
            _persist_transition(
                root, _state_path(root, current_state["batch_id"], int(post_id)),
                current_state, event)
        operation = _run_ready_once(
            root, execute=True, batch_id=state["batch_id"], post_id=post_id,
            runner=runner,
            max_run_attempts=int((state.get("retry_counts") or {}).get(
                "run", 0)) + 1)
        item = operation["results"][0] if operation.get("results") else None
        _, _, _, _, fresh_states = _context(root)
        final_status = fresh_states[int(post_id)]["workflow_status"]
        return {**base, "reexecution": (
                    "completed" if item and item["result"] == "completed"
                    else "failed"), "final_status": final_status,
                "operation_result": item,
                "next_step": _recover_next_step(final_status, post_id),
                "writes_performed": True}
    root, article, state, execution, prewrite, current, reasons = (
        _restart_from_current_plan(
            root, post_id, source_factory,
            allow_prepared_source_restart_retry=True))
    unavailable = any("production_readonly_unavailable" in reason
                      for reason in reasons)
    source_changed = bool(
        current and not current["checks"].get("chinese_source_unchanged", True))
    execution_status = _normalized_execution_status(
        execution.get("status")) if execution else None
    strategy = "blocked"
    strategy_reasons = list(reasons)
    if unavailable:
        strategy_reasons = [
            "production_readonly_unavailable: production data was not "
            "obtained; cannot determine whether Chinese content changed"]
    elif (state["workflow_status"] == "excerpt_failed"
          and execution_status == "excerpt_rejected"
          and current
          and current.get("recovery_kind") == "rejected_excerpt_regeneration"
          and not reasons):
        strategy = "rejected_excerpt_regeneration"
        strategy_reasons = [
            "已有中文摘要连续生成失败，生产中文源及英文源仍与 pre-write 基线一致"]
    elif (current and current.get("recovery_kind")
          == "prepared_source_restart_retry"):
        _, _, _, _, _, _, retry_reasons = _prepared_source_restart_retry_plan(
            root, post_id, source_factory)
        if not retry_reasons:
            strategy, strategy_reasons = "retry_prepared_source_restart", [
                "已重建的当前 generation 在生成摘要前发生瞬时网络错误；"
                "中文源、空摘要和英文基线仍全部匹配"]
    elif (current and current.get("recovery_kind") in {
            "excerpt_generation_retry",
            "legacy_prepared_excerpt_generation_retry"} and not reasons):
        strategy, strategy_reasons = "retry_excerpt_generation", [
            "GLM 摘要请求在写入中文摘要前失败；中文源、空摘要和英文基线仍全部匹配"]
    elif execution_status in {
            "chinese_excerpt_saved", "translation_started",
            "translation_failed"} and not source_changed:
        ignored = {"chinese_source_unchanged"}
        remaining = [reason for reason in reasons if reason not in ignored]
        if not remaining and state["workflow_status"] in {
                "blocked", "ready_for_translation_resume", "translation_failed"}:
            strategy, strategy_reasons = "resume", [
                "execution evidence is translation-failed and all recovery checks pass"]
    elif source_changed:
        remaining = [reason for reason in reasons
                     if reason != "chinese_source_unchanged"]
        if not remaining:
            strategy, strategy_reasons = "restart_from_current", [
                "production Chinese title or content changed after pre-write"]
    display_failure = failure
    if strategy == "retry_excerpt_generation":
        preserved = (state.get("recovery") or {}).get("preserved_failure")
        if isinstance(preserved, dict):
            display_failure = preserved
    display_failure_text = "\n".join(str(display_failure.get(key, "")) for key in (
        "reason", "stderr_summary", "stdout_summary", "error_summary"))
    display_failure_category = _classify_subprocess_failure(
        type("Failure", (), {
            "returncode": display_failure.get("returncode", -1),
            "stderr": display_failure_text, "stdout": ""})(), "preflight")["category"]
    evidence_path = _relative(_execution_path(root, post_id), root)
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": "execute" if execute else "preview",
        "repository_root": str(root), "batch_id": state["batch_id"],
        "post_id": int(post_id), "english_post_id": article["english_post_id"],
        "current_status": state["workflow_status"],
        "actual_error": ("rejected_excerpt_generation"
                         if strategy == "rejected_excerpt_regeneration" else
                         (_structured_execution_failure(
                             _execution_details(root, article)) or {}).get(
                                 "category", display_failure_category)),
        "execution_evidence": evidence_path,
        "production_source": (
            "unknown" if unavailable else "changed" if source_changed else "unchanged"),
        "strategy": strategy, "strategy_reasons": strategy_reasons,
        "will_execute": (
            "重新生成中文摘要，并继续安全执行"
            if strategy in {"rejected_excerpt_regeneration", "retry_excerpt_generation"} else
            f"{strategy} -> execute_single_candidate"
            if strategy != "blocked" else "blocked"),
        "baseline_rebuild": "not_run", "reexecution": "not_run",
        "final_status": state["workflow_status"],
        "writes_performed": False, "integrity_ok": True,
    }
    if not execute or strategy == "blocked":
        return result
    if strategy in {"restart_from_current", "rejected_excerpt_regeneration"}:
        restarted = restart_from_current(
            root, post_id, apply=True, source_factory=source_factory)
        if not restarted["eligible"] or not restarted["changed"]:
            return {**result, "strategy": "blocked",
                    "strategy_reasons": restarted["blocking_reasons"]}
        result["baseline_rebuild"] = "completed"
        operation = _run_ready_once(
            root, execute=True, batch_id=state["batch_id"], post_id=post_id,
            runner=runner)
    elif strategy == "retry_prepared_source_restart":
        restored = _restore_prepared_source_restart_for_retry(
            root, post_id, source_factory)
        if not restored["eligible"] or not restored["changed"]:
            return {**result, "strategy": "blocked",
                    "strategy_reasons": restored["blocking_reasons"]}
        operation = _run_ready_once(
            root, execute=True, batch_id=state["batch_id"], post_id=post_id,
            runner=runner)
    elif strategy == "retry_excerpt_generation":
        restored = _restore_excerpt_generation_for_retry(
            root, post_id, source_factory)
        if not restored["eligible"] or not restored["changed"]:
            return {**result, "strategy": "blocked",
                    "strategy_reasons": restored["blocking_reasons"]}
        operation = _run_ready_once(
            root, execute=True, batch_id=state["batch_id"], post_id=post_id,
            runner=runner, recovery_retry=True)
    else:
        operation = resume(
            root, execute=True, batch_id=state["batch_id"], post_id=post_id,
            runner=runner, allow_blocked=True)
    item = operation["results"][0] if operation.get("results") else None
    _, _, _, _, fresh_states = _context(root)
    final_status = fresh_states[int(post_id)]["workflow_status"]
    return {**result, "reexecution": (
                "completed" if item and item["result"] == "completed" else "failed"),
            "final_status": final_status, "operation_result": item,
            "next_step": _recover_next_step(final_status, post_id),
            "writes_performed": True}


def _recover_next_step(final_status, post_id):
    if final_status == "completed":
        return "无"
    if final_status == "excerpt_failed":
        return (
            "检查 rejected excerpt evidence 或摘要请求失败 evidence；确认问题后重新运行 "
            f"recover --post-id {int(post_id)}")
    if final_status == "ready_for_execution":
        return "生产连接恢复后重新运行同一条 recover --execute 命令"
    return f"根据当前状态 {final_status} 重新运行 recover --post-id {int(post_id)}"


SUMMARY_KEYS = (
    "awaiting_manual_conversion", "awaiting_readonly_validation",
    "validation_failed", "ready_for_execution", "execution_in_progress",
    "ready_for_translation_resume", "excerpt_failed", "translation_failed",
    "execution_failed", "completed", "blocked",
)


def _summary_bucket(status):
    if status in SUMMARY_KEYS:
        return status
    return "blocked"


def _next_action(counts):
    if counts["blocked"]:
        return "需要人工排查 blocked"
    if counts["ready_for_translation_resume"] or counts["translation_failed"]:
        return "可以 resume"
    if counts["ready_for_execution"]:
        return "可以执行 ready 文章"
    if counts["awaiting_readonly_validation"]:
        return "执行生产只读验收"
    if counts["awaiting_manual_conversion"]:
        return "继续人工转换"
    if counts["completed"] == counts["total"]:
        return "当前批次已完成"
    return "检查失败文章"


def summary(root):
    root, batches, _, _, states = _context(root)
    results = []
    for batch in batches:
        counts = Counter({key: 0 for key in SUMMARY_KEYS})
        for article in batch["articles"]:
            state = states.get(article["chinese_post_id"])
            status = _summary_bucket(
                state["workflow_status"] if state else "blocked")
            counts[status] += 1
            if status in {"excerpt_failed", "translation_failed"}:
                counts["execution_failed"] += 1
        total = len(batch["articles"])
        pending = total - counts["completed"]
        exhausted = sum(
            any(int(value) >= (
                MAX_RESUME_ATTEMPTS if key == "resume" else MAX_RUN_ATTEMPTS)
                for key, value in (states.get(article["chinese_post_id"], {})
                                   .get("retry_counts") or {}).items())
            for article in batch["articles"]
        )
        action_counts = {**dict(counts), "total": total}
        results.append({
            "batch_id": batch["batch_id"], "source_file": batch["source_file"],
            "total": total, **dict(counts), "pending": pending,
            "remaining": pending, "retry_exhausted": exhausted,
            "next_action": _next_action(action_counts), "terminal": pending == 0,
        })
    latest = _latest_coordination_batch(batches, states)
    can_create = latest is None
    return {
        "schema_version": SCHEMA_VERSION, "repository_root": str(root),
        "batches": results,
        "totals": {
            key: sum(item[key] for item in results)
            for key in ("total",) + SUMMARY_KEYS
            + ("pending", "remaining", "retry_exhausted")
        },
        "latest_incomplete_batch": latest["batch_id"] if latest else None,
        "can_create_next_batch": can_create,
        "recommendation": (
            "all batches complete" if can_create
            else "continue latest incomplete batch; do not create a new batch"),
        "writes_performed": False, "integrity_ok": True,
    }


def render_text(result):
    counts = result["execution_counts"]
    lines = [
        f"仓库: {result['repository_root']}",
        f"固定批次: {len(result['batches'])}",
        f"固定文章: {result['fixed_article_count']}",
        (
            "执行证据: "
            f"completed={counts['completed']} failed={counts['failed']} "
            f"pending={counts['pending']} "
            f"translation_started={counts['translation_started']} "
            f"other={counts['other']} "
            f"no_execution_evidence={counts['no_execution_evidence']}"
        ),
        (
            "协调状态: "
            f"count={result['coordination_state_count']} "
            f"legacy_import={result['legacy_import_count']} "
            f"awaiting_manual_conversion="
            f"{result['awaiting_manual_conversion_count']} "
            f"uninitialized={result['uninitialized_count']} "
            f"integrity={'ok' if result['state_integrity'] else 'error'}"
        ),
        (
            f"完整性: {'ok' if result['integrity_ok'] else 'error'} "
            f"conflicts={len(result['conflicts'])} errors={len(result['errors'])}"
        ),
        (
            f"流程: ready={result['ready_for_execution_count']} "
            f"in_progress={result['execution_in_progress_count']} "
            f"translation_resume={result['ready_for_translation_resume_count']} "
            f"excerpt_failed={result['excerpt_failed_count']} "
            f"translation_failed={result['translation_failed_count']} "
            f"validation_failed={result['validation_failed_count']} "
            f"blocked={result['blocked_count']} "
            f"remaining={result['remaining_count']} "
            f"retry_exhausted={result['retry_exhausted_count']}"
        ),
        f"下一步: {result['next_action']}",
        "批次:",
    ]
    for batch in result["batches"]:
        lines.append(
            f"- {batch['batch_id']} | {batch['source_file']} | "
            f"fixed={batch['fixed_article_count']} "
            f"completed={batch['completed_count']} "
            f"incomplete={batch['incomplete_count']} "
            f"validation_evidence={batch['validation_evidence_count']} "
            f"coordination={batch['coordination_state_count']} "
            f"uninitialized={batch['uninitialized_count']} "
            f"integrity={'ok' if batch['integrity_ok'] else 'error'}"
        )
    latest = result["latest_incomplete_batch"]
    if latest is None:
        lines.append("最新未完成批次: 无")
    elif latest["status"] == "determined":
        lines.append(f"最新未完成批次: {latest['batch_id']}")
    else:
        lines.append(f"最新未完成批次: 无法确定（{latest['reason']}）")
    if result["conflicts"]:
        lines.append("冲突:")
        lines.extend(
            "- " + json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in result["conflicts"]
        )
    if result["errors"]:
        lines.append("错误:")
        lines.extend("- " + message for message in result["errors"])
    return "\n".join(lines)


def render_init_text(result):
    lines = [
        f"模式: {result['mode']}",
        f"仓库: {result['repository_root']}",
        f"固定批次: {result['fixed_batch_count']}",
        f"固定文章: {result['fixed_article_count']}",
        (
            f"计划: planned={result['planned_count']} "
            f"would_create={result['would_create_count']} "
            f"created={result['created_count']} "
            f"unchanged={result['unchanged_count']}"
        ),
        (
            f"映射: legacy_import={result['legacy_import_count']} "
            f"awaiting_manual_conversion="
            f"{result['awaiting_manual_conversion_count']}"
        ),
        (
            f"完整性: {'ok' if result['integrity_ok'] else 'error'} "
            f"conflicts={len(result['conflicts'])} "
            f"errors={len(result['errors'])}"
        ),
        f"发生写入: {'yes' if result['writes_performed'] else 'no'}",
        "批次:",
    ]
    for batch in result["batches"]:
        lines.append(
            f"- {batch['batch_id']} | planned={batch['planned_count']} "
            f"would_create={batch['would_create_count']} "
            f"created={batch['created_count']} "
            f"unchanged={batch['unchanged_count']} "
            f"legacy_import={batch['legacy_import_count']} "
            f"awaiting_manual_conversion="
            f"{batch['awaiting_manual_conversion_count']}"
        )
    if result["conflicts"]:
        lines.append("冲突:")
        lines.extend(
            "- " + json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in result["conflicts"]
        )
    if result["errors"]:
        lines.append("错误:")
        lines.extend("- " + message for message in result["errors"])
    return "\n".join(lines)


def render_current_text(result):
    if result["all_completed"]:
        return "全部固定批次均已完成"
    lines = [
        f"最新未完成批次: {result['batch_id']}",
        f"文章数: {len(result['articles'])}",
    ]
    for item in result["articles"]:
        lines.append(
            f"{item['position']:02d}. zh={item['chinese_post_id']} "
            f"en={item['english_post_id']} | {item['published_at']} | "
            f"{item['workflow_status']} | SH={item['syntax_count_before']} | "
            f"converted={item['manual_conversion_confirmed']} "
            f"languages={item['language_review_confirmed']} "
            f"validation={item['validation_status']} "
            f"execution={item['execution_status']} | {item['title']}"
        )
    return "\n".join(lines)


def render_summary_text(result):
    lines = [
        "批次汇总:",
    ]
    for item in result["batches"]:
        lines.append(
            f"- {item['batch_id']}: total={item['total']} "
            f"manual={item['awaiting_manual_conversion']} "
            f"validation={item['awaiting_readonly_validation']} "
            f"validation_failed={item['validation_failed']} "
            f"ready={item['ready_for_execution']} "
            f"in_progress={item['execution_in_progress']} "
            f"translation_resume={item['ready_for_translation_resume']} "
            f"excerpt_failed={item['excerpt_failed']} "
            f"translation_failed={item['translation_failed']} "
            f"execution_failed={item['execution_failed']} "
            f"completed={item['completed']} blocked={item['blocked']} "
            f"remaining={item['remaining']} "
            f"retry_exhausted={item['retry_exhausted']} "
            f"next_action={item['next_action']}"
        )
    lines.extend([
        f"最新未完成批次: {result['latest_incomplete_batch'] or '无'}",
        f"建议创建下一批: {result['can_create_next_batch']}",
        f"建议: {result['recommendation']}",
    ])
    return "\n".join(lines)


def render_plan_text(result):
    lines = [
        f"执行计划: planned={result['planned_count']} "
        f"allowed={result['allowed_count']}",
    ]
    for item in result["items"]:
        lines.append(
            f"- {item['batch_id']} zh={item['post_id']} "
            f"en={item['english_post_id']} allowed={item['allowed']} "
            f"blocked={';'.join(item['blocking_reasons']) or '-'}"
        )
    lines.append("生产调用: 0")
    return "\n".join(lines)


def render_operation_text(result):
    lines = [
        f"模式: {result.get('mode', 'operation')}",
        f"完整性: {'ok' if result['integrity_ok'] else 'error'}",
    ]
    for field in (
            "workflow_status", "selected_count", "allowed_count",
            "planned_count", "changed_count", "processed_count",
            "pending_count", "stopped_early"):
        if field in result:
            lines.append(f"{field}: {result[field]}")
    if "items" in result:
        for item in result["items"]:
            lines.append(
                f"- {item.get('batch_id')} zh={item.get('post_id')} "
                f"allowed={item.get('allowed', True)} "
                f"blocked={';'.join(item.get('blocking_reasons', [])) or '-'}")
    if "results" in result:
        for item in result["results"]:
            lines.append(
                f"- zh={item['post_id']} result={item['result']} "
                f"category={item.get('category', '-')} "
                f"returncode={item.get('returncode', '-')} "
                f"error={item.get('error', '-')}")
            if item.get("stderr_summary"):
                lines.append(f"  stderr: {item['stderr_summary']}")
            if item.get("stdout_summary"):
                lines.append(f"  stdout: {item['stdout_summary']}")
            if item.get("execution_evidence"):
                lines.append(
                    f"  execution evidence: {item['execution_evidence']}")
            if item.get("next_step"):
                lines.append(f"  下一步: {item['next_step']}")
    if "eligible" in result:
        suffix = (
            f" retry_count_run={result['retry_count_run']}"
            if "retry_count_run" in result else "")
        lines.append(
            f"eligible={result['eligible']} changed={result['changed']}{suffix}")
        if result["blocking_reasons"]:
            lines.append("blocked: " + ";".join(result["blocking_reasons"]))
    return "\n".join(lines)


def render_manual_completion_text(result):
    lines = [
        f"模式: {result['mode']}",
        f"文章: zh={result['chinese_post_id']} en={result['english_post_id']}",
        f"当前状态: {result['previous_status']}",
        "只读检查: post ID、publish、Polylang、英文标题和正文",
        f"允许人工完成: {'是' if result['eligible'] else '否'}",
        f"写入操作: {'是' if result['writes_performed'] else '否'}",
    ]
    if result["blocking_reasons"]:
        lines.append("阻断原因: " + "; ".join(result["blocking_reasons"]))
    return "\n".join(lines)


def render_recover_text(result):
    source_labels = {
        "changed": "已修改", "unchanged": "未修改", "unknown": "未知",
        "not_required": "无需检查"}
    error_labels = {"none": "无"}
    strategy_labels = {
        "restart_from_current": "restart_from_current",
        "rejected_excerpt_regeneration": "rejected_excerpt_regeneration",
        "retry_excerpt_generation": "retry_excerpt_generation",
        "retry_prepared_source_restart": "retry_prepared_source_restart",
        "resume": "resume", "retry_from_ready": "retry_from_ready",
        "blocked": "blocked", "none": "none"}
    lines = [
        f"模式: {result['mode']}",
        f"文章: zh={result['post_id']} en={result['english_post_id']}",
        f"当前状态: {result['current_status']}",
        f"真实错误: {error_labels.get(result['actual_error'], result['actual_error'])}",
        f"execution evidence: {result['execution_evidence']}",
        f"生产中文源: {source_labels[result['production_source']]}",
        f"恢复策略: {strategy_labels[result['strategy']]}",
    ]
    if result["strategy"] == "none":
        lines.extend([
            "将执行: 无", "写入操作: 否",
            "原因: " + "; ".join(result["strategy_reasons"]),
            "下一步: 无",
        ])
        return "\n".join(lines)
    if result["mode"] == "preview":
        lines.extend([
            f"将执行: {result['will_execute']}",
            "写入操作: 否",
            "原因: " + "; ".join(result["strategy_reasons"]),
        ])
    else:
        lines.extend([
            f"基线重建: {result['baseline_rebuild']}",
            f"重新执行: {result['reexecution']}",
            f"最终状态: {result['final_status']}",
            "下一步: " + result.get(
                "next_step", "无" if result["final_status"] == "completed"
                else "; ".join(result["strategy_reasons"])),
        ])
        operation_result = result.get("operation_result") or {}
        if result["reexecution"] != "completed":
            failure_summary = (
                operation_result.get("stderr_summary")
                or operation_result.get("stdout_summary")
                or operation_result.get("error")
            )
            if failure_summary:
                lines.append(f"子进程失败摘要: {failure_summary}")
    if result["strategy"] == "blocked" and result["mode"] != "preview":
        lines.append("原因: " + "; ".join(result["strategy_reasons"]))
    return "\n".join(lines)


def render_run_progress(kind, index, total, item, result):
    prefix = f"[{index}/{total}]"
    if kind == "start":
        return (
            f"{prefix} 开始处理：zh={item['post_id']} "
            f"en={item['english_post_id']}"
        )
    if kind == "attempt":
        return (
            f"{prefix} 第 {result['attempts']}/3 次尝试："
            f"zh={item['post_id']} mode={result['mode']}"
        )
    if kind == "attempt_failed":
        return (
            f"{prefix} 第 {result['attempts']}/3 次失败："
            f"zh={item['post_id']} result={result['result']} "
            f"error={_safe_subprocess_summary(result['error'])}"
        )
    if kind == "retry_wait":
        return (
            f"{prefix} {result['delay']} 秒后进行第 "
            f"{result['attempts']}/3 次尝试"
        )
    if kind == "continue":
        return f"{prefix} 继续处理下一篇"
    if kind == "finish":
        return (
            f"{prefix} 处理完成：zh={item['post_id']} "
            f"candidate_attempts={result.get('attempts', 1)} result=completed"
        )
    attempt_summary = f"candidate_attempts={result.get('candidate_attempts', result['attempts'])}"
    if result.get("excerpt_generation_attempts") is not None:
        attempt_summary += (
            f" excerpt_generation_attempts={result['excerpt_generation_attempts']}")
    return (
        f"{prefix} 最终失败：zh={item['post_id']} "
        f"{attempt_summary} result={result['result']} "
        f"error={_safe_subprocess_summary(result['error'])}"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only historical article migration status")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show read-only batch status")
    status.add_argument("--json", action="store_true", dest="json_output")
    status.add_argument("--repo-root", type=Path, default=repository_root(),
                        help=argparse.SUPPRESS)
    init = subparsers.add_parser(
        "init-state", help="preview or initialize coordination state")
    init.add_argument("--apply", action="store_true")
    init.add_argument("--json", action="store_true", dest="json_output")
    init.add_argument("--repo-root", type=Path, default=repository_root(),
                      help=argparse.SUPPRESS)
    current = subparsers.add_parser(
        "show-current", help="show the latest incomplete fixed batch")
    current.add_argument("--json", action="store_true", dest="json_output")
    current.add_argument("--repo-root", type=Path, default=repository_root(),
                         help=argparse.SUPPRESS)
    converted = subparsers.add_parser(
        "mark-converted", help="confirm manual conversion and language review")
    converted.add_argument("--post-id", required=True, type=int)
    converted.add_argument("--syntax-count-before", required=True, type=int)
    converted.add_argument("--cbp-count-after", required=True, type=int)
    converted.add_argument("--language-review-confirmed", required=True,
                           action="store_true")
    converted.add_argument("--gutenberg-normalization-confirmed",
                           action="store_true")
    converted.add_argument("--repo-root", type=Path, default=repository_root(),
                           help=argparse.SUPPRESS)
    validation = subparsers.add_parser(
        "record-validation", help="record an existing read-only validation file")
    validation.add_argument("--post-id", required=True, type=int)
    validation.add_argument("--validation-file", required=True)
    validation.add_argument("--repo-root", type=Path, default=repository_root(),
                            help=argparse.SUPPRESS)
    summary_parser = subparsers.add_parser(
        "summary", help="derive per-batch workflow totals")
    summary_parser.add_argument("--json", action="store_true", dest="json_output")
    summary_parser.add_argument(
        "--repo-root", type=Path, default=repository_root(), help=argparse.SUPPRESS)
    plan = subparsers.add_parser(
        "plan-run", help="show safe future execution candidates without executing")
    plan.add_argument("--json", action="store_true", dest="json_output")
    plan.add_argument("--repo-root", type=Path, default=repository_root(),
                      help=argparse.SUPPRESS)
    live = subparsers.add_parser(
        "validate-live", help="run production read-only validation for one article")
    live.add_argument("--post-id", required=True, type=int)
    live.add_argument(
        "--refresh", action="store_true",
        help="fetch production data again for awaiting or failed validation")
    live.add_argument("--json", action="store_true", dest="json_output")
    live.add_argument("--repo-root", type=Path, default=repository_root(),
                      help=argparse.SUPPRESS)
    run = subparsers.add_parser(
        "run-ready", help="preview or execute ready articles in fixed order")
    run.add_argument("--execute", action="store_true")
    run.add_argument("--batch-id")
    run.add_argument("--post-id", type=int)
    run.add_argument("--json", action="store_true", dest="json_output")
    run.add_argument("--repo-root", type=Path, default=repository_root(),
                     help=argparse.SUPPRESS)
    resume_parser = subparsers.add_parser(
        "resume", help="preview or resume recoverable execution states")
    resume_parser.add_argument("--execute", action="store_true")
    resume_parser.add_argument("--batch-id")
    resume_parser.add_argument("--post-id", type=int)
    resume_parser.add_argument("--json", action="store_true", dest="json_output")
    resume_parser.add_argument(
        "--repo-root", type=Path, default=repository_root(),
        help=argparse.SUPPRESS)
    recover_parser = subparsers.add_parser(
        "recover", help="preview or execute evidence-based single-article recovery")
    recover_parser.add_argument("--post-id", required=True, type=int)
    recover_parser.add_argument("--execute", action="store_true")
    recover_parser.add_argument("--json", action="store_true", dest="json_output")
    recover_parser.add_argument(
        "--repo-root", type=Path, default=repository_root(),
        help=argparse.SUPPRESS)
    manual_completed = subparsers.add_parser(
        "mark-manual-completed",
        help="preview or confirm an externally completed manual translation")
    manual_completed.add_argument("--post-id", required=True, type=int)
    manual_completed.add_argument("--confirmed", action="store_true")
    manual_completed.add_argument("--json", action="store_true", dest="json_output")
    manual_completed.add_argument(
        "--repo-root", type=Path, default=repository_root(),
        help=argparse.SUPPRESS)
    sync = subparsers.add_parser(
        "sync-execution", help="preview or apply existing execution evidence")
    sync.add_argument("--apply", action="store_true")
    sync.add_argument("--json", action="store_true", dest="json_output")
    sync.add_argument("--repo-root", type=Path, default=repository_root(),
                      help=argparse.SUPPRESS)
    recovery = subparsers.add_parser(
        "recover-blocked",
        help="preview or recover a blocked run with no write evidence")
    recovery.add_argument("--post-id", required=True, type=int)
    recovery.add_argument("--apply", action="store_true")
    recovery.add_argument("--json", action="store_true", dest="json_output")
    recovery.add_argument(
        "--repo-root", type=Path, default=repository_root(),
        help=argparse.SUPPRESS)
    restart = subparsers.add_parser(
        "restart-from-current",
        help="preview or safely rebuild a failed single-article execution")
    restart.add_argument("--post-id", required=True, type=int)
    restart.add_argument("--apply", action="store_true")
    restart.add_argument("--reason")
    restart.add_argument("--json", action="store_true", dest="json_output")
    restart.add_argument(
        "--repo-root", type=Path, default=repository_root(),
        help=argparse.SUPPRESS)
    reconcile = subparsers.add_parser(
        "reconcile-attempts",
        help="preview or reconcile orphaned run/resume attempt counters")
    reconcile.add_argument("--post-id", required=True, type=int)
    reconcile.add_argument(
        "--stage", choices=("run", "resume"), default="resume")
    excerpt_state = reconcile.add_mutually_exclusive_group()
    excerpt_state.add_argument(
        "--chinese-excerpt-empty", action="store_const",
        const=True, default=None, dest="chinese_excerpt_empty")
    excerpt_state.add_argument(
        "--chinese-excerpt-saved", action="store_const",
        const=False, dest="chinese_excerpt_empty")
    reconcile.add_argument("--apply", action="store_true")
    reconcile.add_argument("--json", action="store_true", dest="json_output")
    reconcile.add_argument(
        "--repo-root", type=Path, default=repository_root(),
        help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.command == "status":
            result = build_status(args.repo_root)
            output = render_text(result)
        elif args.command == "init-state":
            result = init_state(args.repo_root, apply=args.apply)
            output = render_init_text(result)
        elif args.command == "show-current":
            result = show_current(args.repo_root)
            output = render_current_text(result)
        elif args.command == "mark-converted":
            result = mark_converted(
                args.repo_root, args.post_id, args.syntax_count_before,
                args.cbp_count_after, args.language_review_confirmed,
                args.gutenberg_normalization_confirmed)
            output = json.dumps(result, ensure_ascii=False, sort_keys=True)
        elif args.command == "record-validation":
            result = record_validation(
                args.repo_root, args.post_id, args.validation_file)
            output = json.dumps(result, ensure_ascii=False, sort_keys=True)
        elif args.command == "summary":
            result = summary(args.repo_root)
            output = render_summary_text(result)
        elif args.command == "plan-run":
            result = plan_run(args.repo_root)
            output = render_plan_text(result)
        elif args.command == "validate-live":
            result = validate_live(
                args.repo_root, args.post_id, refresh=args.refresh)
            output = render_operation_text(result)
        elif args.command == "run-ready":
            progress = None
            if args.execute and not args.json_output:
                progress = lambda kind, index, total, item, value: print(
                    render_run_progress(
                        kind, index, total, item, value),
                    flush=True)
            result = run_ready(
                args.repo_root, execute=args.execute, batch_id=args.batch_id,
                post_id=args.post_id,
                progress=progress)
            output = render_operation_text(result)
        elif args.command == "resume":
            result = resume(
                args.repo_root, execute=args.execute, batch_id=args.batch_id,
                post_id=args.post_id)
            output = render_operation_text(result)
        elif args.command == "recover":
            result = recover(
                args.repo_root, args.post_id, execute=args.execute)
            output = render_recover_text(result)
        elif args.command == "mark-manual-completed":
            result = mark_manual_completed(
                args.repo_root, args.post_id, confirmed=args.confirmed)
            output = render_manual_completion_text(result)
        elif args.command == "sync-execution":
            result = sync_execution(args.repo_root, apply=args.apply)
            output = render_operation_text(result)
        elif args.command == "recover-blocked":
            result = recover_blocked(
                args.repo_root, args.post_id, apply=args.apply)
            output = render_operation_text(result)
        elif args.command == "restart-from-current":
            result = restart_from_current(
                args.repo_root, args.post_id, apply=args.apply,
                reason=args.reason)
            output = render_operation_text(result)
        else:
            result = reconcile_attempts(
                args.repo_root, args.post_id, apply=args.apply,
                stage=args.stage,
                chinese_excerpt_empty=args.chinese_excerpt_empty)
            output = render_operation_text(result)
    except ReadError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_INTEGRITY_ERROR
    if getattr(args, "json_output", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(output)
    return EXIT_OK if result["integrity_ok"] else result.get(
        "exit_code", EXIT_INTEGRITY_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
