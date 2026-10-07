import pytest
from pydantic import ValidationError

from app.platform.config.settings import PROJECT_ROOT, Settings


def test_blank_app_version_fails_explicitly_without_secret_values() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(app_version="")
    message = str(error.value)
    assert "APP_VERSION must not be blank" in message
    assert "postgres" not in message


def test_environment_file_is_anchored_to_the_repository() -> None:
    assert Settings.model_config["env_file"] == PROJECT_ROOT / ".env"


def test_legacy_retroachievements_key_name_is_supported(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RETRO_ACHIEVEMENTS_KEY=fixture-private-key\n")
    monkeypatch.delenv("RETROACHIEVEMENTS_API_KEY", raising=False)
    monkeypatch.delenv("RETRO_ACHIEVEMENTS_KEY", raising=False)

    configured = Settings(_env_file=env_file)

    assert (
        configured.retroachievements_api_key.get_secret_value()
        == "fixture-private-key"
    )


def test_blank_database_url_fails_explicitly() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(database_url="")
    assert "DATABASE_URL must not be blank" in str(error.value)


def test_retroachievements_api_key_is_masked_in_settings_repr() -> None:
    sentinel = "ra-private-sentinel-123"
    configured = Settings(retroachievements_api_key=sentinel)

    assert configured.retroachievements_api_key.get_secret_value() == sentinel
    assert sentinel not in repr(configured)
    assert "**********" in repr(configured.retroachievements_api_key)


def test_telegram_user_allowlist_parses_comma_separated_ids() -> None:
    configured = Settings(pixel_telegram_allowed_user_ids="12345,67890")

    assert configured.telegram_user_allowlist == frozenset({12345, 67890})
