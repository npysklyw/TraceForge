from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BENCHWARDEN_", env_file=".env", extra="ignore")
    database_url: SecretStr = Field(
        default=SecretStr(
            "postgresql+psycopg://benchwarden:benchwarden@localhost:5432/benchwarden"
        ),
        # Keep existing installations connected to their data during configuration upgrades.
        validation_alias=AliasChoices("BENCHWARDEN_DATABASE_URL", "DATABASE_URL"),
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
