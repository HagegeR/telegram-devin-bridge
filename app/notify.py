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
        html: str | None = None,
        html_name: str = "report.html",
        topic: str | None = None,
        session_id: str | None = None,
        session_url: str | None = None,
    ) -> dict[str, object]: ...


MAX_HTML_BYTES = 2 * 1024 * 1024


def register_notify_route(
    application: FastAPI,
    runtime: NotificationRuntime,
    settings: Settings,
) -> None:
    @application.post("/notify")
    async def notify(request: Request) -> dict[str, object]:
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
        html = values.get("html")
        html_name = values.get("html_name", "report.html")
        topic = values.get("topic")
        session_id = values.get("session_id")
        session_url = values.get("session_url")
        if not isinstance(chat_id, int) and chat_id is not None:
            raise HTTPException(status_code=400, detail="chat_id must be an integer")
        if not isinstance(thread_id, int) and thread_id is not None:
            raise HTTPException(status_code=400, detail="thread_id must be an integer")
        if not isinstance(silent, bool) or not isinstance(markdown, bool):
            raise HTTPException(status_code=400, detail="invalid notification options")
        if html is not None and not isinstance(html, str):
            raise HTTPException(status_code=400, detail="html must be a string")
        if html is not None and len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise HTTPException(status_code=413, detail="html exceeds 2 MiB")
        if not isinstance(html_name, str) or not html_name.lower().endswith(".html"):
            raise HTTPException(status_code=400, detail="html_name must end with .html")
        if topic is not None and not isinstance(topic, str):
            raise HTTPException(status_code=400, detail="topic must be a string")
        if session_id is not None and not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="session_id must be a string")
        if session_url is not None and not isinstance(session_url, str):
            raise HTTPException(status_code=400, detail="session_url must be a string")
        try:
            return await runtime.notify(
                values["text"],
                chat_id=chat_id,
                thread_id=thread_id,
                silent=silent,
                markdown=markdown,
                html=html,
                html_name=html_name,
                topic=topic,
                session_id=session_id,
                session_url=session_url,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
