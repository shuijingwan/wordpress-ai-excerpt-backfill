#!/usr/bin/env python3
"""Build the immutable 42-row candidate manifest from the formal inventory."""

import argparse
import csv
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.candidate_execution import (CANDIDATE_FIELDS, EXPECTED_CANDIDATES, REASON,
                                     SafetyError, load_csv, load_snapshot,
                                     select_inventory_rows, validate_manifest)


def raw_hashes(paths):
    result = {}
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                result[int(record["post_id"])] = record["content_sha256"]
    return result


def build(inventory_path, raw_paths, snapshot_path, output_path):
    selected = select_inventory_rows(load_csv(inventory_path))
    if len(selected) != EXPECTED_CANDIDATES:
        raise SafetyError(f"formal inventory selection must be exactly 42, got {len(selected)}")
    hashes = raw_hashes(raw_paths)
    snapshot = load_snapshot(snapshot_path)
    rows = []
    for source in selected:
        post_id = int(source["post_id"])
        live = snapshot.get(post_id)
        if post_id not in hashes or live is None:
            raise SafetyError(f"missing audited raw record or live snapshot: {post_id}")
        if hashes[post_id] != live["chinese_content_sha256"]:
            raise SafetyError(f"Chinese content changed since inventory: {post_id}")
        rows.append({
            "chinese_post_id": post_id, "chinese_title": source["title"],
            "chinese_content_sha256": hashes[post_id], "chinese_excerpt_empty": "True",
            "english_post_id": int(source["english_post_id"]), "english_post_status": "publish",
            "english_title_sha256": live["english_title_sha256"],
            "english_excerpt_sha256": live["english_excerpt_sha256"],
            "english_content_sha256": live["english_content_sha256"],
            "candidate_reason": REASON, "execution_status": "pending",
        })
    validate_manifest(rows)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CANDIDATE_FIELDS)
            writer.writeheader(); writer.writerows(rows); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("raw", nargs="+", type=Path)
    args = parser.parse_args()
    rows = build(args.inventory, args.raw, args.snapshot, args.output)
    print(f"Candidate manifest written: {args.output}")
    print(f"Candidate count: {len(rows)}")

if __name__ == "__main__":
    main()
