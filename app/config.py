from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_webhook_secret: str
    devin_api_key: str
    public_base_url: str
    database_path: str = "./bridge.sqlite3"
    devin_api_base_url: str = "https://api.devin.ai"
    devin_max_acu_limit: int = 3
    devin_poll_seconds: float = 3
    devin_watch_timeout_seconds: float = 1800
    devin_settle_seconds: float = 30
    telegram_allowed_chat_ids: str = ""
    telegram_allowed_users: str = ""
    telegram_allow_all_users: bool = False
    telegram_free_response_chats: str = ""
    telegram_home_channel: int | None = None
    telegram_notification_mode: str = "important"
    notify_secret: str | None = None
    bot_username: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
