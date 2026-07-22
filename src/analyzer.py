"""Public entry point for local content analysis."""

from .classifier import classify
from .detectors import detect_content
from .risk import assess_risk


def analyze_content(content: str, config: dict) -> dict:
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if not isinstance(config, dict):
        raise TypeError("config must be a dictionary")
    detection = detect_content(content, config)
    classification = classify(detection)
    risk = assess_risk(detection, classification, config)
    syntaxhighlighter = detection["blocks"]["syntaxhighlighter"]
    return {
        "matched_rule_ids": detection["matched_rule_ids"],
        "rule_counts": detection["rule_counts"],
        "evidence": detection["evidence"],
        "editor_format": classification["editor_format"],
        "code_format": classification["code_format"],
        "primary_format": classification["primary_format"],
        "risk_level": risk["risk_level"],
        "risk_reasons": risk["risk_reasons"],
        "blocks": detection["blocks"],
        "shortcodes": detection["shortcodes"],
        "metrics": detection["metrics"],
        "code_format_families": detection["code_format_families"],
        "syntaxhighlighter_count": syntaxhighlighter["count"],
        "syntaxhighlighter_languages": syntaxhighlighter["languages"],
        "syntaxhighlighter_balanced": syntaxhighlighter["balanced"],
        "syntaxhighlighter_attributes_valid": syntaxhighlighter["attributes_valid"],
        "code_block_pro_count": detection["blocks"]["counts"].get(
            "kevinbatdorf/code-block-pro", 0
        ),
        "mixed_code_formats": len(detection["code_format_families"]) > 1,
    }
