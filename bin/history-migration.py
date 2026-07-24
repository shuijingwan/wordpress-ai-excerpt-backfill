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
DEFAULT_SYNTAX_BATCH_EXPECTED_COUNT = 20
SYNTAX_BATCH_EXPECTED_COUNTS = {
    "syntaxhighlighter-20260722-01": 20,
    "syntaxhighlighter-20260723-01": 20,
    "syntaxhighlighter-priority-20260724-01": 5,
}
SYNTAX_FIXED_FIELDS = {
    "schema_version", "batch_id", "batch_sequence", "allocated_at",
    "chinese_post_id", "english_post_id", "chinese_title", "published_at",
    "before_content_sha256", "before_syntaxhighlighter_count",
    "before_code_block_pro_count", "migration_status", "validation_status",
}
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
    "prepared", "excerpt_rejected", "excerpt_generated", "chinese_excerpt_saved",
    "translation_started", "translation_failed", "completed", "failed", "pending",
}
DERIVED_SUFFIXES = ("-validation.csv", "-execution-candidates.csv")
EXECUTION_NAME = re.compile(r"^chinese-(?P<post_id>[1-9][0-9]*)\.execution\.json$")


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


def _load_syntax_batch(path, root):
    rows, fields = _read_csv(path)
    _required(fields, SYNTAX_FIXED_FIELDS, path)
    if not rows:
        raise ReadError(f"{path}: fixed batch is empty")
    batch_ids = {row.get("batch_id", "").strip() for row in rows}
    sequences = {row.get("batch_sequence", "").strip() for row in rows}
    allocated = {row.get("allocated_at", "").strip() for row in rows}
    if len(batch_ids) != 1 or not next(iter(batch_ids)):
        raise ReadError(f"{path}: batch_id must be non-empty and identical in every row")
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
    return {
        "batch_id": batch_id,
        "source_file": _relative(path, root),
        "source_type": "syntaxhighlighter_daily",
        "expected_count": SYNTAX_BATCH_EXPECTED_COUNTS.get(
            batch_id, DEFAULT_SYNTAX_BATCH_EXPECTED_COUNT),
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
    for path in sorted(analysis.glob(SYNTAX_GLOB), key=lambda item: item.name):
        if path.name.endswith(DERIVED_SUFFIXES):
            continue
        try:
            batches.append(_load_syntax_batch(path, root))
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
    prefix = "syntaxhighlighter-migration-batch-"
    if not stem.startswith(prefix):
        return None
    return "syntaxhighlighter-" + stem[len(prefix):]


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
    if batch["source_type"] == "syntaxhighlighter_daily" and validation is None:
        return "awaiting_manual_conversion", False, (
            "fixed SyntaxHighlighter article has no validation or execution evidence")
    raise ReadError(
        f"cannot safely initialize Chinese post without execution evidence: "
        f"{batch['batch_id']}")


def _manual_evidence(batch, legacy_import):
    if not legacy_import:
        return {
            "manual_conversion": {"status": "not_recorded"},
            "language_review": {"status": "not_recorded"},
        }
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
                   language_review_confirmed):
    if not language_review_confirmed:
        raise ReadError("--language-review-confirmed is required")
    if syntax_count_before < 1 or cbp_count_after < 1:
        raise ReadError("code block counts must be positive")
    with InitLock(Path(root).resolve()):
        root, batches, fixed, _, states = _context(root)
        article = fixed.get(int(post_id))
        if article is None:
            raise ReadError(f"Chinese post {post_id} is outside fixed batches")
        batch = next(item for item in batches if item["batch_id"] == article["batch_id"])
        if batch["source_type"] != "syntaxhighlighter_daily":
            raise ReadError("mark-converted only accepts SyntaxHighlighter daily batches")
        state = states.get(int(post_id))
        if state is None:
            raise ReadError(f"coordination state is missing for Chinese post {post_id}")
        expected = int(article["source_row"]["before_syntaxhighlighter_count"])
        if syntax_count_before != expected:
            raise ReadError(
                f"syntax-count-before mismatch: expected {expected}, got "
                f"{syntax_count_before}")
        if cbp_count_after != syntax_count_before:
            raise ReadError("cbp-count-after must equal syntax-count-before")
        if state["workflow_status"] == "awaiting_readonly_validation":
            same = (
                state.get("manual_conversion", {}).get("status") == "confirmed"
                and state["manual_conversion"].get("syntax_count_before")
                == syntax_count_before
                and state["manual_conversion"].get("cbp_count_after")
                == cbp_count_after
                and state.get("language_review", {}).get("status") == "confirmed"
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
        state["updated_at"] = timestamp
        evidence = {
            "syntax_count_before": syntax_count_before,
            "cbp_count_after": cbp_count_after,
            "language_review_confirmed": True,
        }
        event = _transition_event(
            "manual_conversion_and_language_review_confirmed", state,
            previous, state["workflow_status"],
            "manual conversion and per-block language review explicitly confirmed",
            evidence, timestamp,
            f"{syntax_count_before}|{cbp_count_after}|language-confirmed",
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
    expected_cbp = state["manual_conversion"]["cbp_count_after"]
    if _validation_int(
            row, "expected_code_block_pro_count_after", path) != expected_cbp:
        raise ReadError(f"{path}: validation expected Code Block Pro count mismatch")
    validation_reasons = _validation_text(
        row, "validation_reasons", path)
    checks = {
        "syntaxhighlighter_zero":
            _validation_int(row, "after_syntaxhighlighter_count", path) == 0,
        "code_block_pro_count":
            _validation_int(row, "after_code_block_pro_count", path)
            == expected_cbp,
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
            _validation_int(row, "after_code_block_pro_count", path)
            == state["manual_conversion"]["cbp_count_after"],
    }
    status = _validation_text(row, "validation_status", path)
    if status not in {"ready", "pending", "abnormal"}:
        raise ReadError(f"{path}: unknown validation_status {status!r}")
    failure_reasons = [name for name, value in checks.items() if not value]
    if status != "ready":
        failure_reasons.extend(
            item for item in validation_reasons.split("|") if item)
    passed = status == "ready" and not failure_reasons
    return row, passed, sorted(set(failure_reasons)), checks


def _record_validation_locked(root, post_id, validation_file):
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
    if state["workflow_status"] != "awaiting_readonly_validation":
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


def _execution_manifest_row(article, validation_row, source):
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
        "source_migration_type": "syntaxhighlighter-to-code-block-pro",
        "expected_code_block_pro_count":
            validation_row["expected_code_block_pro_count_after"],
        "expected_syntaxhighlighter_count": 0,
    }


def validate_live(root, post_id, source_factory=None):
    """Reuse the batch read-only source and validator for exactly one fixed row."""
    root = Path(root).resolve()
    with InitLock(root):
        root, batches, fixed, _, states = _context(root)
        article = fixed.get(int(post_id))
        if article is None:
            raise ReadError(f"Chinese post {post_id} is outside fixed batches")
        batch = next(
            item for item in batches if item["batch_id"] == article["batch_id"])
        if batch["source_type"] != "syntaxhighlighter_daily":
            raise ReadError("validate-live only accepts SyntaxHighlighter daily batches")
        state = states.get(int(post_id))
        if state is None:
            raise ReadError(f"coordination state is missing for Chinese post {post_id}")
        csv_path, snapshot_path, manifest_path = _validation_paths(
            root, batch["batch_id"], post_id)
        relative_csv = _relative(csv_path, root)
        if state["workflow_status"] in {
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
        if state["workflow_status"] != "awaiting_readonly_validation":
            raise ReadError(
                f"cannot validate live from {state['workflow_status']}")
        if csv_path.exists():
            result = _record_validation_locked(root, post_id, relative_csv)
            return {
                **result, "mode": "import-existing",
                "validation_file": relative_csv,
                "wordpress_writes": 0, "glm_calls": 0,
                "translation_calls": 0,
            }
        if snapshot_path.exists() or manifest_path.exists():
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
            write_outputs(rows, csv_path, snapshot_path)
            # The shared writer intentionally owns validation semantics; rewrite
            # only its CSV line endings atomically for stable Git text evidence.
            _atomic_write_csv(csv_path, LIVE_VALIDATION_FIELDS, rows)
            if rows[0]["validation_status"] == "ready":
                _atomic_write_csv(
                    manifest_path, EXECUTION_MANIFEST_FIELDS,
                    [_execution_manifest_row(article, rows[0], source)])
        except (SafetyError, OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReadError(f"validate-live operation failed: {error}") from error
        result = _record_validation_locked(root, post_id, relative_csv)
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
            if execution is not None:
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
                "allowed": execution is None,
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
    return {
        "chinese_post_id": chinese_id, "english_post_id": english_id,
        "status": status, "source_file": _relative(path, root),
        "sha256": _file_sha256(path),
        "mtime_ns": path.stat().st_mtime_ns,
    }


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
        {"stage": stage, "attempt": attempt}, timestamp,
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
    transient_markers = (
        "network request failed", "urlerror", "ssleoferror",
        "unexpected_eof_while_reading", "unexpected eof",
        "timed out", "timeout", "temporary failure in name resolution",
        "name or service not known", "connection reset", "connection refused",
        "remote end closed connection",
    )
    authentication_markers = (
        "http error 401", "http error 403", "unauthorized", "forbidden",
        "authentication failed",
    )
    if any(marker in combined for marker in transient_markers):
        category = "transient_network_error"
    elif any(marker in combined for marker in authentication_markers):
        category = "authentication_error"
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
        {"stage": stage, "attempt": attempt, **failure},
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


def _executor_command(root, state, *, resume=False, preflight=False):
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


def _select_batch(batches, batch_id):
    if batch_id is None:
        return batches
    selected = [item for item in batches if item["batch_id"] == batch_id]
    if not selected:
        raise ReadError(f"unknown batch_id: {batch_id}")
    return selected


def _run_items(root, batch_id=None):
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
                and execution["status"] in {"prepared", "excerpt_generated"}
                and recovery.get("status") == "applied"
                and recovery.get("stage") == "run"
                and recovery.get("action") == "restart"
                and recovery.get("execution_sha256") == execution_sha256
            )
            if execution and not recovered_restart:
                reasons.append("execution evidence already exists")
            if _manifest_for(root, state) is None:
                reasons.append("single-candidate execution manifest is missing")
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
                    runner=subprocess.run, max_run_attempts=MAX_RUN_ATTEMPTS):
    root, items = _run_items(root, batch_id)
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
        root, _, fixed, _, states = _context(root)
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
                if int((state.get("retry_counts") or {}).get(
                        "run", 0)) >= max_run_attempts:
                    _block_retry_exhausted(root, state, "run")
                    raise ReadError("run retry limit exhausted")
                try:
                    preflight = runner(
                        _executor_command(root, state, preflight=True),
                        cwd=root, text=True, capture_output=True, check=False,
                        timeout=180)
                except (OSError, subprocess.SubprocessError) as error:
                    failure = _exception_failure(error, "preflight")
                    finish(_operation_result(
                        item, "operation_error", failure["category"],
                        failure["returncode"], _failure_error(failure),
                        phase="preflight", **_failure_details(failure)))
                    continue
                if preflight.returncode != 0:
                    failure = _classify_subprocess_failure(
                        preflight, "preflight")
                    finish(_operation_result(
                        item, "operation_error", failure["category"],
                        failure["returncode"], _failure_error(failure),
                        phase="preflight", **_failure_details(failure)))
                    continue
                before = _execution_details(root, fixed[item["post_id"]])
                attempt = _record_attempt_start(root, state, "run")
                completed = runner(
                    _executor_command(root, state, resume=False),
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
                            failure["category"] == "transient_network_error"
                            and not artifacts):
                        _restore_ready_after_transient(
                            root, state, attempt, failure)
                        finish(_operation_result(
                            item, "operation_error", failure["category"],
                            failure["returncode"], _failure_error(failure),
                            phase="execute", attempt=attempt,
                            recovered_to_ready=True,
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
            except (OSError, subprocess.SubprocessError, ReadError) as error:
                if attempt is None:
                    attempt = int((state.get("retry_counts") or {}).get("run", 0))
                failure = _exception_failure(error, "execute")
                if state and state["workflow_status"] == "execution_in_progress":
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


def _resume_items(root, batch_id=None, post_id=None):
    root, batches, _, _, states = _context(root)
    items = []
    for batch in _select_batch(batches, batch_id):
        for article in batch["articles"]:
            if post_id is not None and article["chinese_post_id"] != int(post_id):
                continue
            state = states.get(article["chinese_post_id"])
            if not state or state["workflow_status"] not in RESUME_STATUSES:
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
                "allowed": not reasons, "blocking_reasons": reasons,
            })
    return root, items


def resume(root, execute=False, batch_id=None, post_id=None,
           runner=subprocess.run):
    root, items = _resume_items(root, batch_id, post_id)
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
                if not item["allowed"] or state["workflow_status"] not in RESUME_STATUSES:
                    if "resume retry limit exhausted" in item["blocking_reasons"]:
                        _block_retry_exhausted(root, state, "resume")
                    raise ReadError("; ".join(item["blocking_reasons"])
                                    or "article is no longer resumable")
                before = _execution_details(root, fixed[item["post_id"]])
                attempt = _record_attempt_start(root, state, "resume")
                preflight = runner(
                    _executor_command(root, state, resume=True, preflight=True),
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
                    _executor_command(root, state, resume=True),
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


def _prepare_article_retry(root, post_id):
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
    if execution["status"] in {"prepared", "excerpt_generated"}:
        mode = "run"
        target = "ready_for_execution"
        action = "restart"
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


def run_ready(root, execute=False, batch_id=None, runner=subprocess.run,
              progress=None, sleeper=time.sleep,
              max_attempts=MAX_ARTICLE_ATTEMPTS,
              retry_delay=ARTICLE_RETRY_DELAY):
    root, items = _run_items(root, batch_id)
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
        mode = "run"
        _, _, _, _, current_states = _context(root)
        run_limit = (
            int((current_states[item["post_id"]].get("retry_counts") or {}).get(
                "run", 0)) + max_attempts)
        for article_attempt in range(1, max_attempts + 1):
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
            if progress:
                progress("attempt_failed", index, total, item, final)
            if article_attempt == max_attempts:
                break
            try:
                mode = _prepare_article_retry(root, item["post_id"])
            except ReadError:
                mode = None
            if mode is None:
                if not final.get("error"):
                    final["error"] = (
                        "current execution state cannot be retried safely")
                break
            if mode != "completed":
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
                progress("continue", index, total, item, final)
    completed_count = sum(item["result"] == "completed" for item in results)
    failed_count = sum(item["result"] != "completed" for item in results)
    return {
        "schema_version": SCHEMA_VERSION, "mode": "execute",
        "repository_root": str(root), "selected_count": len(items),
        "results": results, "completed_count": completed_count,
        "failed_count": failed_count, "pending_count": 0,
        "writes_performed": bool(items), "integrity_ok": True,
    }


def _resume_attempt_numbers(events, post_id, event_types, stage="resume"):
    attempts = set()
    for event in events:
        if (
                event.get("chinese_post_id") == int(post_id)
                and event.get("event_type") in event_types):
            evidence = event.get("evidence") or {}
            if evidence.get("stage") == stage:
                try:
                    attempts.add(int(evidence["attempt"]))
                except (KeyError, TypeError, ValueError):
                    pass
    return attempts


def _reconciled_attempt_numbers(events, post_id, stage):
    resolved = set()
    for event in events:
        if (
                event.get("chinese_post_id") == int(post_id)
                and event.get("event_type")
                == f"{stage}_orphaned_attempts_reconciled"):
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
    started = _resume_attempt_numbers(
        events, post_id, {f"{stage}_attempt_started"}, stage=stage)
    terminated = _resume_attempt_numbers(
        events, post_id,
        {f"{stage}_attempt_completed", f"{stage}_attempt_failed"},
        stage=stage)
    reconciled = _reconciled_attempt_numbers(events, post_id, stage)
    orphaned = sorted(started - terminated - reconciled)
    valid_count = len(terminated)
    current_count = int((state.get("retry_counts") or {}).get(stage, 0))
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
    if not orphaned and not recover_failed_run:
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
        started = _resume_attempt_numbers(
            events, post_id, {f"{stage}_attempt_started"}, stage=stage)
        terminated = _resume_attempt_numbers(
            events, post_id,
            {f"{stage}_attempt_completed", f"{stage}_attempt_failed"},
            stage=stage)
        reconciled = _reconciled_attempt_numbers(events, post_id, stage)
        orphaned = sorted(started - terminated - reconciled)
        recover_failed_run = bool(
            stage == "run" and state["workflow_status"] == "blocked"
            and terminated and not orphaned and execution
            and execution["status"] in {
                "prepared", "excerpt_generated", "chinese_excerpt_saved",
                "translation_started", "translation_failed"})
        if (
                (not orphaned and not recover_failed_run)
                or state["workflow_status"] == "completed"):
            return result
        attempts = dict(state.get("retry_counts") or {})
        previous_count = int(attempts.get(stage, 0))
        attempts[stage] = (
            len(terminated) if orphaned else previous_count)
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
        event = _transition_event(
            event_type, state,
            previous_status, target_status,
            (
                f"orphaned {stage} attempts removed from retry count"
                if orphaned else
                "failed run recovered without changing valid attempt count"
            ),
            {
                "stage": stage, "orphaned_attempts": orphaned,
                "terminated_attempts": sorted(terminated),
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
            "planned_count", "changed_count"):
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
    if "eligible" in result:
        suffix = (
            f" retry_count_run={result['retry_count_run']}"
            if "retry_count_run" in result else "")
        lines.append(
            f"eligible={result['eligible']} changed={result['changed']}{suffix}")
        if result["blocking_reasons"]:
            lines.append("blocked: " + ";".join(result["blocking_reasons"]))
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
            f"attempts={result.get('attempts', 1)} result=completed"
        )
    return (
        f"{prefix} 最终失败：zh={item['post_id']} "
        f"attempts={result['attempts']} result={result['result']} "
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
    live.add_argument("--json", action="store_true", dest="json_output")
    live.add_argument("--repo-root", type=Path, default=repository_root(),
                      help=argparse.SUPPRESS)
    run = subparsers.add_parser(
        "run-ready", help="preview or execute ready articles in fixed order")
    run.add_argument("--execute", action="store_true")
    run.add_argument("--batch-id")
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
                args.cbp_count_after, args.language_review_confirmed)
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
            result = validate_live(args.repo_root, args.post_id)
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
                progress=progress)
            output = render_operation_text(result)
        elif args.command == "resume":
            result = resume(
                args.repo_root, execute=args.execute, batch_id=args.batch_id,
                post_id=args.post_id)
            output = render_operation_text(result)
        elif args.command == "sync-execution":
            result = sync_execution(args.repo_root, apply=args.apply)
            output = render_operation_text(result)
        elif args.command == "recover-blocked":
            result = recover_blocked(
                args.repo_root, args.post_id, apply=args.apply)
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
