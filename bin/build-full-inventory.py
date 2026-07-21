#!/usr/bin/env python3
"""Build privacy-reduced CSV and summary reports from bounded raw exports."""

from collections import Counter
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzer import analyze_content  # noqa: E402
from src.eligibility import evaluate_phase1_eligibility  # noqa: E402


REQUIRED_RAW_FIELDS = {
    "schema_version", "post_id", "post_type", "post_status", "title",
    "published_at", "modified_at", "language_source", "language", "excerpt",
    "content", "content_sha256",
}
REQUIRED_TRANSLATION_FIELDS = {
    "schema_version", "post_id", "has_english_translation",
    "english_post_id", "english_post_status",
}
CSV_FIELDS = [
    "post_id", "title", "published_at", "modified_at", "content_characters",
    "excerpt_empty", "is_gutenberg", "has_code_block_pro",
    "code_block_pro_count", "has_core_code", "has_shortcode",
    "has_damaged_blocks", "has_unparseable_blocks", "has_english_translation", "english_post_id",
    "english_post_status", "editor_format", "code_format", "primary_format",
    "risk_level", "manual_review", "phase1_status", "phase1_eligible",
    "translation_replacement_candidate", "category",
]


class InventoryError(ValueError):
    """Raised before formal outputs replace any existing reports."""


def _load_json_object(line, source, line_number):
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise InventoryError(f"{source}:{line_number}: invalid JSON") from error
    if not isinstance(value, dict):
        raise InventoryError(f"{source}:{line_number}: JSON value must be an object")
    return value


def load_translations(path):
    translations = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise InventoryError(f"{path}:{line_number}: blank JSONL record")
            record = _load_json_object(line, path, line_number)
            missing = REQUIRED_TRANSLATION_FIELDS - record.keys()
            if missing or record["schema_version"] != 1:
                raise InventoryError(f"{path}:{line_number}: invalid translation record")
            post_id = record["post_id"]
            if not isinstance(post_id, int) or post_id in translations:
                raise InventoryError(f"{path}:{line_number}: invalid or duplicate post_id")
            linked = record["has_english_translation"]
            if not isinstance(linked, bool):
                raise InventoryError(f"{path}:{line_number}: invalid translation flag")
            if linked:
                if not isinstance(record["english_post_id"], int) or not isinstance(record["english_post_status"], str):
                    raise InventoryError(f"{path}:{line_number}: incomplete English relation")
            elif record["english_post_id"] is not None or record["english_post_status"] is not None:
                raise InventoryError(f"{path}:{line_number}: contradictory English relation")
            translations[post_id] = record
    return translations


def _validate_raw_record(record, source, line_number, seen_post_ids):
    missing = REQUIRED_RAW_FIELDS - record.keys()
    if missing:
        raise InventoryError(f"{source}:{line_number}: missing required fields")
    if record["schema_version"] != 1:
        raise InventoryError(f"{source}:{line_number}: schema_version must be 1")
    if (record["post_type"], record["post_status"]) != ("post", "publish"):
        raise InventoryError(f"{source}:{line_number}: record is not a published post")
    if (record["language_source"], record["language"]) != ("polylang", "zh"):
        raise InventoryError(f"{source}:{line_number}: record is not confirmed Polylang Chinese")
    post_id = record["post_id"]
    if not isinstance(post_id, int) or post_id in seen_post_ids:
        raise InventoryError(f"{source}:{line_number}: invalid or duplicate post_id")
    if not isinstance(record["content"], str) or not isinstance(record["content_sha256"], str):
        raise InventoryError(f"{source}:{line_number}: invalid content fields")
    actual_hash = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
    if actual_hash != record["content_sha256"]:
        raise InventoryError(f"{source}:{line_number}: content_sha256 mismatch")
    seen_post_ids.add(post_id)


def _category(analysis, eligibility):
    rules = set(analysis["matched_rule_ids"])
    anomalous = (
        analysis["editor_format"] in {"mixed", "unknown"}
        or analysis["primary_format"] in {"mixed", "unknown"}
        or "GB_BLOCK_DAMAGED" in rules
        or "EXCLUDE_CODE_BLOCK_PRO_DAMAGED" in eligibility["exclusion_reasons"]
    )
    if anomalous:
        return "mixed-or-anomalous"
    if analysis["editor_format"] == "gutenberg" and "CBP_BLOCK_COMMENT" in rules:
        return "gutenberg-code-block-pro"
    if analysis["editor_format"] == "gutenberg":
        return "gutenberg-without-code-block-pro"
    return "non-gutenberg"


def analyze_record(record, translation, config):
    analysis = analyze_content(record["content"], config)
    eligibility = evaluate_phase1_eligibility(record, analysis)
    block_counts = analysis["blocks"]["counts"]
    rules = set(analysis["matched_rule_ids"])
    linked = translation["has_english_translation"]
    replacement_candidate = (
        eligibility["eligible"]
        and linked
        and translation["english_post_status"] == "publish"
    )
    return {
        "post_id": record["post_id"],
        "title": record["title"],
        "published_at": record["published_at"],
        "modified_at": record["modified_at"],
        "content_characters": len(record["content"]),
        "excerpt_empty": not record["excerpt"].strip(),
        "is_gutenberg": analysis["blocks"]["has_block_comments"],
        "has_code_block_pro": block_counts.get("kevinbatdorf/code-block-pro", 0) > 0,
        "code_block_pro_count": block_counts.get("kevinbatdorf/code-block-pro", 0),
        "has_core_code": block_counts.get("core/code", 0) > 0,
        "has_shortcode": analysis["shortcodes"]["total_count"] > 0,
        "has_damaged_blocks": analysis["blocks"]["damaged"] or "GB_BLOCK_DAMAGED" in rules,
        "has_unparseable_blocks": analysis["blocks"]["damaged"] or bool(
            rules & {"GB_BLOCK_DAMAGED", "GB_UNKNOWN_BLOCK"}
        ),
        "has_english_translation": linked,
        "english_post_id": translation["english_post_id"],
        "english_post_status": translation["english_post_status"],
        "editor_format": analysis["editor_format"],
        "code_format": analysis["code_format"],
        "primary_format": analysis["primary_format"],
        "risk_level": analysis["risk_level"],
        "manual_review": analysis["risk_level"] == "manual-review",
        "phase1_status": eligibility["status"],
        "phase1_eligible": eligibility["eligible"],
        "translation_replacement_candidate": replacement_candidate,
        "category": _category(analysis, eligibility),
    }


