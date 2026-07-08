"""Config guards."""

import pytest

from app.config import Settings
from app.main import check_prod_config


def test_prod_refuses_default_webhook_secret() -> None:
    settings = Settings(app_env="production", telegram_webhook_secret="cutebot-webhook-secret")
    with pytest.raises(RuntimeError, match="TELEGRAM_WEBHOOK_SECRET"):
        check_prod_config(settings)


def test_prod_accepts_real_secret_and_dev_accepts_default() -> None:
    check_prod_config(Settings(app_env="production", telegram_webhook_secret="s3cret"))
    check_prod_config(
        Settings(app_env="development", telegram_webhook_secret="cutebot-webhook-secret")
    )
