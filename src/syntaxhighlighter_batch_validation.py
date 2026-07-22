"""Read-only validation for one fixed SyntaxHighlighter migration batch."""

import csv
from datetime import datetime, timezone
from html import unescape
import json
import os
from pathlib import Path
import re
import tempfile

from src.analyzer import analyze_content
from src.candidate_execution import SafetyError, sha256_text
from src.detectors import BLOCK_COMMENT_RE, _block_name
from src.single_candidate_flow import raw_field


VALIDATION_FIELDS = (
    "schema_version", "batch_id", "batch_sequence", "validated_at",
    "chinese_post_id", "english_post_id", "chinese_title",
    "before_content_sha256", "after_content_sha256",
    "before_syntaxhighlighter_count", "after_syntaxhighlighter_count",
    "before_code_block_pro_count", "expected_code_block_pro_count_after",
    "after_code_block_pro_count", "code_block_pro_languages",
    "chinese_excerpt_empty", "chinese_status", "chinese_language",
    "english_status", "polylang_relation_status", "gutenberg_balanced",
    "validation_status", "validation_reasons",
)
REQUIRED_BATCH_FIELDS = {
    "batch_id", "batch_sequence", "chinese_post_id", "english_post_id",
    "chinese_title", "before_content_sha256", "before_syntaxhighlighter_count",
    "before_code_block_pro_count", "expected_code_block_pro_count_after",
}
TEXTAREA_RE = re.compile(
    r"<textarea\b[^>]*>(?P<code>.*?)</textarea\s*>", re.IGNORECASE | re.DOTALL
)


def load_batch(path, expected_count):
    if type(expected_count) is not int or expected_count < 1:
        raise SafetyError("expected_count must be a positive integer")
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_BATCH_FIELDS - set(reader.fieldnames or ())
        if missing:
            raise SafetyError("batch missing fields: " + ",".join(sorted(missing)))
        rows = list(reader)
    if len(rows) != expected_count:
        raise SafetyError(f"batch count must be exactly {expected_count}, got {len(rows)}")
    chinese = [_positive_id(row, "chinese_post_id") for row in rows]
    english = [_positive_id(row, "english_post_id") for row in rows]
    if len(set(chinese)) != len(chinese):
        raise SafetyError("batch Chinese post IDs must be unique")
    if len(set(english)) != len(english):
        raise SafetyError("batch English post IDs must be unique")
    batch_ids = {row["batch_id"] for row in rows}
    sequences = {row["batch_sequence"] for row in rows}
    if len(batch_ids) != 1 or "" in batch_ids:
        raise SafetyError("batch ID must be nonempty and consistent")
    if len(sequences) != 1:
        raise SafetyError("batch sequence must be consistent")
    return rows


