from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request

from app.config import Settings


class NotificationRuntime(Protocol):
    async def notify(
        self,
        text: str,
        *,
        chat_id: int | None,
        thread_id: int | None,
        silent: bool,
        markdown: bool,
    ) -> int: ...


def register_notify_route(
    application: FastAPI,
    runtime: NotificationRuntime,
    settings: Settings,
) -> None:
    @application.post("/notify")
    async def notify(request: Request) -> dict[str, int]:
        if settings.notify_secret is None:
            raise HTTPException(status_code=404, detail="Not found")
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(
            authorization.encode(),
            f"Bearer {settings.notify_secret}".encode(),
        ):
            raise HTTPException(status_code=403, detail="Invalid notification secret")
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise HTTPException(status_code=400, detail="text is required")
        values = _mapping(payload)
        chat_id = values.get("chat_id")
        thread_id = values.get("thread_id")
        silent = values.get("silent", False)
        markdown = values.get("markdown", True)
        if not isinstance(chat_id, int) and chat_id is not None:
            raise HTTPException(status_code=400, detail="chat_id must be an integer")
        if not isinstance(thread_id, int) and thread_id is not None:
            raise HTTPException(status_code=400, detail="thread_id must be an integer")
        if not isinstance(silent, bool) or not isinstance(markdown, bool):
            raise HTTPException(status_code=400, detail="invalid notification options")
        try:
            sent = await runtime.notify(
                values["text"],
                chat_id=chat_id,
                thread_id=thread_id,
                silent=silent,
                markdown=markdown,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"sent": sent}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
