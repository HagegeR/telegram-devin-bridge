from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

DEFAULT_FILE = "docs/devin-knowledge.md"
HTTP_TIMEOUT = 15


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Parse a leading --- ... --- block of simple `key: value` lines.

    Returns (fields, body). No YAML dependency: values are single-line strings
    (quotes stripped). Multi-line YAML is not supported.
    """
    fields: dict[str, str] = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = "\n".join(lines[index + 1 :]).strip()
                return fields, body
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip().strip('"').strip("'")
        return fields, "\n".join(lines).strip()
    return fields, text.strip()


def _extract_entries(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("items", "knowledge"):
            value = payload.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, Mapping)]
    return []


def _entry_id(entry: Mapping[str, Any]) -> str | None:
    for key in ("id", "knowledge_id"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


async def publish(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    *,
    name: str,
    body: str,
    trigger_description: str,
) -> tuple[str, str]:
    """Create or update a Devin Knowledge entry. Returns (action, id)."""
    headers = {"Authorization": f"Bearer {api_key}"}
    root = base_url.rstrip("/")
    payload = {
        "name": name,
        "body": body,
        "trigger_description": trigger_description,
    }
    list_response = await client.get(
        f"{root}/v1/knowledge", headers=headers, timeout=HTTP_TIMEOUT
    )
    list_response.raise_for_status()
    existing_id: str | None = None
    for entry in _extract_entries(list_response.json()):
        if entry.get("name") == name:
            existing_id = _entry_id(entry)
            break

    if existing_id is not None:
        response = await client.put(
            f"{root}/v1/knowledge/{existing_id}",
            headers=headers,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code in {404, 405}:
            raise RuntimeError(
                f"knowledge entry {existing_id!r} exists but PUT returned "
                f"{response.status_code}; update it manually"
            )
        response.raise_for_status()
        return "updated", existing_id

    response = await client.post(
        f"{root}/v1/knowledge", headers=headers, json=payload, timeout=HTTP_TIMEOUT
    )
    response.raise_for_status()
    result = response.json()
    return "created", _entry_id(result) or "<unknown>"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.publish_knowledge",
        description="Publish docs/devin-knowledge.md to the Devin Knowledge API",
    )
    parser.add_argument("--file", default=DEFAULT_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    text = Path(args.file).read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    name = fields.get("name", "")
    trigger_description = fields.get("trigger_description", "")
    if not name:
        parser.error(f"{args.file} has no `name` in front matter")

    payload = {
        "name": name,
        "body": body,
        "trigger_description": trigger_description,
    }
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    settings = get_settings()

    async def run() -> tuple[str, str]:
        async with httpx.AsyncClient() as client:
            return await publish(
                client,
                settings.devin_api_base_url,
                settings.devin_api_key,
                name=name,
                body=body,
                trigger_description=trigger_description,
            )

    action, entry_id = asyncio.run(run())
    print(f"{action}: id={entry_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
