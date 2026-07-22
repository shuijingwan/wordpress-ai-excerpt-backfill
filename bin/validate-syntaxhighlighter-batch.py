#!/usr/bin/env python3
"""Validate a fixed SyntaxHighlighter migration batch using production read-only reads."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.candidate_execution import SafetyError
from src.syntaxhighlighter_batch_validation import (load_batch, validate_batch,
                                                     write_outputs)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--expected-count", type=int, default=20)
    args = parser.parse_args(argv)
    if args.output.exists() or args.snapshot.exists():
        parser.error("refusing to overwrite validation output or live snapshot")
    try:
        rows = load_batch(args.batch, args.expected_count)
        config = json.loads((ROOT / "config/classification.json").read_text(encoding="utf-8"))
        # One SSH/PHP call reads only the fixed post pairs. It never constructs
        # REST write, GLM, or SlyTranslate clients.
        from src.batch_readonly_ssh import BatchReadonlySshSource
        source = BatchReadonlySshSource.fetch(rows)
        results = validate_batch(rows, source, source, config)
        write_outputs(results, args.output, args.snapshot)
    except SafetyError as error:
        parser.error(str(error))
    counts = Counter(row["validation_status"] for row in results)
    print(json.dumps({
        "batch_id": rows[0]["batch_id"], "count": len(results),
        "ready": counts["ready"], "pending": counts["pending"],
        "abnormal": counts["abnormal"], "wordpress_readonly_post_reads": len(results) * 2,
        "polylang_readonly_checks": len(results), "ssh_readonly_calls": 1,
        "wordpress_writes": 0,
        "glm_calls": 0, "translation_calls": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if counts["pending"] == counts["abnormal"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
