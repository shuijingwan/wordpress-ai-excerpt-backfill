#!/usr/bin/env python3
"""Build one immutable Mixed Gutenberg + SyntaxHighlighter fixed batch."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analyzer import analyze_content  # noqa: E402


SOURCE_TYPE = "mixed_syntaxhighlighter_daily"
SOURCE_EDITOR_FORMAT = "mixed"
TARGET_EDITOR_FORMAT = "gutenberg"
SOURCE_MIGRATION_TYPE = (
    "mixed-syntaxhighlighter-to-gutenberg-code-block-pro"
)
# Historical analysis confirmed that these posts also contain another code
# format or a structural anomaly. They are deliberately excluded from normal
# Mixed daily batches and must be handled by a separate manual workflow.
EXPLICIT_ABNORMAL_IDS = {2710, 4984, 5152, 5520, 12389}
EDIT_URL = "https://admin.shuijingwanwq.com/wp-admin/post.php?post={}&action=edit"
FIXED_BATCH_NAME = re.compile(
    r"^mixed-syntaxhighlighter-migration-batch-(?P<suffix>\d{8}-\d{2})\.csv$")
BATCH_ID_PATTERN = re.compile(
    r"^mixed-syntaxhighlighter-(?P<suffix>\d{8}-\d{2})$")
FIELDS = (
    "schema_version", "batch_id", "batch_sequence", "batch_expected_count",
    "allocated_at", "source_type", "source_editor_format",
    "target_editor_format", "source_migration_type",
    "snapshot_id", "snapshot_generated_at",
    "chinese_post_id", "english_post_id", "chinese_title", "published_at",
    "edit_url", "permalink", "content_sha256",
    "before_syntaxhighlighter_count", "before_code_block_pro_count",
    "expected_syntaxhighlighter_count_after",
    "expected_code_block_pro_count_after",
    "migration_status", "validation_status", "validation_reasons",
)
REQUIRED_PREVIEW_FIELDS = {
    "chinese_post_id", "english_post_id", "chinese_title", "published_at",
    "permalink", "chinese_excerpt_empty", "english_status", "editor_format",
    "syntaxhighlighter_count", "syntaxhighlighter_balanced",
    "code_block_pro_count", "mixed_code_formats", "content_sha256",
    "old_phase1_manifest_member", "preview_status", "preview_reasons",
}


class MixedBatchError(ValueError):
    pass


def _true(value):
    return str(value).strip().lower() == "true"


def _read_csv(path, required=()):
    try:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            fields = set(reader.fieldnames or ())
            missing = set(required) - fields
            if missing:
                raise MixedBatchError(
                    f"{path}: missing fields: {', '.join(sorted(missing))}")
            return list(reader)
    except MixedBatchError:
        raise
    except (OSError, UnicodeError, csv.Error) as error:
        raise MixedBatchError(f"{path}: unable to read CSV: {error}") from error


def _load_jsonl(path, key):
    records = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
                value = int(record[key])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise MixedBatchError(
                    f"{path}:{line_number}: invalid JSONL record") from error
            if value in records:
                raise MixedBatchError(f"{path}:{line_number}: duplicate {key}={value}")
            records[value] = record
    return records


def _load_posts(paths):
    posts = {}
    exported_at = []
    aggregate = hashlib.sha256()
    for path in sorted(map(Path, paths), key=lambda item: item.name):
        aggregate.update(path.name.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(hashlib.sha256(path.read_bytes()).digest())
        for post_id, record in _load_jsonl(path, "post_id").items():
            if post_id in posts:
                raise MixedBatchError(f"duplicate post_id across raw files: {post_id}")
            actual = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
            if actual != record.get("content_sha256"):
                raise MixedBatchError(f"content SHA-256 mismatch: {post_id}")
            posts[post_id] = record
            exported_at.append(record.get("exported_at", ""))
    if not posts or not all(exported_at):
        raise MixedBatchError("raw snapshot is empty or lacks exported_at")
    dates = sorted({value[:10].replace("-", "") for value in exported_at})
    snapshot_id = (
        f"wordpress-zh-posts-{'-'.join(dates)}-sha256-"
        f"{aggregate.hexdigest()[:16]}"
    )
    return posts, snapshot_id, max(exported_at)


def _history_module():
    path = ROOT / "bin/history-migration.py"
    spec = importlib.util.spec_from_file_location("mixed_batch_history", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _historical_exclusions(repository_root):
    history = _history_module()
    errors = []
    conflicts = []
    batches = history.discover_batches(Path(repository_root), errors)
    history.validate_batch_index(batches, conflicts, errors)
    if errors or conflicts:
        raise MixedBatchError(
            "existing history integrity failed: "
            + json.dumps(
                {"errors": errors, "conflicts": conflicts},
                ensure_ascii=False, sort_keys=True))
    chinese = set()
    english = set()
    for batch in batches:
        for article in batch["articles"]:
            chinese.add(article["chinese_post_id"])
            english.add(article["english_post_id"])

    execution_errors = []
    executions = history.read_execution_states(
        Path(repository_root), execution_errors)
    if execution_errors:
        raise MixedBatchError(
            "execution state integrity failed: " + "; ".join(execution_errors))
    for post_id, state in executions.items():
        if state["status"] == "completed":
            chinese.add(post_id)
            english.add(state["english_post_id"])

    state_root = Path(repository_root) / history.STATE_ROOT
    for path in sorted(state_root.glob("*/chinese-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise MixedBatchError(f"{path}: invalid coordination state") from error
        if state.get("workflow_status") == "completed":
            chinese.add(int(state["chinese_post_id"]))
            english.add(int(state["english_post_id"]))
    return chinese, english


def _eligible(preview, post, relation, analysis):
    return (
        preview["preview_reasons"] == "editor-format-mixed"
        and preview["preview_status"] == "abnormal"
        and preview["editor_format"] == "mixed"
        and _true(preview["chinese_excerpt_empty"])
        and preview["english_status"] == "publish"
        and int(preview["syntaxhighlighter_count"]) >= 1
        and _true(preview["syntaxhighlighter_balanced"])
        and int(preview["code_block_pro_count"]) == 0
        and not _true(preview["mixed_code_formats"])
        and not _true(preview["old_phase1_manifest_member"])
        and post.get("post_type") == "post"
        and post.get("post_status") == "publish"
        and post.get("language_source") == "polylang"
        and post.get("language") == "zh"
        and not post.get("excerpt", "").strip()
        and relation is not None
        and relation.get("has_english_translation") is True
        and relation.get("english_post_status") == "publish"
        and int(preview["english_post_id"]) == int(relation["english_post_id"])
        and analysis["editor_format"] == "mixed"
        and analysis["syntaxhighlighter_count"] >= 1
        and analysis["syntaxhighlighter_balanced"]
        and analysis["syntaxhighlighter_attributes_valid"]
        and analysis["code_block_pro_count"] == 0
        and not analysis["mixed_code_formats"]
    )


def _same_text(preview, preview_field, post, post_field, post_id):
    if preview.get(preview_field) != post.get(post_field):
        raise MixedBatchError(
            f"preview/raw {preview_field} mismatch: {post_id}")


def _validate_preview_snapshot(preview, post, analysis):
    post_id = int(preview["chinese_post_id"])
    if int(post.get("post_id", 0)) != post_id:
        raise MixedBatchError(f"preview/raw Chinese post ID mismatch: {post_id}")
    _same_text(preview, "chinese_title", post, "title", post_id)
    _same_text(preview, "published_at", post, "published_at", post_id)
    _same_text(preview, "permalink", post, "permalink", post_id)
    _same_text(preview, "content_sha256", post, "content_sha256", post_id)
    comparisons = {
        "editor_format": analysis["editor_format"],
        "syntaxhighlighter_count": analysis["syntaxhighlighter_count"],
        "syntaxhighlighter_balanced": analysis["syntaxhighlighter_balanced"],
        "code_block_pro_count": analysis["code_block_pro_count"],
        "mixed_code_formats": analysis["mixed_code_formats"],
    }
    for field, actual in comparisons.items():
        value = preview.get(field)
        try:
            expected = (
                _true(value) if isinstance(actual, bool) else
                int(value) if isinstance(actual, int) else value)
        except (TypeError, ValueError) as error:
            raise MixedBatchError(
                f"preview structure field is invalid: {post_id}:{field}") from error
        if expected != actual:
            raise MixedBatchError(
                f"preview/raw structure mismatch: {post_id}:{field}")


def _next_batch_sequence(repository_root, output_path):
    output = Path(output_path).resolve()
    sequences = []
    batch_ids = set()
    directory = Path(repository_root) / "data/analysis"
    for path in sorted(directory.glob(
            "mixed-syntaxhighlighter-migration-batch-*.csv")):
        if path.resolve() == output or not FIXED_BATCH_NAME.fullmatch(path.name):
            continue
        rows = _read_csv(path, {
            "batch_id", "batch_sequence", "source_type"})
        if not rows:
            raise MixedBatchError(f"existing Mixed batch is empty: {path}")
        ids = {row["batch_id"] for row in rows}
        values = {int(row["batch_sequence"]) for row in rows}
        source_types = {row["source_type"] for row in rows}
        expected_id = "mixed-syntaxhighlighter-" + FIXED_BATCH_NAME.fullmatch(
            path.name).group("suffix")
        if ids != {expected_id} or source_types != {SOURCE_TYPE}:
            raise MixedBatchError(f"existing Mixed batch contract mismatch: {path}")
        if len(values) != 1:
            raise MixedBatchError(f"existing Mixed batch sequence mismatch: {path}")
        if ids & batch_ids or next(iter(values)) in sequences:
            raise MixedBatchError("existing Mixed batch ID or sequence is duplicated")
        batch_ids.update(ids); sequences.extend(values)
    if sorted(sequences) != list(range(1, len(sequences) + 1)):
        raise MixedBatchError(
            "existing Mixed batch sequences must be unique and continuous from 1")
    return len(sequences) + 1


def select_candidates(preview_rows, posts, relations, config,
                      excluded_chinese, excluded_english, maximum):
    eligible = []
    rejected_explicit = []
    for row in preview_rows:
        post_id = int(row["chinese_post_id"])
        post = posts.get(post_id)
        relation = relations.get(post_id)
        if post is None:
            raise MixedBatchError(f"preview post missing from raw snapshot: {post_id}")
        analysis = analyze_content(post["content"], config)
        _validate_preview_snapshot(row, post, analysis)
        if not _eligible(row, post, relation, analysis):
            continue
        if post_id in EXPLICIT_ABNORMAL_IDS:
            rejected_explicit.append(post_id)
            continue
        english_id = int(row["english_post_id"])
        if post_id in excluded_chinese or english_id in excluded_english:
            continue
        eligible.append((row, post, relation, analysis))
    eligible.sort(
        key=lambda item: (item[0]["published_at"],
                          int(item[0]["chinese_post_id"])),
        reverse=True)
    return eligible[:min(maximum, len(eligible))], {
        "remaining_eligible_count": len(eligible),
        "selected_count": min(maximum, len(eligible)),
        "remaining_after_selection": max(0, len(eligible) - maximum),
        "explicit_abnormal_candidates_rejected": sorted(rejected_explicit),
    }


def _write_csv(path, rows):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise MixedBatchError(f"refusing to overwrite existing output: {target}")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise MixedBatchError(f"refusing to overwrite existing output: {target}")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_batch(preview_path, raw_paths, translations_path, output_path,
                batch_id, repository_root=ROOT, maximum=20, allocated_at=None,
                config_path=None):
    if type(maximum) is not int or maximum < 1:
        raise MixedBatchError("maximum must be a positive integer")
    match = BATCH_ID_PATTERN.fullmatch(str(batch_id))
    if match is None:
        raise MixedBatchError("batch ID must match mixed-syntaxhighlighter-YYYYMMDD-NN")
    expected_name = (
        "mixed-syntaxhighlighter-migration-batch-"
        + match.group("suffix") + ".csv"
    )
    if Path(output_path).name != expected_name:
        raise MixedBatchError("output filename and batch ID do not match")
    preview = _read_csv(preview_path, REQUIRED_PREVIEW_FIELDS)
    preview_ids = [int(row["chinese_post_id"]) for row in preview]
    if len(preview_ids) != len(set(preview_ids)):
        raise MixedBatchError("preview contains duplicate Chinese post IDs")
    posts, snapshot_id, snapshot_generated_at = _load_posts(raw_paths)
    relations = _load_jsonl(translations_path, "post_id")
    excluded_chinese, excluded_english = _historical_exclusions(repository_root)
    config = json.loads(
        Path(config_path or ROOT / "config/classification.json").read_text(
            encoding="utf-8"))
    selected, stats = select_candidates(
        preview, posts, relations, config,
        excluded_chinese, excluded_english, maximum)
    if not selected:
        return [], {**stats, "snapshot_id": snapshot_id,
                    "snapshot_generated_at": snapshot_generated_at}

    sequence = _next_batch_sequence(repository_root, output_path)
    timestamp = allocated_at or datetime.now(timezone.utc).isoformat()
    rows = []
    for preview_row, post, relation, analysis in selected:
        before_sh = analysis["syntaxhighlighter_count"]
        before_cbp = analysis["code_block_pro_count"]
        rows.append({
            "schema_version": 1,
            "batch_id": batch_id,
            "batch_sequence": sequence,
            "batch_expected_count": len(selected),
            "allocated_at": timestamp,
            "source_type": SOURCE_TYPE,
            "source_editor_format": SOURCE_EDITOR_FORMAT,
            "target_editor_format": TARGET_EDITOR_FORMAT,
            "source_migration_type": SOURCE_MIGRATION_TYPE,
            "snapshot_id": snapshot_id,
            "snapshot_generated_at": snapshot_generated_at,
            "chinese_post_id": int(preview_row["chinese_post_id"]),
            "english_post_id": int(relation["english_post_id"]),
            "chinese_title": post["title"],
            "published_at": post["published_at"],
            "edit_url": EDIT_URL.format(int(preview_row["chinese_post_id"])),
            "permalink": post["permalink"],
            "content_sha256": post["content_sha256"],
            "before_syntaxhighlighter_count": before_sh,
            "before_code_block_pro_count": before_cbp,
            "expected_syntaxhighlighter_count_after": 0,
            "expected_code_block_pro_count_after": before_cbp + before_sh,
            "migration_status": "pending",
            "validation_status": "not-checked",
            "validation_reasons": "",
        })
    _write_csv(output_path, rows)
    return rows, {
        **stats, "batch_sequence": sequence,
        "snapshot_id": snapshot_id,
        "snapshot_generated_at": snapshot_generated_at,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build one Mixed Gutenberg + SyntaxHighlighter fixed batch")
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--maximum", type=int, default=20)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("raw", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        rows, stats = build_batch(
            args.preview, args.raw, args.translations, args.output,
            args.batch_id, args.repo_root, args.maximum,
            config_path=args.config)
    except MixedBatchError as error:
        parser.error(str(error))
    if rows:
        print(f"Batch written: {args.output}")
    else:
        print("No batch written: no eligible unallocated candidates remain.")
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
