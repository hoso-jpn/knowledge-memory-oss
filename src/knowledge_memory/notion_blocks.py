"""Notion blocks to Markdown. Structure and characters are kept as the API returned them."""

from __future__ import annotations

import re
from html import escape

from .core import MemoryError

HEADINGS = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}
# The blog rebuilds the Content property from annotations, because Notion strips the
# Markdown delimiters out of plain_text. Reproducing its rules keeps the stored body
# byte-identical to the string the blog renders.
DECORATION = (("bold", "strong"), ("italic", "em"), ("strikethrough", "del"),
              ("underline", "u"))
MARKDOWN_DECORATION = (("bold", "**"), ("italic", "*"), ("strikethrough", "~~"))


def code_span(text: str) -> str:
    """Fence inline code with one more backtick than its longest internal run."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def property_markdown(value) -> str:
    """Rebuild a rich_text property the way the blog does, decorations included."""
    if not isinstance(value, dict):
        return ""
    if value.get("object") == "list":
        return "".join(property_markdown(entry) for entry in value.get("results", []))
    kind = value.get("type")
    if kind in ("rich_text", "title"):
        items = value.get(kind)
        items = items if isinstance(items, list) else [items]
        return "".join(_item_markdown(item) for item in items if isinstance(item, dict))
    return property_text(value)


def _item_markdown(item) -> str:
    annotations = item.get("annotations") or {}
    href = item.get("href") or (item.get("text") or {}).get("link")
    if isinstance(href, dict):
        href = href.get("url")
    plain = item.get("plain_text")
    if plain is None:
        plain = (item.get("text") or {}).get("content", "")
    if not any(annotations.get(key) for key, _ in DECORATION) \
            and not annotations.get("code") and not href:
        return plain                      # untouched, so Gutenberg HTML passes through
    text = code_span(plain) if annotations.get("code") else _escape(plain)
    for key, tag in DECORATION:
        if annotations.get(key):
            text = f"<{tag}>{text}</{tag}>"
    if href:
        target = _escape(href).replace('"', "&quot;")
        text = f'<a href="{target}">{text}</a>'
    return text


def rich_text(items) -> str:
    """Join a rich_text array. Notion splits long text across items, so order matters."""
    out = []
    for item in items or []:
        text = item.get("plain_text")
        if text is None:
            text = (item.get("text") or {}).get("content", "")
        if item.get("type") == "equation" and not item.get("plain_text"):
            text = (item.get("equation") or {}).get("expression", "")
        annotations = item.get("annotations") or {}
        if item.get("type") == "equation":
            text = f"${text}$"
        elif annotations.get("code"):
            text = code_span(text)
        else:
            for key, mark in MARKDOWN_DECORATION:
                if annotations.get(key):
                    text = f"{mark}{text}{mark}"
            if annotations.get("underline"):
                text = f"<u>{text}</u>"
        link = (item.get("text") or {}).get("link") or item.get("href")
        if isinstance(link, dict):
            link = link.get("url")
        if link:
            text = f"[{text}]({link})"
        out.append(text)
    return "".join(out)


def property_text(value) -> str:
    """Read a page property or a full property read-back into plain text."""
    if not isinstance(value, dict):
        return ""
    if value.get("object") == "list":
        parts = []
        for entry in value.get("results", []):
            kind = entry.get("type")
            if kind in ("rich_text", "title"):
                parts.append(rich_text([entry[kind]]))
            else:
                parts.append(property_text(entry))
        return "".join(parts)
    kind = value.get("type")
    if kind in ("rich_text", "title"):
        return rich_text(value.get(kind))
    if kind == "date":
        date = value.get("date") or {}
        return date.get("start") or ""
    if kind == "checkbox":
        return "true" if value.get("checkbox") else "false"
    if kind in ("select", "status"):
        return ((value.get(kind) or {}) or {}).get("name") or ""
    if kind == "multi_select":
        return ", ".join(option.get("name", "") for option in value.get("multi_select") or [])
    if kind in ("url", "email", "phone_number"):
        return value.get(kind) or ""
    if kind == "number":
        number = value.get("number")
        return "" if number is None else str(number)
    if kind == "formula":
        return property_text({"type": (value.get("formula") or {}).get("type"),
                              **(value.get("formula") or {})})
    if kind == "string":
        return value.get("string") or ""
    return ""


def _cell_html(items):
    """Render text as escaped HTML, retaining links, formatting and line breaks."""
    out = []
    for item in items or []:
        plain = item.get("plain_text")
        if plain is None:
            plain = (item.get("text") or {}).get("content", "")
        if item.get("type") == "equation":
            plain = item.get("plain_text") or (item.get("equation") or {}).get("expression", "")
        text = escape(plain).replace("\n", "<br>")
        annotations = item.get("annotations") or {}
        for key, tag in (*DECORATION, ("code", "code")):
            if annotations.get(key):
                text = f"<{tag}>{text}</{tag}>"
        link = item.get("href") or (item.get("text") or {}).get("link")
        if isinstance(link, dict):
            link = link.get("url")
        if link:
            text = f'<a href="{escape(link, quote=True)}">{text}</a>'
        out.append(text)
    return "".join(out)


def _table(block, indent, out):
    body = block.get("table") or {}
    width = body.get("table_width")
    if type(width) is not int or width < 1:
        raise MemoryError("Notion table requires a positive table_width")
    for flag in ("has_column_header", "has_row_header"):
        if type(body.get(flag)) is not bool:
            raise MemoryError(f"Notion table requires boolean {flag}")
    out.append(indent + "<table>")
    for row_index, row in enumerate(block.get("_children") or []):
        cells = (row.get("table_row") or {}).get("cells")
        if row.get("type") != "table_row" or not isinstance(cells, list) or len(cells) != width:
            raise MemoryError("Notion table row width/type does not match its table")
        out.append(indent + "<tr>")
        for column, cell in enumerate(cells):
            scope = ("col" if body["has_column_header"] and row_index == 0 else
                     "row" if body["has_row_header"] and column == 0 else None)
            tag = "th" if scope else "td"
            attribute = f' scope="{scope}"' if scope else ""
            out.append(f"{indent}<{tag}{attribute}>{_cell_html(cell)}</{tag}>")
        out.append(indent + "</tr>")
    out.append(indent + "</table>")


def _block(block, depth, out, page_url, report):
    kind = block.get("type")
    body = block.get(kind) or {}
    indent = "  " * depth
    text = rich_text(body.get("rich_text"))
    if kind in HEADINGS:
        out.append(f"{indent}{HEADINGS[kind]} {text}")
    elif kind == "paragraph":
        out.append(indent + text if text else "")
    elif kind == "bulleted_list_item":
        out.append(f"{indent}- {text}")
    elif kind == "numbered_list_item":
        out.append(f"{indent}1. {text}")
    elif kind == "to_do":
        mark = "x" if body.get("checked") else " "
        out.append(f"{indent}- [{mark}] {text}")
    elif kind == "quote":
        out.extend(f"{indent}> {line}" for line in (text or "").split("\n"))
    elif kind == "callout":
        out.extend(f"{indent}> {line}" for line in (text or "").split("\n"))
    elif kind == "code":
        language = body.get("language") or ""
        fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", text)), default=0))
        out.append(f"{indent}{fence}{language}")
        out.extend(indent + line for line in text.split("\n"))
        out.append(f"{indent}{fence}")
    elif kind == "equation":
        out.append(f"{indent}$${body.get('expression', '')}$$")
    elif kind == "divider":
        out.append(f"{indent}---")
    elif kind == "toggle":
        out.append(f"{indent}- {text}")
    elif kind in ("image", "video", "audio", "file", "pdf", "embed", "bookmark"):
        caption = rich_text(body.get("caption"))
        if body.get("type") in ("file", "file_upload") or "file" in body:
            # The signed link stays only in the raw snapshot, never in searchable text.
            block_id = block.get("id") or "unknown"
            source = page_url or ""
            if source and block.get("id"):
                source = source.split("#")[0] + "#" + block_id.replace("-", "")
            label = body.get("name") or kind
            label = label.replace("[", "\\[").replace("]", "\\]")
            reference = f"[{label}]({source})" if source else label
            out.append(f"{indent}添付控え: {reference} (block_id: {block_id})")
            if caption:
                out.append(indent + "キャプション: " + caption)
            report.append({"block_id": block_id, "type": kind, "action": "attachment_reference_only"})
        else:
            source = body.get("external", {}).get("url") or body.get("url") or ""
            label = caption or body.get("name") or kind
            out.append(f"{indent}[{label}]({source})" if source else f"{indent}[{label}]")
    elif kind == "table":
        _table(block, indent, out)
        out.append("")
        return
    elif kind in ("column_list", "column", "synced_block"):
        if kind == "synced_block" and body.get("synced_from") and not block.get("_children"):
            out.append(indent + "〔同期元の内容は未取得〕")
            report.append({"block_id": block.get("id"), "type": kind, "action": "unresolved"})
    elif kind in ("table_of_contents", "breadcrumb"):
        report.append({"block_id": block.get("id"), "type": kind, "action": "omit_navigation"})
    else:
        out.append(f"{indent}〔未対応ブロック: {kind} / {block.get('id', 'unknown')}〕")
        if text:
            out.append(indent + text)
        report.append({"block_id": block.get("id"), "type": kind, "action": "unsupported"})
    for child in block.get("_children") or []:
        _block(child, depth + 1 if kind not in ("table", "column_list", "column",
                                                "synced_block") else depth, out, page_url, report)
    if kind in HEADINGS or kind in ("paragraph", "code", "quote", "callout", "equation",
                                    "divider", "table"):
        out.append("")


def blocks_to_markdown(blocks, page_url=None, report=None) -> str:
    out = []
    report = report if report is not None else []
    for block in blocks or []:
        _block(block, 0, out, page_url, report)
    lines, blank = [], False
    for line in out:
        if line.strip() == "":
            blank = True
            continue
        if blank and lines:
            lines.append("")
        blank = False
        lines.append(line)
    return ("\n".join(lines).strip() + "\n") if lines else ""