def _positive_id(row, field):
    try:
        value = int(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise SafetyError(f"invalid {field}") from error
    if value < 1:
        raise SafetyError(f"invalid {field}")
    return value


def inspect_code_block_pro(content):
    """Return languages, code presence, and attribute integrity for paired CBP blocks."""
    stack = []
    details = []
    damaged = False
    for match in BLOCK_COMMENT_RE.finditer(content):
        name = _block_name(match.group("name"))
        if match.group("close"):
            if not stack or stack[-1][0] != name:
                continue
            opening = stack.pop()
            if name != "kevinbatdorf/code-block-pro":
                continue
            attrs_valid = True
            raw_attrs = (opening[3] or "").strip()
            attributes = {}
            if raw_attrs:
                try:
                    attributes = json.loads(raw_attrs)
                except json.JSONDecodeError:
                    attributes = {}
                    attrs_valid = False
                if not isinstance(attributes, dict):
                    attributes = {}
                    attrs_valid = False
            language = attributes.get("language")
            language_valid = language is None or isinstance(language, str)
            if not language_valid:
                language = None
            code = attributes.get("code")
            if not isinstance(code, str):
                inner = content[opening[2]:match.start()]
                textarea = TEXTAREA_RE.search(inner)
                code = unescape(textarea.group("code")) if textarea else ""
            details.append({
                "language": language.strip() if isinstance(language, str) else "",
                "code_nonempty": bool(code.strip()),
                "attributes_valid": attrs_valid,
                "language_valid": language_valid,
            })
        elif match.group("self"):
            if name == "kevinbatdorf/code-block-pro":
                damaged = True
        else:
            stack.append((name, match.start(), match.end(), match.group("attrs")))
    if any(item[0] == "kevinbatdorf/code-block-pro" for item in stack):
        damaged = True
    return {"blocks": details, "damaged": damaged}


def validate_one(row, chinese, english, polylang, config, validated_at):
    zh_id = _positive_id(row, "chinese_post_id")
    en_id = _positive_id(row, "english_post_id")
    content = raw_field(chinese, "content")
    analysis = analyze_content(content, config)
    cbp = inspect_code_block_pro(content)
    counts = analysis["blocks"]["counts"]
    after_sh = analysis["syntaxhighlighter_count"]
    after_cbp = counts.get("kevinbatdorf/code-block-pro", 0)
    after_hash = sha256_text(content)
    expected_cbp = int(row["expected_code_block_pro_count_after"])
    relation_ok = bool(
        isinstance(polylang, dict)
        and polylang.get("chinese_post_id") == zh_id
        and polylang.get("chinese_language") == "zh"
        and polylang.get("linked_english_post_id") == en_id
        and polylang.get("english_post_id") == en_id
        and polylang.get("english_language") == "en"
        and polylang.get("linked_chinese_post_id") == zh_id
    )

    pending = []
    abnormal = []
    if after_sh:
        pending.append("syntaxhighlighter-remains")
    if after_hash == row["before_content_sha256"]:
        pending.append("content-hash-unchanged")
    checks = (
        (chinese.get("id") == zh_id, "chinese-missing-or-id-mismatch"),
        (chinese.get("status") == "publish", "chinese-not-publish"),
        (raw_field(chinese, "title") == row["chinese_title"], "chinese-title-changed"),
        (not raw_field(chinese, "excerpt").strip(), "chinese-excerpt-not-empty"),
        (polylang.get("chinese_language") == "zh" if isinstance(polylang, dict) else False,
         "chinese-language-not-zh"),
        (english.get("id") == en_id, "english-missing-or-id-mismatch"),
        (english.get("status") == "publish", "english-not-publish"),
        (relation_ok, "polylang-relation-abnormal"),
        (analysis["blocks"]["has_block_comments"], "not-gutenberg"),
        (analysis["blocks"]["balanced"], "gutenberg-unbalanced"),
        (after_cbp == expected_cbp, "code-block-pro-count-mismatch"),
        (len(cbp["blocks"]) == after_cbp and not cbp["damaged"], "code-block-pro-unparseable"),
        (all(item["code_nonempty"] for item in cbp["blocks"]), "code-block-pro-empty"),
        (all(item["attributes_valid"] and item["language_valid"] for item in cbp["blocks"]),
         "code-block-pro-attributes-invalid"),
    )
    abnormal.extend(reason for passed, reason in checks if not passed)
    other_formats = set(analysis["code_format_families"]) - {
        "code-block-pro", "syntaxhighlighter"
    }
    if other_formats:
        abnormal.append("unexpected-code-format:" + "|".join(sorted(other_formats)))
    status = "abnormal" if abnormal else ("pending" if pending else "ready")
    reasons = abnormal + pending
    languages = [item["language"] or "(unset)" for item in cbp["blocks"]]
    return {
        "schema_version": 1, "batch_id": row["batch_id"],
        "batch_sequence": int(row["batch_sequence"]), "validated_at": validated_at,
        "chinese_post_id": zh_id, "english_post_id": en_id,
        "chinese_title": row["chinese_title"],
        "before_content_sha256": row["before_content_sha256"],
        "after_content_sha256": after_hash,
        "before_syntaxhighlighter_count": int(row["before_syntaxhighlighter_count"]),
        "after_syntaxhighlighter_count": after_sh,
        "before_code_block_pro_count": int(row["before_code_block_pro_count"]),
        "expected_code_block_pro_count_after": expected_cbp,
        "after_code_block_pro_count": after_cbp,
        "code_block_pro_languages": "|".join(languages),
        "chinese_excerpt_empty": not raw_field(chinese, "excerpt").strip(),
        "chinese_status": chinese.get("status"),
        "chinese_language": polylang.get("chinese_language") if isinstance(polylang, dict) else None,
        "english_status": english.get("status"),
        "polylang_relation_status": "normal" if relation_ok else "abnormal",
        "gutenberg_balanced": analysis["blocks"]["balanced"],
        "validation_status": status, "validation_reasons": "|".join(reasons),
    }


def validate_batch(rows, wp, polylang_checker, config, validated_at=None):
    timestamp = validated_at or datetime.now(timezone.utc).isoformat()
    results = []
    for row in rows:
        zh_id = _positive_id(row, "chinese_post_id")
        en_id = _positive_id(row, "english_post_id")
        chinese = wp.get_post(zh_id)
        english = wp.get_post(en_id)
        try:
            polylang = polylang_checker.check(zh_id, en_id)
        except SafetyError:
            polylang = None
        results.append(validate_one(row, chinese, english, polylang, config, timestamp))
    return results


def write_outputs(rows, output_path, snapshot_path):
    output = Path(output_path)
    snapshot = Path(snapshot_path)
    if output.resolve(strict=False) == snapshot.resolve(strict=False):
        raise SafetyError("validation output and live snapshot must be different paths")
    if output.exists() or snapshot.exists():
        raise SafetyError("refusing to overwrite validation output or live snapshot")
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    csv_temp = _temporary(output)
    json_temp = _temporary(snapshot)
    try:
        with os.fdopen(csv_temp[0], "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=VALIDATION_FIELDS)
            writer.writeheader(); writer.writerows(rows)
            handle.flush(); os.fsync(handle.fileno())
        with os.fdopen(json_temp[0], "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush(); os.fsync(handle.fileno())
        if output.exists() or snapshot.exists():
            raise SafetyError("refusing to overwrite validation output or live snapshot")
        os.replace(json_temp[1], snapshot)
        os.replace(csv_temp[1], output)
    finally:
        Path(csv_temp[1]).unlink(missing_ok=True)
        Path(json_temp[1]).unlink(missing_ok=True)


def _temporary(path):
    return tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
