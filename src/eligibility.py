"""Phase-one eligibility for confirmed Gutenberg + Code Block Pro posts."""


ELIGIBLE_STATUS = "eligible-gutenberg-code-block-pro"

SYNTAXHIGHLIGHTER_RULES = {
    "SH_SHORTCODE",
    "SH_BRUSH_MARKER",
    "SH_HTML_CLASS",
    "SH_DAMAGED",
    "SH_GUTENBERG_BLOCK",
    "SH_ATTRIBUTES_INVALID",
}

DAMAGED_STRUCTURE_RULES = {
    "GB_BLOCK_DAMAGED",
    "CODE_STRUCTURE_DAMAGED",
    "SC_UNCLOSED",
    "SC_ORPHAN_CLOSE",
    "SH_DAMAGED",
    "SH_ATTRIBUTES_INVALID",
}

CODE_BLOCK_PRO_CONFIRMATION_RULES = {
    "CBP_BLOCK_COMMENT",
    "CBP_BLOCK_CLASS",
}

CODE_BLOCK_PRO_COMPLETE_RULES = {
    "CBP_BLOCK_COMMENT",
    "CBP_BLOCK_CLASS",
    "CBP_SHIKI_STRUCTURE",
}


def _is_unknown_language_value(value):
    return value is None or (isinstance(value, str) and value.strip().lower() in {"", "unknown"})


def _matched_rules(analysis):
    value = analysis.get("matched_rule_ids", [])
    if not isinstance(value, (list, tuple, set)):
        raise TypeError("analysis['matched_rule_ids'] must be a list, tuple, or set")
    if any(not isinstance(rule_id, str) for rule_id in value):
        raise TypeError("every analysis['matched_rule_ids'] item must be a string")
    return set(value)


def evaluate_phase1_eligibility(post: dict, analysis: dict) -> dict:
    """Return all deterministic phase-one exclusion reasons without mutating inputs."""
    if not isinstance(post, dict):
        raise TypeError("post must be a dict")
    if not isinstance(analysis, dict):
        raise TypeError("analysis must be a dict")

    rules = _matched_rules(analysis)
    reasons = set()

    if post.get("post_type") != "post" or post.get("post_status") != "publish":
        reasons.add("EXCLUDE_NOT_PUBLISHED_POST")

    language_source = post.get("language_source")
    language = post.get("language")
    if _is_unknown_language_value(language_source) or _is_unknown_language_value(language):
        reasons.add("EXCLUDE_LANGUAGE_UNKNOWN")
    elif language_source != "polylang" or language != "zh":
        reasons.add("EXCLUDE_NOT_POLYLANG_ZH")

    editor_format = analysis.get("editor_format", "unknown")
    code_format = analysis.get("code_format", "unknown")
    primary_format = analysis.get("primary_format", "unknown")

    if editor_format != "gutenberg":
        reasons.add("EXCLUDE_NOT_GUTENBERG")
    if code_format != "code-block-pro" or not (rules & CODE_BLOCK_PRO_CONFIRMATION_RULES):
        reasons.add("EXCLUDE_NO_CODE_BLOCK_PRO")
    if code_format == "code-block-pro" and not CODE_BLOCK_PRO_COMPLETE_RULES <= rules:
        reasons.add("EXCLUDE_CODE_BLOCK_PRO_DAMAGED")
    if rules & SYNTAXHIGHLIGHTER_RULES:
        reasons.add("EXCLUDE_SYNTAXHIGHLIGHTER")
    if editor_format == "mixed":
        reasons.add("EXCLUDE_MIXED_EDITOR_FORMAT")
    if code_format == "mixed":
        reasons.add("EXCLUDE_MIXED_CODE_FORMAT")
    if "unknown" in {editor_format, code_format, primary_format}:
        reasons.add("EXCLUDE_FORMAT_UNKNOWN")
    if rules & DAMAGED_STRUCTURE_RULES:
        reasons.add("EXCLUDE_DAMAGED_STRUCTURE")

    risk_level = analysis.get("risk_level", "manual-review")
    if risk_level == "manual-review" or "risk_level" not in analysis:
        reasons.add("EXCLUDE_MANUAL_REVIEW")

    expected_primary = "gutenberg+code-block-pro"
    if (editor_format == "gutenberg" and code_format == "code-block-pro" and
            primary_format not in {expected_primary, "unknown"}):
        # The documented reasons describe the dimensions that caused the mismatch.
        # A contradictory primary value must still be unsafe even if dimensions look eligible.
        reasons.add("EXCLUDE_FORMAT_UNKNOWN")

    exclusion_reasons = sorted(reasons)
    eligible = not exclusion_reasons
    return {
        "phase": "phase-1",
        "status": ELIGIBLE_STATUS if eligible else "excluded",
        "eligible": eligible,
        "exclusion_reasons": exclusion_reasons,
    }
