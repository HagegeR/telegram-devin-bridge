from __future__ import annotations

import re


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
