"""Safe Markdown rendering for the read-only learner viewer."""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token


def _is_external_url(href: str) -> bool:
    parsed = urlsplit(href)
    return bool(parsed.scheme or href.startswith("//"))


def _render_link_open(
    tokens: list[Token], index: int, options: dict[str, object], env: object
) -> str:
    token = tokens[index]
    href = token.attrGet("href") or ""
    if _is_external_url(href):
        token.attrSet("rel", "noopener noreferrer")
    return MARKDOWN.renderer.renderToken(tokens, index, options, env)


def _render_image_as_alt(
    tokens: list[Token], index: int, options: dict[str, object], env: object
) -> str:
    token = tokens[index]
    alt = token.attrGet("alt") or token.content or "Image"
    return f'<span class="markdown-image-alt">[Image: {escape(alt)}]</span>'


MARKDOWN = (
    MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    .enable("table")
    .enable("strikethrough")
)
MARKDOWN.renderer.rules["link_open"] = _render_link_open
MARKDOWN.renderer.rules["image"] = _render_image_as_alt

LESSON_MASTERY_MARKER = re.compile(
    r"<!-- /?lesson-mastery:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12} -->"
)


def render_markdown(source: str) -> str:
    """Render Markdown without raw HTML, remote images, or unsafe links."""

    env: dict[str, object] = {}
    tokens = MARKDOWN.parse(source, env)
    visible_tokens: list[Token] = []
    index = 0
    while index < len(tokens):
        if (
            index + 2 < len(tokens)
            and tokens[index].type == "paragraph_open"
            and tokens[index + 1].type == "inline"
            and tokens[index + 2].type == "paragraph_close"
            and LESSON_MASTERY_MARKER.fullmatch(tokens[index + 1].content.strip())
        ):
            index += 3
            continue
        if tokens[index].type == "inline":
            for child in tokens[index].children or []:
                if child.type == "text":
                    child.content = LESSON_MASTERY_MARKER.sub("", child.content)
        visible_tokens.append(tokens[index])
        index += 1
    return MARKDOWN.renderer.render(visible_tokens, MARKDOWN.options, env)
