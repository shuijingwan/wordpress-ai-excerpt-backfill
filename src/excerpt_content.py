"""Prepare natural-language-only article text for excerpt generation."""

import html
import re


OMISSION = "正文中间部分已省略"
_CBP = re.compile(
    r"<!--\s+wp:kevinbatdorf/code-block-pro(?:\s+[^>]*)?-->(.*?)"
    r"<!--\s+/wp:kevinbatdorf/code-block-pro\s+-->", re.I | re.S,
)
_CBP_SELF_CLOSING = re.compile(
    r"<!--\s+wp:kevinbatdorf/code-block-pro(?:\s+[^>]*)?/\s*-->", re.I | re.S,
)
_PROTECTED_HTML = re.compile(
    r"<(pre|code|textarea|script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S,
)
_BLOCK_COMMENT = re.compile(r"<!--\s*/?wp:[\s\S]*?-->", re.I)
_HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_TAG = re.compile(r"<[^>]+>")
_SHORTCODE = re.compile(r"\[(?:/?[A-Za-z][\w-]*)(?:\s[^\]]*)?/?\]", re.S)


def extract_excerpt_source(content):
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    text = _CBP.sub(" ", content)
    text = _CBP_SELF_CLOSING.sub(" ", text)
    text = _PROTECTED_HTML.sub(" ", text)
    text = _BLOCK_COMMENT.sub(" ", text)
    text = _HTML_COMMENT.sub(" ", text)
    text = _SHORTCODE.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 28000:
        return text[:20000].rstrip() + f"\n\n{OMISSION}\n\n" + text[-8000:].lstrip()
    return text[:20000]
