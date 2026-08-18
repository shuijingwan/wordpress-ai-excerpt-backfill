#!/usr/bin/env python3
"""Build a manual-review audit from fresh, read-only production JSONL exports."""

import argparse
from collections import Counter
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config/syntaxhighlighter-retirement.json"
DEFAULT_CSV = PROJECT_ROOT / "data/analysis/syntaxhighlighter-retirement-audit.csv"
DEFAULT_TXT = PROJECT_ROOT / "data/analysis/syntaxhighlighter-retirement-audit.txt"
EDIT_URL = "https://admin.shuijingwanwq.com/wp-admin/post.php?post={post_id}&action=edit"
FIELDS = (
    "post_id", "language", "post_type", "post_status", "title",
    "published_at", "permalink", "matched_shortcodes", "match_count", "edit_url",
)
REQUIRED_INPUT_FIELDS = {
    "schema_version", "post_id", "language_source", "language", "post_type",
    "post_status", "title", "published_at", "permalink", "content", "content_sha256",
}


class AuditError(ValueError):
    pass


def load_config(path):
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle)
    shortcodes = config.get("shortcodes") if isinstance(config, dict) else None
    if not isinstance(shortcodes, list) or not shortcodes or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-z0-9_-]+", item)
            for item in shortcodes):
        raise AuditError("config shortcodes must be a non-empty list of lowercase names")
    if len(shortcodes) != len(set(shortcodes)):
        raise AuditError("config contains duplicate shortcodes")
    if config.get("shortcode_case_sensitive") is not True:
        raise AuditError("production config must explicitly require case-sensitive matching")
    return config


def build_pattern(shortcodes):
    names = "|".join(re.escape(name) for name in sorted(shortcodes, key=len, reverse=True))
    # Mirrors relevant WordPress shortcode boundaries, while excluding escaped
    # literal forms such as [[php]...[/php]]. Matching is deliberately global:
    # code blocks, pre/code HTML, comments, and freeform content are not protected.
    return re.compile(
        rf"(?<!\[)\[(?P<close>/)?(?P<name>{names})(?![\w-])"
        rf"(?P<attributes>[^\]]*?)\](?!\])"
    )


def validate_record(record, source, line_number, seen):
    if not isinstance(record, dict):
        raise AuditError(f"{source}:{line_number}: record must be an object")
    missing = sorted(REQUIRED_INPUT_FIELDS - record.keys())
    if missing:
        raise AuditError(f"{source}:{line_number}: missing fields: {', '.join(missing)}")
    if record["schema_version"] != 1:
        raise AuditError(f"{source}:{line_number}: schema_version must be 1")
    if (record["post_type"], record["post_status"], record["language_source"], record["language"]) != (
            "post", "publish", "polylang", "zh"):
        raise AuditError(f"{source}:{line_number}: record is outside post + publish + Polylang zh")
    post_id = record["post_id"]
    if not isinstance(post_id, int) or isinstance(post_id, bool) or post_id < 1:
        raise AuditError(f"{source}:{line_number}: post_id must be a positive integer")
    if post_id in seen:
        raise AuditError(f"{source}:{line_number}: duplicate post_id {post_id}")
    seen.add(post_id)
    for field in ("title", "published_at", "permalink", "content", "content_sha256"):
        if not isinstance(record[field], str):
            raise AuditError(f"{source}:{line_number}: {field} must be a string")
    actual = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", record["content_sha256"]) or actual != record["content_sha256"]:
        raise AuditError(f"{source}:{line_number}: content_sha256 mismatch")


def load_records(paths):
    records, seen = [], set()
    for path_value in paths:
        path = Path(path_value)
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise AuditError(f"{path}:{line_number}: blank record")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AuditError(f"{path}:{line_number}: invalid JSON: {error.msg}") from error
                validate_record(record, path, line_number, seen)
                records.append(record)
    if not records:
        raise AuditError("input contains no records")
    return records


def audit(records, pattern, shortcode_order):
    order = {name: position for position, name in enumerate(shortcode_order)}
    rows = []
    for record in records:
        counts = Counter(match.group("name") for match in pattern.finditer(record["content"]))
        if not counts:
            continue
        names = sorted(counts, key=lambda name: order[name])
        rows.append({
            "post_id": record["post_id"], "language": "zh", "post_type": "post",
            "post_status": "publish", "title": record["title"],
            "published_at": record["published_at"], "permalink": record["permalink"],
            "matched_shortcodes": ", ".join(f"[{name}]" for name in names),
            "match_count": sum(counts.values()),
            "edit_url": EDIT_URL.format(post_id=record["post_id"]),
        })
    rows.sort(key=lambda row: (row["published_at"], row["post_id"]), reverse=True)
    return rows


def atomic_write(path, writer):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_reports(rows, csv_path, txt_path):
    def csv_writer(handle):
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    def txt_writer(handle):
        handle.write("SyntaxHighlighter retirement audit（人工检查候选清单；命中不代表必须修改）\n\n")
        for index, row in enumerate(rows):
            if index:
                handle.write("---\n\n")
            handle.write(
                f"ID: {row['post_id']}\n标题: {row['title']}\n发布时间: {row['published_at']}\n"
                f"命中: {row['matched_shortcodes']}\n次数: {row['match_count']}\n"
                f"前台:\n{row['permalink']}\n编辑:\n{row['edit_url']}\n\n"
            )

    atomic_write(csv_path, csv_writer)
    atomic_write(txt_path, txt_writer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="fresh JSONL page; repeatable")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--txt", default=DEFAULT_TXT)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        records = load_records(args.input)
        rows = audit(records, build_pattern(config["shortcodes"]), config["shortcodes"])
        write_reports(rows, args.csv, args.txt)
    except (AuditError, OSError) as error:
        parser.error(str(error))
    print(f"Scanned {len(records)} records; wrote {len(rows)} manual-review candidates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
