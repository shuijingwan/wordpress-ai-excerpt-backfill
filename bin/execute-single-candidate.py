#!/usr/bin/env python3
"""Execute exactly one fixed candidate; defaults to an offline snapshot dry run."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.candidate_execution import (SafetyError, authorize_live_selection, load_csv,
                                     load_snapshot, validate_live)


DEFAULT_MANIFEST = ROOT / "data/analysis/gutenberg-cbp-empty-excerpt-candidates.csv"
DEFAULT_SNAPSHOT = ROOT / "data/analysis/gutenberg-cbp-empty-excerpt-live-snapshot.jsonl"
DEFAULT_BACKUPS = ROOT / "data/backups/single-candidate"


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--post-id", required=True, type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--preflight-live", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--expected-candidate-count", type=int, default=42)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUPS)
    args = parser.parse_args(argv)
    if args.resume and not args.execute:
        parser.error("--resume requires --execute")
    if args.expected_candidate_count < 1:
        parser.error("--expected-candidate-count must be positive")
    return args


def main(argv=None):
    args = parse_args(argv)
    rows = load_csv(args.manifest)
    authorize_live_selection(
        rows, [args.post_id], expected_count=args.expected_candidate_count
    )
    row = next(item for item in rows if int(item["chinese_post_id"]) == args.post_id)
    if args.preflight_live:
        # This branch imports only the GET-capable WordPress client. It does not
        # import or construct GLM/SlyTranslate clients and writes no local files.
        from src.polylang_ssh import PolylangSshChecker
        from src.single_candidate_flow import preflight_live_result
        from src.wordpress_clients import WordPressRestClient
        config = json.loads((ROOT / "config/classification.json").read_text(encoding="utf-8"))
        result = preflight_live_result(row, WordPressRestClient(), PolylangSshChecker(), config)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["preflight_passed"] else 1
    if not args.execute:
        live = load_snapshot(args.snapshot).get(args.post_id)
        failures = ["snapshot_missing"] if live is None else validate_live(row, live)
        result = {
            "mode": "dry-run", "chinese_post_id": args.post_id,
            "english_post_id": int(row["english_post_id"]),
            "passed": not failures, "failures": failures,
            "glm_calls": 0, "wordpress_writes": 0, "wordpress_post_calls": 0,
            "ssh_readonly_calls": 0, "translation_calls": 0,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not failures else 1

    # Imports and credential reads occur only after explicit --execute.
    from src.polylang_ssh import PolylangSshChecker
    from src.single_candidate_flow import SingleCandidateFlow
    from src.wordpress_clients import SlyTranslateClient, WordPressRestClient

    config = json.loads((ROOT / "config/classification.json").read_text(encoding="utf-8"))
    wp_client = WordPressRestClient()
    translator_client = SlyTranslateClient()
    polylang_checker = PolylangSshChecker()
    glm_client = None
    if not args.resume:
        from src.glm47_excerpt_client import Glm47ExcerptClient
        glm_client = Glm47ExcerptClient()
    flow = SingleCandidateFlow(rows, wp_client, glm_client, translator_client,
                               polylang_checker, args.backup_dir, config,
                               expected_candidate_count=args.expected_candidate_count)
    state = flow.execute(args.post_id, resume=args.resume)
    print(json.dumps({"chinese_post_id": args.post_id, "english_post_id": state["english_post_id"],
                      "status": state["status"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        rejected_paths = getattr(error, "rejected_excerpt_paths", None)
        if rejected_paths:
            for rejected_path in rejected_paths:
                print(f"Rejected excerpt saved to: {rejected_path}", file=sys.stderr)
        else:
            rejected_path = getattr(error, "rejected_excerpt_path", None)
            if rejected_path is not None:
                print(f"Rejected excerpt saved to: {rejected_path}", file=sys.stderr)
        raise SystemExit(2)
