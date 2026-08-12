#!/usr/bin/env python3
"""Build the one explicit final Mixed SyntaxHighlighter exception batch.

This builder intentionally has no candidate discovery mode.  The five audited
IDs below are its entire authority; normal Mixed batch selection continues to
exclude them through EXPLICIT_ABNORMAL_IDS.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analyzer import analyze_content  # noqa: E402
from src.syntaxhighlighter_batch_validation import inspect_code_block_pro  # noqa: E402


BATCH_ID = "mixed-syntaxhighlighter-special-20260812-01"
OUTPUT_NAME = "mixed-syntaxhighlighter-special-batch-20260812-01.csv"
SOURCE_TYPE = "mixed_syntaxhighlighter_special"
SOURCE_MIGRATION_TYPE = "mixed-syntaxhighlighter-to-gutenberg-code-block-pro"
EDIT_URL = "https://admin.shuijingwanwq.com/wp-admin/post.php?post={}&action=edit"

# These facts are the 2026-08-12 production read-only audit baseline.  A sixth
# post cannot be selected because this table, not a query, defines the batch.
SPECIAL_ARTICLES = (
    {
        "chinese_post_id": 2710, "english_post_id": 11572,
        "syntaxhighlighter_count": 51, "classic_pre_code_count": 1,
        "code_block_pro_count": 0, "shortcode_structure_damaged": False,
        "exception_type": "classic-pre-code+syntaxhighlighter",
    },
    {
        "chinese_post_id": 4984, "english_post_id": 14491,
        "syntaxhighlighter_count": 2, "classic_pre_code_count": 0,
        "code_block_pro_count": 0, "shortcode_structure_damaged": True,
        "exception_type": "syntaxhighlighter-related-structure-damaged",
    },
    {
        "chinese_post_id": 5152, "english_post_id": 14415,
        "syntaxhighlighter_count": 6, "classic_pre_code_count": 1,
        "code_block_pro_count": 0, "shortcode_structure_damaged": False,
        "exception_type": "classic-pre-code+syntaxhighlighter",
    },
    {
        "chinese_post_id": 5520, "english_post_id": 14235,
        "syntaxhighlighter_count": 4, "classic_pre_code_count": 1,
        "code_block_pro_count": 0, "shortcode_structure_damaged": False,
        "exception_type": "classic-pre-code+syntaxhighlighter",
    },
    {
        "chinese_post_id": 12389, "english_post_id": 12394,
        "syntaxhighlighter_count": 4, "classic_pre_code_count": 0,
        "code_block_pro_count": 1, "shortcode_structure_damaged": False,
        "exception_type": "code-block-pro+syntaxhighlighter",
        "preserved_code_block_pro_language": "go",
    },
)
FIELDS = (
    "schema_version", "batch_id", "batch_sequence", "batch_expected_count",
    "allocated_at", "source_type", "source_editor_format",
    "target_editor_format", "source_migration_type", "snapshot_id",
    "snapshot_generated_at", "chinese_post_id", "english_post_id",
    "chinese_title", "published_at", "edit_url", "permalink",
    "content_sha256", "before_syntaxhighlighter_count",
    "before_classic_pre_code_count", "before_code_block_pro_count",
    "before_shortcode_structure_damaged", "before_code_block_pro_languages",
    "preserved_code_block_pro_code_sha256", "special_exception_type",
    "expected_syntaxhighlighter_count_after",
    "expected_code_block_pro_count_after", "migration_status",
    "validation_status", "validation_reasons",
)


class SpecialBatchError(ValueError):
    pass


def _load_jsonl(paths, key):
    values = {}
    aggregate = hashlib.sha256()
    exported_at = []
    for path in sorted((Path(item) for item in paths), key=lambda item: item.name):
        aggregate.update(path.name.encode("utf-8")); aggregate.update(b"\0")
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise SpecialBatchError(f"unable to read {path}: {error}") from error
        aggregate.update(hashlib.sha256(raw).digest())
        for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            try:
                item = json.loads(line)
                value = int(item[key])
            except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise SpecialBatchError(f"{path}:{number}: invalid JSONL record") from error
            if value in values:
                raise SpecialBatchError(f"duplicate {key}={value} across raw input")
            values[value] = item
            if "exported_at" in item:
                exported_at.append(item["exported_at"])
    return values, aggregate.hexdigest()[:16], max(exported_at, default="")


def _history_module():
    path = ROOT / "bin/history-migration.py"
    spec = importlib.util.spec_from_file_location("special_batch_history", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_csv(path, rows):
    target = Path(path)
    if target.exists():
        raise SpecialBatchError(f"refusing to overwrite existing output: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader(); writer.writerows(rows)
            handle.flush(); os.fsync(handle.fileno())
        if target.exists():
            raise SpecialBatchError(f"refusing to overwrite existing output: {target}")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_batch(raw_paths, translations_path, output_path, repository_root=ROOT,
                allocated_at=None, config_path=None):
    output = Path(output_path)
    if output.name != OUTPUT_NAME:
        raise SpecialBatchError(f"output filename must be {OUTPUT_NAME}")
    repository_root = Path(repository_root).resolve()
    history = _history_module()
    errors, conflicts = [], []
    batches = history.discover_batches(repository_root, errors)
    fixed = history.validate_batch_index(batches, conflicts, errors)
    if errors or conflicts:
        raise SpecialBatchError("existing history integrity failed")
    if any(item["chinese_post_id"] in fixed for item in SPECIAL_ARTICLES):
        raise SpecialBatchError("special article already belongs to a fixed batch")
    if any((repository_root / "data/analysis").glob("mixed-syntaxhighlighter-special-batch-*.csv")):
        raise SpecialBatchError("a special Mixed SyntaxHighlighter batch already exists")
    posts, source_digest, exported_at = _load_jsonl(raw_paths, "post_id")
    relations, _, _ = _load_jsonl([translations_path], "post_id")
    config = json.loads(Path(config_path or ROOT / "config/classification.json").read_text(encoding="utf-8"))
    timestamp = allocated_at or datetime.now(timezone.utc).isoformat()
    rows = []
    for definition in SPECIAL_ARTICLES:
        zh_id = definition["chinese_post_id"]
        en_id = definition["english_post_id"]
        post = posts.get(zh_id)
        relation = relations.get(zh_id)
        if post is None or relation is None:
            raise SpecialBatchError(f"missing fixed special article input: {zh_id}")
        if not (
                post.get("post_id") == zh_id and post.get("post_type") == "post"
                and post.get("post_status") == "publish"
                and post.get("language_source") == "polylang"
                and post.get("language") == "zh" and not post.get("excerpt", "").strip()
                and relation.get("has_english_translation") is True
                and relation.get("english_post_id") == en_id
                and relation.get("english_post_status") == "publish"):
            raise SpecialBatchError(f"fixed special article integrity mismatch: {zh_id}")
        actual_hash = hashlib.sha256(post["content"].encode("utf-8")).hexdigest()
        if actual_hash != post.get("content_sha256"):
            raise SpecialBatchError(f"content SHA-256 mismatch: {zh_id}")
        analysis = analyze_content(post["content"], config)
        cbp = inspect_code_block_pro(post["content"])
        classic_count = analysis["rule_counts"].get("CLASSIC_PRE_CODE", 0)
        if not (
                analysis["syntaxhighlighter_count"] == definition["syntaxhighlighter_count"]
                and classic_count == definition["classic_pre_code_count"]
                and analysis["code_block_pro_count"] == definition["code_block_pro_count"]
                and analysis["shortcodes"]["damaged"] == definition["shortcode_structure_damaged"]):
            raise SpecialBatchError(f"special audit baseline mismatch: {zh_id}")
        languages = [item["language"] for item in cbp["blocks"]]
        preserved_hash = ""
        if "preserved_code_block_pro_language" in definition:
            if languages != [definition["preserved_code_block_pro_language"]]:
                raise SpecialBatchError(f"preserved Code Block Pro language mismatch: {zh_id}")
            preserved_hash = cbp["blocks"][0]["code_sha256"]
        rows.append({
            "schema_version": 1, "batch_id": BATCH_ID, "batch_sequence": 1,
            "batch_expected_count": len(SPECIAL_ARTICLES), "allocated_at": timestamp,
            "source_type": SOURCE_TYPE, "source_editor_format": "mixed",
            "target_editor_format": "gutenberg",
            "source_migration_type": SOURCE_MIGRATION_TYPE,
            "snapshot_id": f"production-readonly-special-audit-20260812-01-sha256-{source_digest}",
            "snapshot_generated_at": timestamp,
            "chinese_post_id": zh_id, "english_post_id": en_id,
            "chinese_title": post["title"], "published_at": post["published_at"],
            "edit_url": EDIT_URL.format(zh_id), "permalink": post["permalink"],
            "content_sha256": actual_hash,
            "before_syntaxhighlighter_count": analysis["syntaxhighlighter_count"],
            "before_classic_pre_code_count": classic_count,
            "before_code_block_pro_count": analysis["code_block_pro_count"],
            "before_shortcode_structure_damaged": str(analysis["shortcodes"]["damaged"]),
            "before_code_block_pro_languages": "|".join(languages),
            "preserved_code_block_pro_code_sha256": preserved_hash,
            "special_exception_type": definition["exception_type"],
            "expected_syntaxhighlighter_count_after": 0,
            "expected_code_block_pro_count_after": (
                analysis["code_block_pro_count"] + analysis["syntaxhighlighter_count"]),
            "migration_status": "pending", "validation_status": "not-checked",
            "validation_reasons": "",
        })
    _write_csv(output, rows)
    return rows, {
        "batch_id": BATCH_ID, "selected_count": len(rows),
        "source_exported_at": exported_at, "snapshot_id": rows[0]["snapshot_id"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the fixed final special Mixed batch")
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--allocated-at")
    parser.add_argument("raw", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        rows, stats = build_batch(args.raw, args.translations, args.output,
                                  args.repo_root, args.allocated_at, args.config)
    except SpecialBatchError as error:
        parser.error(str(error))
    print(f"Batch written: {args.output}")
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
