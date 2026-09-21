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
    telegram_images_as_documents: str = "auto"
    telegram_allowed_chat_ids: str = ""
    telegram_allowed_users: str = ""
    telegram_allow_all_users: bool = False
    telegram_free_response_chats: str = ""
    telegram_home_channel: int | None = None
    telegram_notification_mode: str = "important"
    notify_secret: str | None = None
    doctor_secret: str | None = None
    admin_secret: str | None = None
    admin_env_allowlist: str = (
        "DEVIN_SESSION_INSTRUCTIONS,DEVIN_MAX_ACU_LIMIT,DEVIN_POLL_SECONDS,"
        "DEVIN_POLL_FAST_SECONDS,DEVIN_WATCH_TIMEOUT_SECONDS,"
        "DEVIN_SETTLE_SECONDS,DEVIN_STATUS_AFTER_SECONDS,"
        "TELEGRAM_RICH_MESSAGES,TELEGRAM_DRAFTS,TELEGRAM_IMAGES_AS_DOCUMENTS,"
        "TRANSCRIPTION_BACKEND,TRANSCRIPTION_MODEL,TRANSCRIPTION_LANGUAGE,"
        "WHISPER_CPP_BIN,WHISPER_CPP_MODEL,WHISPER_CPP_FAST,"
        "WHISPER_CPP_EXTRA_ARGS,"
        "TELEGRAM_NOTIFICATION_MODE,"
        "TELEGRAM_FREE_RESPONSE_CHATS,TELEGRAM_ALLOWED_CHAT_IDS,"
        "TELEGRAM_ALLOWED_USERS,TELEGRAM_DEBOUNCE_SECONDS,"
        "TELEGRAM_QUEUE_WHILE_BUSY,TELEGRAM_LONG_REPLY_CHARS,"
        "TELEGRAM_RATE_LIMIT_PER_MINUTE,BOT_USERNAME"
    )
    admin_log_path: str = "/var/log/telegram-devin-bridge.log"
    admin_restart_command: str = (
        "nohup sh -c 'sleep 1; rc-service telegram-devin-bridge restart' "
        ">/dev/null 2>&1 &"
    )
    admin_env_path: str = ".env"
    bot_username: str | None = None
    telegram_admin_user_ids: str = ""
    transcription_api_key: str | None = None
    transcription_base_url: str = "https://api.openai.com/v1"
    transcription_backend: str = "api"
    transcription_model: str = "whisper-1"
    transcription_language: str | None = None
    whisper_cpp_bin: str = "whisper-cli"
    whisper_cpp_model: str = "/opt/whisper.cpp/models/ggml-base.en.bin"
    whisper_cpp_fast: bool = True
    whisper_cpp_extra_args: str = ""
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

    @model_validator(mode="after")
    def validate_image_delivery_mode(self) -> "Settings":
        self.telegram_images_as_documents = self.telegram_images_as_documents.casefold()
        if self.telegram_images_as_documents not in {"auto", "true", "false"}:
            raise ValueError(
                "telegram_images_as_documents must be auto, true, or false"
            )
        self.transcription_backend = self.transcription_backend.casefold()
        if self.transcription_backend not in {"api", "local", "whispercpp"}:
            raise ValueError(
                "transcription_backend must be api, local, or whispercpp"
            )
        return self

    @model_validator(mode="after")
    def validate_id_lists(self) -> "Settings":
        for field_name, property_name in (
            ("telegram_allowed_chat_ids", "allowed_chat_ids"),
            ("telegram_allowed_users", "allowed_users"),
            ("telegram_free_response_chats", "free_response_chats"),
            ("telegram_admin_user_ids", "admin_user_ids"),
        ):
            try:
                getattr(self, property_name)
            except ValueError as exc:
                raise ValueError(
                    f"{field_name} must be a comma-separated list of integers"
                ) from exc
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
        return self._csv_ints(self.telegram_admin_user_ids) or self.allowed_users

    @property
    def admin_env_keys(self) -> frozenset[str]:
        return frozenset(
            item.strip().upper()
            for item in self.admin_env_allowlist.split(",")
            if item.strip()
        )

    @property
    def transcription_enabled(self) -> bool:
        return self.transcription_backend in {"local", "whispercpp"} or bool(
            self.transcription_api_key
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
