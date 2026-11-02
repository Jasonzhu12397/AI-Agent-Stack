import pytest
from app.config import Settings


def test_settings_accept_valid_environment_and_worker_interval() -> None:
    settings = Settings(app_env="test", worker_poll_seconds=1.5)

    assert settings.app_env == "test"
    assert settings.worker_poll_seconds == 1.5


def test_settings_reject_unknown_environment() -> None:
    with pytest.raises(ValueError):
        Settings(app_env="staging")


@pytest.mark.parametrize("interval", [0, -1, 301])
def test_settings_reject_invalid_worker_poll_interval(interval: float) -> None:
    with pytest.raises(ValueError):
        Settings(worker_poll_seconds=interval)
