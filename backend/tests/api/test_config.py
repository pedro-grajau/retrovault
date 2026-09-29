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
    assert PROJECT_ROOT.name == "retrovault"
