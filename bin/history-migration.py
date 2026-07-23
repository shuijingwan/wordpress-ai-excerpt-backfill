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
import sys
import tempfile


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
    "validation_failed", "ready_for_excerpt", "excerpt_failed",
    "ready_for_translation_resume", "translation_failed", "completed", "blocked",
    "paused",
}

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
    return {
        "batch_id": next(iter(batch_ids)),
        "source_file": _relative(path, root),
        "source_type": "syntaxhighlighter_daily",
        "expected_count": 20,
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
    return result


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
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "status":
        result = build_status(args.repo_root)
        output = render_text(result)
    else:
        result = init_state(args.repo_root, apply=args.apply)
        output = render_init_text(result)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(output)
    if result["integrity_ok"]:
        return EXIT_OK
    return result.get("exit_code", EXIT_INTEGRITY_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
