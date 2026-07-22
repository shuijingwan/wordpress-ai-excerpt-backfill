#!/usr/bin/env python3
"""Build a review-only SyntaxHighlighter migration preview from local snapshots."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analyzer import analyze_content  # noqa: E402


FIELDS = (
    "chinese_post_id", "english_post_id", "chinese_title", "published_at",
    "permalink", "chinese_excerpt_empty", "english_status", "editor_format",
    "syntaxhighlighter_count", "syntaxhighlighter_languages",
    "syntaxhighlighter_balanced", "code_block_pro_count", "mixed_code_formats",
    "content_sha256", "old_phase1_manifest_member", "preview_status", "preview_reasons",
)


class PreviewError(ValueError):
    pass


def _load_jsonl(path, key):
    records = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise PreviewError(f"{path}:{line_number}: blank record")
            record = json.loads(line)
            value = record[key]
            if value in records:
                raise PreviewError(f"{path}:{line_number}: duplicate {key}")
            records[value] = record
    return records


def _load_posts(paths):
    posts = {}
    for path in paths:
        for post_id, record in _load_jsonl(path, "post_id").items():
            if post_id in posts:
                raise PreviewError(f"duplicate post_id across raw inputs: {post_id}")
            actual = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
            if actual != record["content_sha256"]:
                raise PreviewError(f"content_sha256 mismatch: {post_id}")
            posts[post_id] = record
    return posts


def _old_ids(path):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return {int(row["chinese_post_id"]) for row in csv.DictReader(handle)}


def build_preview(raw_paths, translations_path, old_manifest_path, output_path, config_path=None):
    config = json.loads(
        Path(config_path or ROOT / "config/classification.json").read_text(encoding="utf-8")
    )
    posts = _load_posts(raw_paths)
    translations = _load_jsonl(translations_path, "post_id")
    old_ids = _old_ids(old_manifest_path)
    rows = []
    for post_id, post in sorted(posts.items()):
        if (post.get("post_type"), post.get("post_status")) != ("post", "publish"):
            continue
        if (post.get("language_source"), post.get("language")) != ("polylang", "zh"):
            continue
        analysis = analyze_content(post["content"], config)
        if analysis["syntaxhighlighter_count"] < 1 or post["excerpt"].strip():
            continue
        relation = translations.get(post_id)
        if not relation or not relation.get("has_english_translation"):
            continue
        if relation.get("english_post_status") != "publish" or post_id in old_ids:
            continue

        reasons = []
        families = set(analysis["code_format_families"])
        if analysis["editor_format"] != "gutenberg":
            reasons.append("editor-format-" + analysis["editor_format"])
        if not analysis["syntaxhighlighter_balanced"]:
            reasons.append("syntaxhighlighter-unbalanced")
        if not analysis["syntaxhighlighter_attributes_valid"]:
            reasons.append("syntaxhighlighter-attributes-invalid")
        if "SH_DAMAGED" in analysis["matched_rule_ids"]:
            reasons.append("syntaxhighlighter-related-structure-damaged")
        if analysis["mixed_code_formats"]:
            reasons.append("mixed-code-formats:" + "|".join(sorted(families)))
        if reasons:
            status = "mixed" if analysis["mixed_code_formats"] else "abnormal"
        else:
            status = "ready"
        rows.append({
            "chinese_post_id": post_id,
            "english_post_id": relation["english_post_id"],
            "chinese_title": post["title"],
            "published_at": post["published_at"],
            "permalink": post["permalink"],
            "chinese_excerpt_empty": True,
            "english_status": relation["english_post_status"],
            "editor_format": analysis["editor_format"],
            "syntaxhighlighter_count": analysis["syntaxhighlighter_count"],
            "syntaxhighlighter_languages": "|".join(analysis["syntaxhighlighter_languages"]),
            "syntaxhighlighter_balanced": analysis["syntaxhighlighter_balanced"],
            "code_block_pro_count": analysis["code_block_pro_count"],
            "mixed_code_formats": analysis["mixed_code_formats"],
            "content_sha256": post["content_sha256"],
            "old_phase1_manifest_member": False,
            "preview_status": status,
            "preview_reasons": ";".join(reasons),
        })

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a non-executable local preview CSV")
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--old-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("raw", nargs="+", type=Path)
    args = parser.parse_args(argv)
    rows = build_preview(args.raw, args.translations, args.old_manifest, args.output, args.config)
    print(f"Preview rows: {len(rows)}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
