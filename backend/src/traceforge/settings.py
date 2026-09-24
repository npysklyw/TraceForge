from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://traceforge:traceforge@localhost:5432/traceforge"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
