#!/usr/bin/env python3
"""Validate all fixed candidates from a read-only live snapshot."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.candidate_execution import dry_run, load_csv, load_snapshot, select_inventory_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    args = parser.parse_args()
    inventory = load_csv(args.inventory)
    protected = [row["post_id"] for row in inventory if (
        row.get("category") == "gutenberg-code-block-pro" and row.get("excerpt_empty") == "False")]
    result = dry_run(load_csv(args.manifest), load_snapshot(args.snapshot), protected)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["skipped"] == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
