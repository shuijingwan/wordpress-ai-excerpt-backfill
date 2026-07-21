"""Expansion helpers used only by synthetic test fixtures."""


def _non_negative_int(expansion, name):
    if name not in expansion:
        raise ValueError(f"fixture expansion requires {name!r}")
    value = expansion[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"fixture expansion {name!r} must be a non-negative integer")
    return value


def expand_fixture_content(case):
    """Return content after applying a declared synthetic fixture expansion."""
    if "content" not in case or not isinstance(case["content"], str):
        raise ValueError("fixture content must be a string")
    expansion = case.get("fixture_expansion")
    if expansion is None:
        return case["content"]
    if not isinstance(expansion, dict):
        raise ValueError("fixture_expansion must be an object")
    expansion_type = expansion.get("type")

    if expansion_type == "repeat-table-rows":
        rows = _non_negative_int(expansion, "rows")
        columns = _non_negative_int(expansion, "columns")
        cells = "".join(f"<td>值{column + 1}</td>" for column in range(columns))
        return "<table>" + "".join(f"<tr>{cells}</tr>" for _ in range(rows)) + "</table>"

    if expansion_type == "multiply-elements":
        images = _non_negative_int(expansion, "images")
        links = _non_negative_int(expansion, "links")
        image_html = "".join(
            f'<img src="/media/sample-{index + 1}.png" alt="示例">'
            for index in range(images)
        )
        link_html = "".join(
            f'<a href="/docs/sample-{index + 1}">站内</a>'
            for index in range(links)
        )
        return f"<p>{image_html}{link_html}</p>"

    if expansion_type == "repeat-text":
        count = _non_negative_int(expansion, "count")
        text = expansion.get("text")
        if not isinstance(text, str):
            raise ValueError("fixture expansion 'text' must be a string")
        return text * count

    if expansion_type is None:
        raise ValueError("fixture expansion requires 'type'")
    raise ValueError(f"unknown fixture expansion type: {expansion_type!r}")
