import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from app.clients import DevinClient, TelegramClient
from app.config import get_settings
from app.store import Store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()
store = Store(settings.database_path)
devin = DevinClient(
    api_key=settings.devin_api_key,
    base_url=settings.devin_api_base_url,
    poll_seconds=settings.devin_poll_seconds,
    reply_timeout_seconds=settings.devin_reply_timeout_seconds,
    max_acu_limit=settings.devin_max_acu_limit,
)
telegram = TelegramClient(settings.telegram_bot_token)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await devin.close()
    await telegram.close()


app = FastAPI(title="Telegram–Devin Bridge", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()
    message = update.get("message")
    if not isinstance(message, dict):
        return {"accepted": True}

    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return {"accepted": True}

    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return {"accepted": True}
    if settings.allowed_chat_ids and chat_id not in settings.allowed_chat_ids:
        logger.warning("Rejected Telegram message from unauthorized chat %s", chat_id)
        return {"accepted": True}

    background_tasks.add_task(process_message, chat_id, text)

    return {"accepted": True}


async def process_message(chat_id: int, text: str) -> None:
    try:
        session = store.get(chat_id)
        if session is None:
            session_id = await devin.create_session(
                f"You are replying to a user through Telegram. User message: {text}",
                chat_id,
            )
            store.put(chat_id, session_id)
            previous_message_id = None
        else:
            session_id, previous_message_id = session
            await devin.send_message(session_id, text)

        reply = await devin.wait_for_reply(session_id, previous_message_id)
        if reply is None:
            await telegram.send_message(
                chat_id,
                "Devin is still working. Please send another message in a moment.",
            )
        else:
            message_id, reply_text = reply
            store.mark_message(chat_id, message_id)
            await telegram.send_message(chat_id, reply_text)
    except Exception:
        logger.exception("Failed to process Telegram message for chat %s", chat_id)
        await telegram.send_message(
            chat_id,
            "I couldn't reach Devin right now. Please try again shortly.",
        )
