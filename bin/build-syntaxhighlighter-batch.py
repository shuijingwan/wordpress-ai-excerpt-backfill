#!/usr/bin/env python3
"""Build one immutable manual SyntaxHighlighter migration batch from a local preview."""

import argparse
import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile


FIELDS = (
    "schema_version", "batch_id", "batch_sequence", "allocated_at",
    "chinese_post_id", "english_post_id", "chinese_title", "published_at",
    "edit_url", "permalink", "before_content_sha256",
    "before_syntaxhighlighter_count", "before_code_block_pro_count",
    "expected_syntaxhighlighter_count_after", "expected_code_block_pro_count_after",
    "migration_status", "validation_status", "validation_reasons",
)
REQUIRED_PREVIEW_FIELDS = {
    "chinese_post_id", "english_post_id", "chinese_title", "published_at", "permalink",
    "chinese_excerpt_empty", "english_status", "syntaxhighlighter_count",
    "syntaxhighlighter_balanced", "code_block_pro_count", "mixed_code_formats",
    "content_sha256", "old_phase1_manifest_member", "preview_status",
}
EDIT_URL = "https://admin.shuijingwanwq.com/wp-admin/post.php?post={}&action=edit"
BATCH_PATTERN = "syntaxhighlighter-migration-batch-*.csv"


class BatchError(ValueError):
    pass


def _true(value):
    return str(value).strip().lower() == "true"


def _read_csv(path, required=()):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = set(required) - fields
        if missing:
            raise BatchError(f"{path}: missing fields: {', '.join(sorted(missing))}")
        return list(reader)


def _ids(path):
    return {int(row["chinese_post_id"]) for row in _read_csv(path, {"chinese_post_id"})}


def _eligible(row):
    try:
        return (
            row["preview_status"] == "ready"
            and _true(row["chinese_excerpt_empty"])
            and row["english_status"] == "publish"
            and int(row["syntaxhighlighter_count"]) >= 1
            and _true(row["syntaxhighlighter_balanced"])
            and int(row["code_block_pro_count"]) == 0
            and not _true(row["mixed_code_formats"])
            and not _true(row["old_phase1_manifest_member"])
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BatchError(f"invalid preview row for post {row.get('chinese_post_id')}") from error


def _existing_batches(output):
    output = Path(output)
    return sorted(path for path in output.parent.glob(BATCH_PATTERN) if path != output)


def _allocated_ids_and_next_sequence(paths):
    allocated = set()
    sequences = []
    for path in paths:
        rows = _read_csv(path, {"chinese_post_id", "batch_sequence"})
        if not rows:
            raise BatchError(f"existing batch is empty: {path}")
        values = {int(row["batch_sequence"]) for row in rows}
        if len(values) != 1:
            raise BatchError(f"existing batch has inconsistent sequence: {path}")
        sequences.extend(values)
        allocated.update(int(row["chinese_post_id"]) for row in rows)
    unique = sorted(set(sequences))
    if unique != list(range(1, len(unique) + 1)) or len(unique) != len(sequences):
        raise BatchError("existing batch sequences must be unique and continuous from 1")
    return allocated, len(unique) + 1


def build_batch(preview_path, output_path, expected_count, batch_id, pilot_manifest,
                old_phase1_manifest, allocated_at=None):
    if type(expected_count) is not int or expected_count < 1:
        raise BatchError("expected_count must be a positive integer")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise BatchError("batch_id is required")
    output = Path(output_path)
    if output.exists():
        raise BatchError(f"refusing to overwrite existing output: {output}")

    preview = _read_csv(preview_path, REQUIRED_PREVIEW_FIELDS)
    preview_ids = [int(row["chinese_post_id"]) for row in preview]
    if len(preview_ids) != len(set(preview_ids)):
        raise BatchError("preview contains duplicate Chinese post IDs")
    ready_rows = [row for row in preview if row["preview_status"] == "ready"]
    eligible_rows = [row for row in ready_rows if _eligible(row)]
    pilot_ids = _ids(pilot_manifest)
    old_ids = _ids(old_phase1_manifest)
    existing_paths = _existing_batches(output)
    batch_ids, sequence = _allocated_ids_and_next_sequence(existing_paths)

    eligible_ids = {int(row["chinese_post_id"]) for row in eligible_rows}
    excluded_pilot = eligible_ids & pilot_ids
    excluded_old = eligible_ids & old_ids
    excluded_batches = eligible_ids & batch_ids
    excluded = pilot_ids | old_ids | batch_ids
    available = [row for row in eligible_rows if int(row["chinese_post_id"]) not in excluded]
    # Count is ascending, while date and ID are descending.
    available.sort(key=lambda row: int(row["chinese_post_id"]), reverse=True)
    available.sort(key=lambda row: row["published_at"], reverse=True)
    available.sort(key=lambda row: int(row["syntaxhighlighter_count"]))
    if len(available) < expected_count:
        raise BatchError(
            f"not enough eligible unallocated candidates: expected {expected_count}, found {len(available)}"
        )

    timestamp = allocated_at or datetime.now(timezone.utc).isoformat()
    selected = []
    for row in available[:expected_count]:
        before_sh = int(row["syntaxhighlighter_count"])
        before_cbp = int(row["code_block_pro_count"])
        selected.append({
            "schema_version": 1,
            "batch_id": batch_id,
            "batch_sequence": sequence,
            "allocated_at": timestamp,
            "chinese_post_id": int(row["chinese_post_id"]),
            "english_post_id": int(row["english_post_id"]),
            "chinese_title": row["chinese_title"],
            "published_at": row["published_at"],
            "edit_url": EDIT_URL.format(int(row["chinese_post_id"])),
            "permalink": row["permalink"],
            "before_content_sha256": row["content_sha256"],
            "before_syntaxhighlighter_count": before_sh,
            "before_code_block_pro_count": before_cbp,
            "expected_syntaxhighlighter_count_after": 0,
            "expected_code_block_pro_count_after": before_cbp + before_sh,
            "migration_status": "pending",
            "validation_status": "not-checked",
            "validation_reasons": "",
        })

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader(); writer.writerows(selected)
            handle.flush(); os.fsync(handle.fileno())
        if output.exists():
            raise BatchError(f"refusing to overwrite existing output: {output}")
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)

    return selected, {
        "preview_ready": len(ready_rows),
        "strictly_eligible": len(eligible_rows),
        "excluded_pilot_ids": sorted(excluded_pilot),
        "excluded_old_count": len(excluded_old),
        "excluded_existing_batch_count": len(excluded_batches),
        "available_before_allocation": len(available),
        "remaining_unallocated_ready": len(available) - len(selected),
        "batch_sequence": sequence,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build one fixed manual migration batch")
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-count", type=int, default=20)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--pilot-manifest", required=True, type=Path)
    parser.add_argument("--old-phase1-manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        rows, stats = build_batch(
            args.preview, args.output, args.expected_count, args.batch_id,
            args.pilot_manifest, args.old_phase1_manifest,
        )
    except BatchError as error:
        parser.error(str(error))
    print(f"Batch written: {args.output}")
    print(f"Candidates: {len(rows)}")
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