def _temporary_path(final_path):
    final_path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=final_path.parent, prefix=f".{final_path.name}.", suffix=".tmp", delete=False,
    )
    path = Path(handle.name)
    handle.close()
    return path


def _atomic_text(path, text):
    temporary = _temporary_path(path)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_inventory(raw_paths, translations_path, output_prefix, config_path=None):
    config_file = Path(config_path) if config_path else PROJECT_ROOT / "config/classification.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    translations = load_translations(translations_path)
    prefix = Path(output_prefix)
    csv_path = Path(str(prefix) + ".csv")
    summary_path = Path(str(prefix) + ".summary.json")
    markdown_path = Path(str(prefix) + ".md")
    csv_temporary = _temporary_path(csv_path)
    seen_post_ids = set()
    counts = Counter()
    try:
        with csv_temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for raw_path in raw_paths:
                with Path(raw_path).open(encoding="utf-8") as source:
                    for line_number, line in enumerate(source, 1):
                        if not line.strip():
                            raise InventoryError(f"{raw_path}:{line_number}: blank JSONL record")
                        record = _load_json_object(line, raw_path, line_number)
                        _validate_raw_record(record, raw_path, line_number, seen_post_ids)
                        translation = translations.get(record["post_id"])
                        if translation is None:
                            raise InventoryError(f"{raw_path}:{line_number}: missing translation relation")
                        row = analyze_record(record, translation, config)
                        writer.writerow(row)
                        counts["total"] += 1
                        counts[f"category:{row['category']}"] += 1
                        if row["excerpt_empty"] and row["category"] == "gutenberg-code-block-pro":
                            counts["excerpt_empty_gutenberg_code_block_pro"] += 1
                        for field in (
                            "excerpt_empty", "has_english_translation", "phase1_eligible",
                            "translation_replacement_candidate", "manual_review",
                        ):
                            counts[field] += bool(row[field])
            extra_translations = set(translations) - seen_post_ids
            if extra_translations:
                raise InventoryError("translation export contains post IDs absent from raw exports")
            handle.flush()
            os.fsync(handle.fileno())

        summary = {
            "schema_version": 1,
            "total_posts": counts["total"],
            "categories": {
                name: counts[f"category:{name}"] for name in (
                    "gutenberg-code-block-pro", "gutenberg-without-code-block-pro",
                    "non-gutenberg", "mixed-or-anomalous",
                )
            },
            "excerpt_empty": counts["excerpt_empty"],
            "excerpt_empty_gutenberg_code_block_pro": counts["excerpt_empty_gutenberg_code_block_pro"],
            "has_english_translation": counts["has_english_translation"],
            "no_english_translation": counts["total"] - counts["has_english_translation"],
            "phase1_eligible": counts["phase1_eligible"],
            "translation_replacement_candidates": counts["translation_replacement_candidate"],
            "manual_review": counts["manual_review"],
        }
        markdown = "\n".join([
            "# WordPress 中文已发布文章格式盘点",
            "",
            f"- 文章总数：{summary['total_posts']}",
            f"- Gutenberg + Code Block Pro：{summary['categories']['gutenberg-code-block-pro']}",
            f"- Gutenberg，不含 Code Block Pro：{summary['categories']['gutenberg-without-code-block-pro']}",
            f"- 非 Gutenberg：{summary['categories']['non-gutenberg']}",
            f"- mixed 或异常格式：{summary['categories']['mixed-or-anomalous']}",
            f"- 摘要为空：{summary['excerpt_empty']}",
            f"- 摘要为空且属于 Gutenberg + Code Block Pro：{summary['excerpt_empty_gutenberg_code_block_pro']}",
            f"- 已有关联英文文章：{summary['has_english_translation']}",
            f"- 没有关联英文文章：{summary['no_english_translation']}",
            f"- 第一阶段结构合格：{summary['phase1_eligible']}",
            f"- 有已发布英文关联的第一阶段结构候选：{summary['translation_replacement_candidates']}",
            f"- 需要人工复核：{summary['manual_review']}",
            "",
            "> 本报告仅来自只读导出和确定性本地分析，不代表已经授权或执行摘要、翻译或 WordPress 写入。",
            "",
        ])
        _atomic_text(summary_path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        _atomic_text(markdown_path, markdown)
        os.replace(csv_temporary, csv_path)
        return summary, (csv_path, summary_path, markdown_path)
    finally:
        csv_temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a full read-only WordPress format inventory")
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("raw", nargs="+", type=Path)
    args = parser.parse_args(argv)
    summary, paths = build_inventory(args.raw, args.translations, args.output_prefix, args.config)
    print(f"Input records: {summary['total_posts']}")
    for path in paths:
        print(f"Output: {path}")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
