from __future__ import annotations

import json
import re


def normalize_rich_linebreaks(text: str) -> str:
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    in_fence = False
    for index, value in enumerate(lines):
        line = value.rstrip("\r\n")
        newline = value[len(line) :]
        if not newline:
            result.append(value)
            continue
        if not line:
            result.append(value)
            continue
        next_line = (
            lines[index + 1].rstrip("\r\n")
            if index + 1 < len(lines)
            else ""
        )
        is_table = _is_pipe_table_line(line) or _is_pipe_table_line(next_line)
        is_fence = line.lstrip().startswith("```")
        if (
            newline == "\n"
            and not in_fence
            and not is_fence
            and not is_table
            and bool(next_line)
            and index + 1 < len(lines)
        ):
            result.append(line + "  \n")
        else:
            result.append(value)
        if is_fence:
            in_fence = not in_fence
    return "".join(result)


def _is_pipe_table_line(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 2 and stripped.startswith("|") and stripped.endswith("|")


def _escape(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)


def _inline(text: str) -> str:
    tokens: list[str] = []

    def protect(value: str) -> str:
        marker = f"\x00{len(tokens)}\x00"
        tokens.append(value)
        return marker

    def code(match: re.Match[str]) -> str:
        return protect(f"`{match.group(1)}`")

    def link(match: re.Match[str]) -> str:
        label = _escape(match.group(1))
        url = match.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return protect(f"[{label}]({url})")

    text = re.sub(r"`([^`\n]+)`", code, text)
    text = re.sub(r"\[([^\[\]\n]+)\]\(([^()\n]+)\)", link, text)

    def styled(pattern: str, opening: str, closing: str) -> None:
        nonlocal text

        def replacement(match: re.Match[str]) -> str:
            value = next(
                (group for group in match.groups() if group is not None),
                "",
            )
            return protect(f"{opening}{_escape(value)}{closing}")

        text = re.sub(pattern, replacement, text)

    styled(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__", "*", "*")
    styled(r"~~([^~\n]+)~~", "~", "~")
    styled(r"\|\|([^|\n]+)\|\|", "||", "||")
    styled(
        r"(?<!\w)\*([^*\n]+)\*(?!\w)|(?<!\w)_([^_\n]+)_(?!\w)",
        "_",
        "_",
    )

    escaped = _escape(text)
    for index, token in enumerate(tokens):
        escaped = escaped.replace(f"\x00{index}\x00", token)
    return escaped


def markdown_to_telegram_markdown_v2(text: str) -> str:
    lines = text.splitlines()
    result: list[str] = []
    in_fence = False
    fence_language = ""
    for line in lines:
        if line.startswith("```"):
            if not in_fence:
                in_fence = True
                fence_language = line[3:].strip()
                result.append(f"```{fence_language}")
            else:
                in_fence = False
                result.append("```")
            continue
        if in_fence:
            result.append(line.replace("\\", "\\\\").replace("`", "\\`"))
            continue
        if line.startswith("**>"):
            result.append("**>" + _inline(line[3:].lstrip()))
        elif line.startswith("#"):
            result.append(f"*{_escape(line.lstrip('#').strip())}*")
        elif line.startswith(">"):
            closer = line[1:].lstrip()
            if closer.rstrip() == "||":
                result.append(">||")
            else:
                result.append(">" + _inline(closer))
        elif re.match(r"^-\s+", line):
            result.append("• " + _inline(line[2:]))
        elif line.strip() == "||":
            result.append("||")
        else:
            result.append(_inline(line))
    if in_fence:
        result.append("```")
    return "\n".join(result)


def chunk(text: str, limit: int = 4096) -> list[str]:
    if limit < 32:
        raise ValueError("limit must leave room for chunk suffixes")
    if len(text) <= limit:
        return [text]
    capacity = limit - 16
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    in_fence = False
    language = ""

    def flush(close_fence: bool = False) -> None:
        nonlocal current, current_len
        if close_fence and in_fence:
            current.append("```")
        chunks.append("\n".join(current))
        current = []
        current_len = 0

    for line in text.splitlines():
        if line.startswith("```"):
            extra = len(line) + (1 if current else 0)
            if current and current_len + extra > capacity:
                flush(close_fence=in_fence)
                if in_fence:
                    opening = f"```{language}" if language else "```"
                    current.append(opening)
                    current_len = len(opening)
            current.append(line)
            current_len += len(line) + (1 if len(current) > 1 else 0)
            if not in_fence:
                in_fence = True
                language = line[3:].strip()
            else:
                in_fence = False
            continue
        remaining = line
        if not remaining:
            if current_len + (1 if current else 0) >= capacity:
                flush(close_fence=in_fence)
                if in_fence:
                    opening = f"```{language}" if language else "```"
                    current.append(opening)
                    current_len = len(opening)
            current.append("")
            current_len += 1 if len(current) > 1 else 0
            continue
        while remaining:
            reserve = 3 if in_fence else 0
            available = capacity - current_len - reserve
            if available <= 0:
                flush(close_fence=in_fence)
                if in_fence:
                    opening = f"```{language}" if language else "```"
                    current.append(opening)
                    current_len = len(opening)
                continue
            piece = remaining[:available]
            current.append(piece)
            current_len += len(piece) + (1 if len(current) > 1 else 0)
            remaining = remaining[len(piece) :]
    if current:
        flush()
    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [
        f"{value} ({index}/{total})"
        for index, value in enumerate(chunks, start=1)
    ]


def extract_options(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    index = len(lines) - 1
    while index >= 0 and not lines[index].strip():
        index -= 1
    if index < 0:
        return text, []
    match = re.fullmatch(r"OPTIONS:(.+)", lines[index].strip())
    if match is None:
        return text, []
    options = [item.strip() for item in match.group(1).split("|")]
    if not 1 <= len(options) <= 8 or any(
        not option or len(option) > 60 for option in options
    ):
        return text, []
    return "\n".join(lines[:index]).rstrip(), options


def extract_attachments(text: str) -> tuple[str, list[str]]:
    pattern = re.compile(r"ATTACHMENT:(\{.*\})")
    urls: list[str] = []
    lines = text.splitlines()
    found = False
    remaining: list[str] = []
    for line in lines:
        match = pattern.fullmatch(line.strip())
        if match is None:
            remaining.append(line)
            continue
        found = True
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        url = value.get("url") if isinstance(value, dict) else None
        if isinstance(url, str) and url not in urls:
            urls.append(url)
    if not found:
        return text, []
    return "\n".join(remaining).rstrip(), urls


def extract_large_code_blocks(
    text: str,
    *,
    minimum_chars: int = 1500,
) -> tuple[str, list[tuple[str, bytes]]]:
    pattern = re.compile(r"```([A-Za-z0-9]*)\n(.*?)```", re.DOTALL)
    documents: list[tuple[str, bytes]] = []
    counter = 0

    def replacement(match: re.Match[str]) -> str:
        nonlocal counter
        code = match.group(2)
        if len(code) <= minimum_chars:
            return match.group(0)
        counter += 1
        language = match.group(1)
        extension = language if language.isalnum() else "txt"
        filename = f"snippet-{counter}.{extension or 'txt'}"
        documents.append((filename, code.encode()))
        return f"📎 {filename}"

    return pattern.sub(replacement, text), documents


def split_long_text(text: str, limit: int) -> tuple[str, str]:
    if len(text) <= limit:
        return text, ""
    boundary = text.rfind("\n\n", 0, limit + 1)
    if boundary <= 0:
        boundary = text.rfind("\n", 0, limit + 1)
    if boundary <= 0:
        boundary = limit
    return text[:boundary].rstrip(), text[boundary:].lstrip()


# InputRichBlock / RichText builders for sendRichMessage's `blocks` field.
# Structured blocks need no escaping — values are JSON, not markup.


def rich_paragraph(text: object) -> dict[str, object]:
    return {"type": "paragraph", "text": text}


def rich_details(summary: object, blocks: list[dict[str, object]]) -> dict[str, object]:
    return {"type": "details", "summary": summary, "blocks": blocks}


def rich_text_bold(text: str) -> dict[str, object]:
    return {"type": "bold", "text": text}


def rich_text_code(text: str) -> dict[str, object]:
    return {"type": "code", "text": text}


def rich_text_link(text: str, url: str) -> dict[str, object]:
    return {"type": "url", "text": text, "url": url}


_END_DETAILS = re.compile(r"^END\s+DETAILS\s*$", re.IGNORECASE)
_END_TABLE = re.compile(r"^END\s+TABLE\s*$", re.IGNORECASE)
_TABLE_SEPARATOR = re.compile(r":?-+:?")
_FENCE = re.compile(r"^```")

_CONTROL_LINE = re.compile(
    r"^(REACT|URGENT|SILENT|PIN|PROGRESS|POLL)\s*:\s*(.*)$"
)


def extract_controls(text: str) -> tuple[str, dict[str, object]]:
    """Strip control marker lines from a reply body.

    Returns the remaining text plus a dict with any of: ``react`` (emoji),
    ``urgent``/``silent`` (notification override), ``pin``, ``progress``
    (edit-in-place), ``poll`` (``[question, *options]``). Marker lines that
    don't parse are kept as ordinary text.
    """
    controls: dict[str, object] = {}
    kept: list[str] = []
    in_fence = False
    in_details = False
    for line in text.split("\n"):
        stripped = line.strip()
        # Marker-looking lines inside code fences or a DETAILS: body are
        # literal content, not commands.
        if not in_fence and _END_DETAILS.match(stripped):
            in_details = False
        if in_fence or in_details:
            if _FENCE.match(stripped):
                in_fence = not in_fence
            kept.append(line)
            continue
        if _FENCE.match(stripped):
            in_fence = True
            kept.append(line)
            continue
        if stripped.startswith("DETAILS:"):
            in_details = True
            kept.append(line)
            continue
        match = _CONTROL_LINE.match(stripped)
        if match is None:
            kept.append(line)
            continue
        name, value = match.group(1), match.group(2).strip()
        if name == "REACT" and value:
            controls["react"] = value
        elif name == "URGENT" and not value:
            controls["urgent"] = True
        elif name == "SILENT" and not value:
            controls["silent"] = True
        elif name == "PIN" and not value:
            controls["pin"] = True
        elif name == "PROGRESS" and not value:
            controls["progress"] = True
        elif name == "POLL":
            parts = [part.strip() for part in value.split("|") if part.strip()]
            if (
                len(parts) in range(3, 12)
                and len(parts[0]) <= 300
                and all(len(part) <= 100 for part in parts[1:])
            ):
                controls["poll"] = parts
                continue
            kept.append(line)
            continue
        else:
            kept.append(line)
            continue
    return "\n".join(kept), controls


def parse_rich_segments(text: str) -> list[dict[str, object]] | None:
    """Split reply text on ``TABLE:``/``DETAILS:`` markers into ordered segments.

    Returns ``None`` when no marker is present so callers keep the plain
    send path. Each segment is ``{"markdown": str}`` or
    ``{"blocks": [...], "fallback": str}`` — fallback is marker-free text
    for deployments where rich messages are unavailable.
    """
    lines = text.split("\n")
    segments: list[dict[str, object]] = []
    plain: list[str] = []
    found = False
    index = 0

    def flush() -> None:
        while plain and not plain[0].strip():
            plain.pop(0)
        while plain and not plain[-1].strip():
            plain.pop()
        if plain:
            segments.append({"markdown": "\n".join(plain)})
            plain.clear()

    in_fence = False
    while index < len(lines):
        stripped = lines[index].strip()
        if _FENCE.match(stripped):
            in_fence = not in_fence
        if stripped == "TABLE:" and not in_fence:
            index += 1
            raw: list[str] = []
            while index < len(lines) and _is_pipe_table_line(lines[index]):
                raw.append(lines[index])
                index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index < len(lines) and _END_TABLE.match(lines[index].strip()):
                index += 1
            cells: list[list[dict[str, object]]] = []
            for row in raw:
                values = [cell.strip() for cell in row.strip().strip("|").split("|")]
                if all(_TABLE_SEPARATOR.fullmatch(cell) for cell in values):
                    continue
                cells.append(
                    [{"text": value, "is_header": not cells} for value in values]
                )
            if cells:
                found = True
                flush()
                segments.append(
                    {
                        "blocks": [
                            {
                                "type": "table",
                                "is_compact": True,
                                "is_striped": True,
                                "cells": cells,
                            }
                        ],
                        "fallback": "\n".join(raw),
                    }
                )
            else:
                plain.append("TABLE:")
                plain.extend(raw)
            continue
        if stripped.startswith("DETAILS:") and not in_fence:
            marker = lines[index]
            summary = stripped[len("DETAILS:") :].strip() or "Details"
            index += 1
            raw = []
            while index < len(lines) and not _END_DETAILS.match(
                lines[index].strip()
            ):
                raw.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1  # skip the END DETAILS line
            if any(row.strip() for row in raw):
                found = True
                flush()
                segments.append(
                    {
                        "blocks": [
                            rich_details(
                                summary,
                                [rich_paragraph(row) for row in raw if row.strip()],
                            )
                        ],
                        "fallback": "\n".join([summary, *raw]),
                    }
                )
            else:
                plain.append(marker)
                plain.extend(raw)
            continue
        plain.append(lines[index])
        index += 1
    if found:
        flush()
    return segments if found else None
