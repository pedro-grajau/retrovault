from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")
    app_version: str = "dev"
    database_url: str = "postgresql+psycopg://postgres:retrovault-local@db:5432/retrovault"

    @field_validator("app_version")
    @classmethod
    def app_version_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("APP_VERSION must not be blank")
        return value

    @field_validator("database_url")
    @classmethod
    def database_url_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DATABASE_URL must not be blank")
        return value


settings = Settings()
