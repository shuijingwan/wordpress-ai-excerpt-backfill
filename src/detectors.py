"""Deterministic detectors for WordPress post_content."""

from collections import Counter
from html.parser import HTMLParser
import json
import re


BLOCK_COMMENT_RE = re.compile(
    r"<!--\s*(?P<close>/)?wp:(?P<name>[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)?)"
    r"(?P<attrs>\s+.*?)?\s*(?P<self>/)?-->",
    re.DOTALL,
)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_COMMENT_CANDIDATE_RE = re.compile(r"<!--(?P<body>.*?)(?:-->|$)", re.DOTALL)
WP_COMMENT_BODY_RE = re.compile(r"^\s*/?wp:", re.IGNORECASE)
SHORTCODE_RE = re.compile(
    r"(?<!\[)(?P<close>\[/?)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)"
    r"(?P<attrs>[^\]\n]*?)(?P<self>/?)\](?!\])"
)
HTML_CODE_REGION_RE = re.compile(
    r"<(?P<tag>pre|code)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
TEXTAREA_REGION_RE = re.compile(
    r"<textarea\b[^>]*>.*?</textarea\s*>", re.IGNORECASE | re.DOTALL
)


def _block_name(name):
    lowered = name.lower()
    return lowered if "/" in lowered else f"core/{lowered}"


def _evidence(content, start, end, limit=240):
    excerpt = content[start:end]
    truncated = len(excerpt) > limit
    return {"offset": start, "excerpt": excerpt[:limit], "truncated": truncated}


class _MetricsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.counts = Counter()
        self.internal_images = 0
        self.internal_links = 0
        self._tables = []
        self.table_shapes = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = dict(attrs)
        if tag in {"img", "a", "figure", "iframe", "table"}:
            self.counts[tag] += 1
        if tag == "img" and (attributes.get("src") or "").startswith(("/", "./", "../")):
            self.internal_images += 1
        if tag == "a" and (attributes.get("href") or "").startswith(("/", "./", "../")):
            self.internal_links += 1
        if tag == "table":
            self._tables.append({"rows": 0, "current_columns": 0, "max_columns": 0, "cells": 0})
        elif self._tables and tag == "tr":
            table = self._tables[-1]
            table["rows"] += 1
            table["current_columns"] = 0
        elif self._tables and tag in {"td", "th"}:
            table = self._tables[-1]
            table["current_columns"] += 1
            table["cells"] += 1
            table["max_columns"] = max(table["max_columns"], table["current_columns"])

    def handle_endtag(self, tag):
        if tag.lower() == "table" and self._tables:
            self.table_shapes.append(self._tables.pop())

    def close(self):
        super().close()
        self.table_shapes.extend(self._tables)
        self._tables = []


class _EmptyOutsideBlocksParser(HTMLParser):
    EMPTY_WRAPPERS = {"p", "div", "span", "section"}
    EMPTY_VOID_TAGS = {"br"}
    ZERO_WIDTH_CHARACTERS = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.substantial = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.EMPTY_WRAPPERS:
            self.stack.append(tag)
        elif tag not in self.EMPTY_VOID_TAGS:
            self.substantial = True

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag not in self.EMPTY_VOID_TAGS:
            self.substantial = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in self.EMPTY_WRAPPERS or not self.stack or self.stack[-1] != tag:
            self.substantial = True
            return
        self.stack.pop()

    def handle_data(self, data):
        if any(not character.isspace() and character not in self.ZERO_WIDTH_CHARACTERS
               for character in data):
            self.substantial = True

    def handle_entityref(self, name):
        if name.lower() != "nbsp":
            self.substantial = True

    def handle_charref(self, name):
        try:
            value = int(name[1:], 16) if name.lower().startswith("x") else int(name, 10)
            character = chr(value)
        except (ValueError, OverflowError):
            self.substantial = True
            return
        if not character.isspace() and character not in self.ZERO_WIDTH_CHARACTERS:
            self.substantial = True

    def handle_comment(self, data):
        self.substantial = True

    def handle_decl(self, decl):
        self.substantial = True

    def unknown_decl(self, data):
        self.substantial = True

    def handle_pi(self, data):
        self.substantial = True

    def error(self, message):
        self.substantial = True


def _outside_blocks_has_substantial_content(content):
    parser = _EmptyOutsideBlocksParser()
    try:
        parser.feed(content)
        parser.close()
    except Exception:
        return True
    return parser.substantial or bool(parser.stack)


def _parse_blocks(content, config, add):
    textarea_ranges = [(match.start(), match.end()) for match in TEXTAREA_REGION_RE.finditer(content)]
    matches = [
        match for match in BLOCK_COMMENT_RE.finditer(content)
        if not _inside_ranges(match.start(), textarea_ranges)
    ]
    consumed_ranges = {(match.start(), match.end()) for match in matches}
    stack = []
    blocks = Counter()
    top_ranges = []
    damaged_ranges = []
    syntaxhighlighter_count = 0
    syntaxhighlighter_languages = []
    syntaxhighlighter_damaged = False
    syntaxhighlighter_attributes_valid = True

    def record_syntaxhighlighter(opening):
        nonlocal syntaxhighlighter_count, syntaxhighlighter_attributes_valid
        syntaxhighlighter_count += 1
        add("SH_GUTENBERG_BLOCK", opening[1], opening[2])
        raw_attributes = (opening[3] or "").strip()
        if not raw_attributes:
            return
        try:
            attributes = json.loads(raw_attributes)
        except (json.JSONDecodeError, TypeError):
            syntaxhighlighter_attributes_valid = False
            add("SH_ATTRIBUTES_INVALID", opening[1], opening[2])
            return
        if not isinstance(attributes, dict):
            syntaxhighlighter_attributes_valid = False
            add("SH_ATTRIBUTES_INVALID", opening[1], opening[2])
            return
        language = attributes.get("language")
        if isinstance(language, str) and language.strip():
            syntaxhighlighter_languages.append(language.strip().lower())
        elif language is not None:
            syntaxhighlighter_attributes_valid = False
            add("SH_ATTRIBUTES_INVALID", opening[1], opening[2])

    for match in matches:
        name = _block_name(match.group("name"))
        add("GB_BLOCK_COMMENT", match.start(), match.end())
        if match.group("close"):
            if not stack or stack[-1][0] != name:
                damaged_ranges.append((match.start(), match.end()))
                if name == "syntaxhighlighter/code":
                    syntaxhighlighter_damaged = True
                    add("SH_DAMAGED", match.start(), match.end())
                continue
            opening = stack.pop()
            _, start, _, _ = opening
            if name == "syntaxhighlighter/code":
                record_syntaxhighlighter(opening)
            if not stack:
                top_ranges.append((start, match.end()))
        else:
            blocks[name] += 1
            if match.group("self"):
                if name == "syntaxhighlighter/code":
                    syntaxhighlighter_damaged = True
                    add("SH_DAMAGED", match.start(), match.end())
                if not stack:
                    top_ranges.append((match.start(), match.end()))
            else:
                stack.append((name, match.start(), match.end(), match.group("attrs")))
    damaged_ranges.extend((start, end) for _, start, end, _ in stack)
    for name, start, end, _ in stack:
        if name == "syntaxhighlighter/code":
            syntaxhighlighter_damaged = True
            add("SH_DAMAGED", start, end)

    for candidate in HTML_COMMENT_CANDIDATE_RE.finditer(content):
        if ((candidate.start(), candidate.end()) in consumed_ranges
                or _inside_ranges(candidate.start(), textarea_ranges)):
            continue
        if WP_COMMENT_BODY_RE.match(candidate.group("body")):
            damaged_ranges.append((candidate.start(), candidate.end()))

    damaged = bool(damaged_ranges)
    if damaged:
        for start, end in dict.fromkeys(damaged_ranges):
            add("GB_BLOCK_DAMAGED", start, end)
    elif matches:
        add("GB_BLOCK_BALANCED", matches[0].start(), matches[-1].end())

    known_core = set(config["known_core_blocks"])
    known_third = set(config["known_third_party_blocks"])
    for name, count in blocks.items():
        first = next(match for match in matches if not match.group("close") and _block_name(match.group("name")) == name)
        if not name.startswith("core/"):
            add("GB_THIRD_PARTY_BLOCK", first.start(), first.end(), count=count)
        if name not in known_core and name not in known_third:
            add("GB_UNKNOWN_BLOCK", first.start(), first.end(), count=count)
        if name == "core/html":
            add("GB_RAW_HTML_BLOCK", first.start(), first.end(), count=count)
            add("RAW_HTML_PRESENT", first.start(), first.end(), count=count)
        if name == "core/code":
            add("CORE_CODE_BLOCK", first.start(), first.end(), count=count)
        if name == "kevinbatdorf/code-block-pro":
            add("CBP_BLOCK_COMMENT", first.start(), first.end(), count=count)

    outside = content
    for start, end in sorted(top_ranges, reverse=True):
        outside = outside[:start] + outside[end:]
    outside = HTML_COMMENT_RE.sub("", outside)
    if matches and not damaged and _outside_blocks_has_substantial_content(outside):
        add("CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS", 0, min(len(content), 240))

    return {
        "counts": dict(sorted(blocks.items())),
        "total_count": sum(blocks.values()),
        "distinct_count": len(blocks),
        "balanced": bool(matches) and not damaged,
        "damaged": damaged,
        "has_block_comments": bool(matches),
        "top_ranges": top_ranges,
        "syntaxhighlighter": {
            "count": syntaxhighlighter_count,
            "languages": sorted(set(syntaxhighlighter_languages)),
            "balanced": syntaxhighlighter_count > 0 and not syntaxhighlighter_damaged,
            "attributes_valid": syntaxhighlighter_attributes_valid,
        },
    }


def _inside_ranges(position, ranges):
    return any(start <= position < end for start, end in ranges)


def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _gutenberg_code_content_ranges(content):
    protected_names = {
        "syntaxhighlighter/code",
        "kevinbatdorf/code-block-pro",
    }
    stack = []
    ranges = []
    for match in BLOCK_COMMENT_RE.finditer(content):
        name = _block_name(match.group("name"))
        if match.group("close"):
            if stack and stack[-1][0] == name:
                opening_name, _, content_start = stack.pop()
                if opening_name in protected_names:
                    ranges.append((content_start, match.start()))
            continue
        if not match.group("self"):
            stack.append((name, match.start(), match.end()))
    return ranges


def _syntaxhighlighter_content_ranges(content, matches, known_sh):
    stack = []
    ranges = []
    for match in matches:
        name = match.group("name").lower()
        if name not in known_sh:
            continue
        if match.group("close") == "[/":
            if stack and stack[-1][0] == name:
                _, content_start = stack.pop()
                ranges.append((content_start, match.start()))
        elif not match.group("self"):
            stack.append((name, match.end()))
    ranges.extend((content_start, len(content)) for _, content_start in stack)
    return ranges


def _shortcode_protected_ranges(content, matches, known_sh):
    ranges = _gutenberg_code_content_ranges(content)
    ranges.extend((match.start(), match.end()) for match in HTML_CODE_REGION_RE.finditer(content))
    ranges.extend(_syntaxhighlighter_content_ranges(content, matches, known_sh))
    return _merge_ranges(ranges)


def _parse_shortcodes(content, config, add):
    all_matches = list(SHORTCODE_RE.finditer(content))
    known_sh = set(config["known_syntaxhighlighter_shortcodes"])
    known = known_sh | set(config.get("known_shortcodes", []))
    paired = known_sh | set(config.get("paired_shortcodes", []))
    protected_ranges = _shortcode_protected_ranges(content, all_matches, known_sh)
    matches = [
        match for match in all_matches
        if match.group("name").lower() in known_sh
        or not _inside_ranges(match.start(), protected_ranges)
    ]
    opening_names = {
        match.group("name").lower()
        for match in matches
        if match.group("close") != "[/"
    }
    closing_names = {
        match.group("name").lower()
        for match in matches
        if match.group("close") == "[/"
    }
    unknown_paired_names = (opening_names & closing_names) - known
    counts = Counter()
    stack = []
    damaged = False
    for match in matches:
        name = match.group("name").lower()
        closing = match.group("close") == "[/"
        if closing:
            if name not in paired:
                continue
            if stack and stack[-1]["name"] == name:
                opening = stack.pop()
                if name in known_sh:
                    add("SH_SHORTCODE", opening["start"], opening["end"])
            else:
                damaged = True
                add("SC_ORPHAN_CLOSE", match.start(), match.end())
                if name in known_sh:
                    add("SH_DAMAGED", match.start(), match.end())
            continue

        is_known = name in known
        has_attribute_assignment = bool(re.search(r"\b[A-Za-z_][A-Za-z0-9_:-]*\s*=", match.group("attrs")))
        has_unknown_shortcode_evidence = (
            name in unknown_paired_names or has_attribute_assignment or bool(match.group("self"))
        )
        if not is_known and not has_unknown_shortcode_evidence:
            continue

        counts[name] += 1
        add("SC_KNOWN" if is_known else "SC_UNKNOWN", match.start(), match.end())
        if name in known_sh and match.group("self"):
            add("SH_SHORTCODE", match.start(), match.end())
        elif name in paired and not match.group("self"):
            stack.append({"name": name, "start": match.start(), "end": match.end()})

    for opening in stack:
        damaged = True
        add("SC_UNCLOSED", opening["start"], opening["end"])
        if opening["name"] in known_sh:
            add("SH_DAMAGED", opening["start"], opening["end"])

    return {
        "counts": dict(sorted(counts.items())),
        "total_count": sum(counts.values()),
        "damaged": damaged,
    }


def detect_content(content, config):
    """Return deterministic facts, rules, counts, metrics, and bounded evidence."""
    rule_counts = Counter()
    evidence = {}

    def add(rule_id, start, end, count=1):
        rule_counts[rule_id] += count
        bucket = evidence.setdefault(rule_id, [])
        if len(bucket) < 5:
            bucket.append(_evidence(content, start, end))

    blocks = _parse_blocks(content, config, add)
    shortcodes = _parse_shortcodes(content, config, add)

    cbp_class = re.search(r'class\s*=\s*["\'][^"\']*\bwp-block-kevinbatdorf-code-block-pro\b', content, re.I)
    if cbp_class:
        add("CBP_BLOCK_CLASS", cbp_class.start(), cbp_class.end())
    shiki = re.search(r'<pre\b[^>]*class\s*=\s*["\'][^"\']*\bshiki\b[^"\']*["\'][^>]*>.*?<code\b[^>]*>.*?<span\b[^>]*class\s*=\s*["\'][^"\']*\bline\b', content, re.I | re.S)
    if shiki and (cbp_class or "kevinbatdorf/code-block-pro" in blocks["counts"]):
        add("CBP_SHIKI_STRUCTURE", shiki.start(), shiki.end())

    pre_code_matches = list(re.finditer(r"<(?:pre|code)\b", content, re.I))
    ordinary_pre_code = [m for m in pre_code_matches if not _inside_ranges(m.start(), blocks["top_ranges"])]
    if ordinary_pre_code:
        add("CLASSIC_PRE_CODE", ordinary_pre_code[0].start(), ordinary_pre_code[0].end(), count=len(ordinary_pre_code))

    parser = _MetricsParser()
    parser.feed(content)
    parser.close()
    thresholds = config["risk_thresholds"]
    max_rows = max((table["rows"] for table in parser.table_shapes), default=0)
    max_columns = max((table["max_columns"] for table in parser.table_shapes), default=0)
    max_cells = max((table["cells"] for table in parser.table_shapes), default=0)

    simple_rules = {
        "MEDIA_IMAGE": parser.counts["img"],
        "MEDIA_LOCAL_IMAGE": parser.internal_images,
        "LINK_INTERNAL": parser.internal_links,
        "MEDIA_FIGURE": parser.counts["figure"],
        "MEDIA_IFRAME": parser.counts["iframe"],
        "STRUCT_TABLE": parser.counts["table"],
    }
    for rule_id, count in simple_rules.items():
        if count:
            add(rule_id, 0, min(len(content), 240), count=count)
    if blocks["counts"].get("core/embed", 0):
        add("MEDIA_EMBED", 0, min(len(content), 240), count=blocks["counts"]["core/embed"])
    if (max_rows >= thresholds["large_table_rows"] or
            max_columns >= thresholds["large_table_columns"] or
            max_cells >= thresholds["large_table_cells"]):
        add("STRUCT_LARGE_TABLE", 0, min(len(content), 240))
    if len(content) >= thresholds["very_long_content"]:
        add("CONTENT_VERY_LONG", 0, min(len(content), 240))

    families = set()
    if rule_counts["SH_SHORTCODE"] or rule_counts["SH_GUTENBERG_BLOCK"]:
        families.add("syntaxhighlighter")
    if rule_counts["CBP_BLOCK_COMMENT"] or rule_counts["CBP_BLOCK_CLASS"]:
        families.add("code-block-pro")
    if rule_counts["CORE_CODE_BLOCK"]:
        families.add("core-code")
    if rule_counts["CLASSIC_PRE_CODE"]:
        families.add("classic-pre-code")

    metrics = {
        "content_character_count": len(content),
        "image_count": parser.counts["img"],
        "local_image_count": parser.internal_images,
        "internal_link_count": parser.internal_links,
        "link_count": parser.counts["a"],
        "figure_count": parser.counts["figure"],
        "iframe_count": parser.counts["iframe"],
        "embed_count": blocks["counts"].get("core/embed", 0),
        "table_count": parser.counts["table"],
        "largest_table_rows": max_rows,
        "largest_table_columns": max_columns,
        "largest_table_cells": max_cells,
    }
    return {
        "matched_rule_ids": sorted(rule_counts),
        "rule_counts": dict(sorted(rule_counts.items())),
        "evidence": evidence,
        "blocks": blocks,
        "shortcodes": shortcodes,
        "metrics": metrics,
        "code_format_families": sorted(families),
        "structure_damaged": blocks["damaged"],
        "shortcode_structure_damaged": shortcodes["damaged"],
        "code_format_unknown": bool(rule_counts["SH_DAMAGED"]),
        "classic_outside_blocks": bool(rule_counts["CLASSIC_SUBSTANTIAL_OUTSIDE_BLOCKS"]),
    }
