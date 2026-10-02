from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
        populate_by_name=True,
    )
    app_version: str = "dev"
    database_url: str = (
        "postgresql+psycopg://postgres:retrovault-local@db:5432/retrovault"
    )
    retroachievements_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "RETROACHIEVEMENTS_API_KEY", "RETRO_ACHIEVEMENTS_KEY"
        ),
    )
    retroachievements_cache_dir: Path = PROJECT_ROOT / ".cache" / "retroachievements"

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

    @field_validator("retroachievements_api_key")
    @classmethod
    def ra_key_must_not_contain_control_characters(cls, value: SecretStr) -> SecretStr:
        if any(ord(character) < 32 for character in value.get_secret_value()):
            raise ValueError("RETROACHIEVEMENTS_API_KEY is invalid")
        return value


settings = Settings()
