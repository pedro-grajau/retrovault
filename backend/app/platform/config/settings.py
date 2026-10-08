import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

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
    pixel_telegram_bot_token: SecretStr = Field(default=SecretStr(""))
    pixel_telegram_bot_username: str = ""
    pixel_telegram_webhook_secret: SecretStr = Field(default=SecretStr(""))
    pixel_telegram_allowed_user_ids: str = ""
    pixel_telegram_webhook_max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    pixel_telegram_message_max_age_seconds: int = Field(default=900, ge=60, le=3600)
    pixel_telegram_retention_days: int = Field(default=30, ge=1, le=365)
    pixel_telegram_typing_threshold_seconds: float = Field(default=3.0, ge=0.1, le=30)
    pixel_context_reference_secret: SecretStr = Field(default=SecretStr(""))
    pixel_context_reference_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    pixel_openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("PIXEL_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    pixel_openai_model_snapshot: str = ""
    pixel_public_site_url: str = ""
    pixel_ai_configuration_version: str = "intent-config.v1"
    pixel_ai_input_usd_per_million_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=10000
    )
    pixel_ai_output_usd_per_million_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=10000
    )
    pixel_ai_max_input_tokens: int = Field(default=20000, ge=1, le=32000)
    pixel_ai_max_output_tokens: int = Field(default=300, ge=1, le=4000)
    pixel_ai_monthly_budget_usd: Decimal = Field(
        default=Decimal("25.00"), gt=0, le=25
    )
    pixel_ai_reservation_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    pixel_ai_ledger_retention_days: int = Field(default=180, ge=180, le=180)

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

    @field_validator("pixel_telegram_bot_token")
    @classmethod
    def telegram_bot_token_must_not_contain_control_characters(
        cls, value: SecretStr
    ) -> SecretStr:
        token = value.get_secret_value()
        if any(ord(character) < 33 or ord(character) == 127 for character in token):
            raise ValueError("PIXEL_TELEGRAM_BOT_TOKEN is invalid")
        return value

    @field_validator("pixel_telegram_bot_username")
    @classmethod
    def telegram_username_is_valid(cls, value: str) -> str:
        username = value.removeprefix("@")
        if username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ValueError("PIXEL_TELEGRAM_BOT_USERNAME is invalid")
        return username

    @field_validator("pixel_telegram_webhook_secret")
    @classmethod
    def telegram_webhook_secret_is_valid(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret and (
            not 32 <= len(secret) <= 256
            or not re.fullmatch(r"[A-Za-z0-9_-]+", secret)
        ):
            raise ValueError(
                "PIXEL_TELEGRAM_WEBHOOK_SECRET must be 32 to 256 URL-safe characters"
            )
        return value

    @field_validator("pixel_telegram_allowed_user_ids")
    @classmethod
    def telegram_user_ids_are_valid(cls, value: str) -> str:
        if not value:
            return value
        parts = value.split(",")
        if (
            len(parts) > 1000
            or any(not part.isascii() or not part.isdigit() or int(part) <= 0 for part in parts)
            or len(set(parts)) != len(parts)
        ):
            raise ValueError("PIXEL_TELEGRAM_ALLOWED_USER_IDS must be unique positive IDs")
        return value

    @property
    def telegram_user_allowlist(self) -> frozenset[int]:
        return frozenset(
            int(value) for value in self.pixel_telegram_allowed_user_ids.split(",") if value
        )

    @field_validator("pixel_context_reference_secret")
    @classmethod
    def context_secret_must_be_long_enough(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret and len(secret.encode("utf-8")) < 32:
            raise ValueError("PIXEL_CONTEXT_REFERENCE_SECRET must be at least 32 bytes")
        return value

    @field_validator("pixel_openai_api_key")
    @classmethod
    def openai_api_key_has_no_control_characters(cls, value: SecretStr) -> SecretStr:
        if any(ord(character) < 33 or ord(character) == 127 for character in value.get_secret_value()):
            raise ValueError("OPENAI_API_KEY is invalid")
        return value

    @field_validator("pixel_openai_model_snapshot")
    @classmethod
    def openai_model_must_be_a_snapshot(cls, value: str) -> str:
        snapshot = value.strip()
        if snapshot and not re.fullmatch(r"[A-Za-z0-9._-]+-\d{4}-\d{2}-\d{2}", snapshot):
            raise ValueError("PIXEL_OPENAI_MODEL_SNAPSHOT must be a dated model snapshot")
        if snapshot:
            try:
                date.fromisoformat(snapshot[-10:])
            except ValueError as exc:
                raise ValueError(
                    "PIXEL_OPENAI_MODEL_SNAPSHOT must contain a valid calendar date"
                ) from exc
        return snapshot

    @field_validator("pixel_public_site_url")
    @classmethod
    def public_site_url_is_valid(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("PIXEL_PUBLIC_SITE_URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PIXEL_PUBLIC_SITE_URL is invalid")
        return value

    @field_validator("pixel_ai_configuration_version")
    @classmethod
    def ai_configuration_version_must_not_be_blank(cls, value: str) -> str:
        if not value.strip() or len(value) > 64:
            raise ValueError("PIXEL_AI_CONFIGURATION_VERSION is invalid")
        return value.strip()


settings = Settings()
