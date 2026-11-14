"""Bounded plain JSON transport in Jira strings or a plain-text ADF document."""

from ..api_wire import text


def document(value: str) -> dict[str, object]:
    text(value, 30_000)
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": value}]}],
    }


def plain(value: object) -> str:
    if isinstance(value, str):
        return text(value, 30_000)
    if (
        not isinstance(value, dict)
        or set(value) != {"type", "version", "content"}
        or value["type"] != "doc"
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["content"], list)
        or not 1 <= len(value["content"]) <= 100
    ):
        raise ValueError("expected plain contract text")
    paragraphs = []
    for paragraph in value["content"]:
        if (
            not isinstance(paragraph, dict)
            or set(paragraph) != {"type", "content"}
            or paragraph["type"] != "paragraph"
            or not isinstance(paragraph["content"], list)
            or not 1 <= len(paragraph["content"]) <= 100
        ):
            raise ValueError("unsupported contract formatting")
        parts = []
        for part in paragraph["content"]:
            if (
                not isinstance(part, dict)
                or set(part) != {"type", "text"}
                or part["type"] != "text"
            ):
                raise ValueError("unsupported contract formatting")
            parts.append(text(part["text"], 30_000))
        paragraphs.append("".join(parts))
    return text("\n".join(paragraphs), 30_000)
