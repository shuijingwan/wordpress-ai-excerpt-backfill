"""Explainable risk reasons and deterministic risk-level precedence."""


MANUAL_REASONS = {"RISK_DAMAGED_STRUCTURE", "RISK_FORMAT_UNKNOWN", "RISK_LANGUAGE_UNKNOWN"}
HIGH_REASONS = {
    "RISK_MIXED_EDITOR_FORMAT", "RISK_MIXED_CODE_FORMAT", "RISK_UNKNOWN_BLOCK",
    "RISK_UNKNOWN_SHORTCODE", "RISK_LARGE_TABLE", "RISK_PROTECTED_STRUCTURE_HEAVY",
}
MEDIUM_REASONS = {
    "RISK_RAW_HTML", "RISK_IFRAME_OR_EMBED", "RISK_MANY_LINKS",
    "RISK_MANY_IMAGES", "RISK_VERY_LONG_CONTENT",
}


def assess_risk(detection, classification, config):
    rules = set(detection["matched_rule_ids"])
    metrics = detection["metrics"]
    thresholds = config["risk_thresholds"]
    reasons = set()

    if rules & {"GB_BLOCK_DAMAGED", "CODE_STRUCTURE_DAMAGED", "SC_UNCLOSED", "SC_ORPHAN_CLOSE"}:
        reasons.add("RISK_DAMAGED_STRUCTURE")
    if classification["editor_format"] == "mixed":
        reasons.add("RISK_MIXED_EDITOR_FORMAT")
    if classification["code_format"] == "mixed":
        reasons.add("RISK_MIXED_CODE_FORMAT")
    if rules & {"GB_UNKNOWN_BLOCK", "GB_INACTIVE_BLOCK"}:
        reasons.add("RISK_UNKNOWN_BLOCK")
    if "SC_UNKNOWN" in rules:
        reasons.add("RISK_UNKNOWN_SHORTCODE")
    if "RAW_HTML_PRESENT" in rules:
        reasons.add("RISK_RAW_HTML")
    if metrics["iframe_count"] or metrics["embed_count"]:
        reasons.add("RISK_IFRAME_OR_EMBED")
    if "STRUCT_LARGE_TABLE" in rules:
        reasons.add("RISK_LARGE_TABLE")
    if metrics["link_count"] >= thresholds["many_links"]:
        reasons.add("RISK_MANY_LINKS")
    if metrics["image_count"] >= thresholds["many_images"]:
        reasons.add("RISK_MANY_IMAGES")
    if metrics["content_character_count"] >= thresholds["very_long_content"]:
        reasons.add("RISK_VERY_LONG_CONTENT")
    if "PROTECTED_STRUCTURE_HEAVY" in rules:
        reasons.add("RISK_PROTECTED_STRUCTURE_HEAVY")
    if "unknown" in {classification["editor_format"], classification["code_format"], classification["primary_format"]}:
        reasons.add("RISK_FORMAT_UNKNOWN")
    if "LANGUAGE_UNKNOWN" in rules:
        reasons.add("RISK_LANGUAGE_UNKNOWN")

    if reasons & MANUAL_REASONS:
        level = "manual-review"
    elif reasons & HIGH_REASONS or len(reasons & MEDIUM_REASONS) >= 3:
        level = "high"
    elif reasons & MEDIUM_REASONS:
        level = "medium"
    else:
        level = "low"
    return {"risk_level": level, "risk_reasons": sorted(reasons)}
