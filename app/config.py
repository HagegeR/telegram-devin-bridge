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
    devin_poll_seconds: float = 2
    devin_reply_timeout_seconds: float = 180
    telegram_allowed_chat_ids: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        return frozenset(
            int(value.strip())
            for value in self.telegram_allowed_chat_ids.split(",")
            if value.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
