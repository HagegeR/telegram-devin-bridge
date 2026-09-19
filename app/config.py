from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_webhook_secret: str | None = None
    devin_api_key: str
    devin_service_user_api_key: str | None = None
    devin_org_id: str | None = None
    public_base_url: str | None = None
    database_path: str = "./bridge.sqlite3"
    devin_api_base_url: str = "https://api.devin.ai"
    devin_max_acu_limit: int = 3
    devin_poll_seconds: float = 5
    devin_poll_fast_seconds: float = 1
    devin_watch_timeout_seconds: float = 1800
    devin_settle_seconds: float = 30
    devin_status_after_seconds: float = 8
    devin_session_instructions: str = ""
    telegram_debounce_seconds: float = 1.5
    telegram_queue_while_busy: bool = True
    telegram_long_reply_chars: int = 3500
    telegram_rate_limit_per_minute: int = 20
    telegram_mode: str = "webhook"
    telegram_rich_messages: bool = True
    telegram_drafts: bool = False
    telegram_allowed_chat_ids: str = ""
    telegram_allowed_users: str = ""
    telegram_allow_all_users: bool = False
    telegram_free_response_chats: str = ""
    telegram_home_channel: int | None = None
    telegram_notification_mode: str = "important"
    notify_secret: str | None = None
    doctor_secret: str | None = None
    bot_username: str | None = None
    telegram_admin_user_ids: str = ""
    transcription_api_key: str | None = None
    transcription_base_url: str = "https://api.openai.com/v1"
    transcription_model: str = "whisper-1"
    telegram_attach_voice: bool = False
    github_token: str | None = None
    self_update_command: str = "sh deploy/self-update.sh"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    @model_validator(mode="after")
    def validate_transport(self) -> "Settings":
        if self.telegram_mode not in {"webhook", "polling"}:
            raise ValueError("telegram_mode must be webhook or polling")
        if self.telegram_mode == "webhook":
            if not self.public_base_url:
                raise ValueError("public_base_url is required in webhook mode")
            if not self.telegram_webhook_secret:
                raise ValueError(
                    "telegram_webhook_secret is required in webhook mode"
                )
        return self

    @staticmethod
    def _csv_ints(value: str) -> frozenset[int]:
        return frozenset(
            int(item.strip())
            for item in value.split(",")
            if item.strip()
        )

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        return self._csv_ints(self.telegram_allowed_chat_ids)

    @property
    def allowed_users(self) -> frozenset[int]:
        return self._csv_ints(self.telegram_allowed_users)

    @property
    def free_response_chats(self) -> frozenset[int]:
        return self._csv_ints(self.telegram_free_response_chats)

    @property
    def admin_user_ids(self) -> frozenset[int]:
        return self._csv_ints(self.telegram_admin_user_ids)


@lru_cache
def get_settings() -> Settings:
    return Settings()
