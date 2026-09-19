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
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", link, text)

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
        if line.startswith("#"):
            result.append(f"*{_escape(line.lstrip('#').strip())}*")
        elif line.startswith(">"):
            result.append(">" + _inline(line[1:].lstrip()))
        elif re.match(r"^-\s+", line):
            result.append("• " + _inline(line[2:]))
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
    match = re.fullmatch(r"OPTIONS:\s*(.+)", lines[index].strip())
    if match is None:
        return text, []
    options = [item.strip() for item in match.group(1).split("|")]
    if not 1 <= len(options) <= 8 or any(
        not option or len(option) > 60 for option in options
    ):
        return text, []
    return "\n".join(lines[:index]).rstrip(), options


def extract_attachments(text: str) -> tuple[str, list[str]]:
    pattern = re.compile(r"^\s*ATTACHMENT:(\{.*\})\s*$")
    urls: list[str] = []
    lines = text.splitlines()
    found = False
    remaining: list[str] = []
    for line in lines:
        match = pattern.fullmatch(line)
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
