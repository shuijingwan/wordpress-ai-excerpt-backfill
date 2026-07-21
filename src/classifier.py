"""Classification of detector facts into the three documented dimensions."""


def classify(detection):
    blocks = detection["blocks"]
    if detection["structure_damaged"]:
        editor_format = "unknown"
    elif blocks["has_block_comments"] and detection["classic_outside_blocks"]:
        editor_format = "mixed"
    elif blocks["has_block_comments"]:
        editor_format = "gutenberg"
    else:
        editor_format = "classic"

    families = detection["code_format_families"]
    if len(families) > 1:
        code_format = "mixed"
    elif detection.get("code_format_unknown", False):
        code_format = "unknown"
    elif len(families) == 1:
        code_format = families[0]
    else:
        code_format = "none"

    if editor_format == "mixed" or code_format == "mixed":
        primary_format = "mixed"
    elif editor_format == "unknown" or code_format == "unknown":
        primary_format = "unknown"
    elif editor_format == "classic" and code_format == "syntaxhighlighter":
        primary_format = "classic+syntaxhighlighter"
    elif editor_format == "classic" and code_format in {"none", "classic-pre-code"}:
        primary_format = "classic/plain"
    elif editor_format == "gutenberg" and code_format == "syntaxhighlighter":
        primary_format = "gutenberg+syntaxhighlighter"
    elif editor_format == "gutenberg" and code_format == "code-block-pro":
        primary_format = "gutenberg+code-block-pro"
    elif editor_format == "gutenberg" and code_format in {"none", "core-code", "classic-pre-code"}:
        primary_format = "gutenberg/plain"
    else:
        primary_format = "unknown"

    return {
        "editor_format": editor_format,
        "code_format": code_format,
        "primary_format": primary_format,
    }
