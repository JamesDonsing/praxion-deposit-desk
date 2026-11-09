"""Runtime settings, read from environment variables only.

No secret ever has a default value. If PayPal sandbox credentials are missing,
the app runs in "mock" mode: every PayPal call is simulated locally and the UI
says so on every screen.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    paypal_env: str  # "sandbox" or "mock"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_invoicer_email: str = ""
    sandbox_buyer_email: str = ""
    llm_api_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    mock_webhook_secret: str = "local-mock-secret"
    database_path: str = "data/deposit_desk.db"
    public_base_url: str = "http://127.0.0.1:8000"
    business_name: str = "Sample Home Services Co. (fictional)"
    rate_limit_enabled: bool = True
    timezone_label: str = "local"
    extra: dict = field(default_factory=dict)

    @property
    def is_mock(self) -> bool:
        return self.paypal_env == "mock"

    @property
    def paypal_api_base(self) -> str:
        # Sandbox only. This demo never talks to the live PayPal environment.
        return "https://api-m.sandbox.paypal.com"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_url and self.llm_api_key)


def load_settings(**overrides) -> Settings:
    requested = _env("PAYPAL_ENV", "").lower()
    has_creds = bool(_env("PAYPAL_CLIENT_ID") and _env("PAYPAL_CLIENT_SECRET"))
    if requested == "live":
        raise RuntimeError(
            "PAYPAL_ENV=live is refused. Deposit Desk is a sandbox-only demo."
        )
    if requested not in ("", "sandbox", "mock"):
        raise RuntimeError(f"Unknown PAYPAL_ENV {requested!r}; use 'sandbox' or 'mock'.")
    env = requested or ("sandbox" if has_creds else "mock")
    if env == "sandbox" and not has_creds:
        raise RuntimeError(
            "PAYPAL_ENV=sandbox needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET. "
            "Unset PAYPAL_ENV to run in mock mode."
        )
    values = dict(
        paypal_env=env,
        paypal_client_id=_env("PAYPAL_CLIENT_ID"),
        paypal_client_secret=_env("PAYPAL_CLIENT_SECRET"),
        paypal_webhook_id=_env("PAYPAL_WEBHOOK_ID"),
        paypal_invoicer_email=_env("PAYPAL_INVOICER_EMAIL"),
        sandbox_buyer_email=_env("SANDBOX_BUYER_EMAIL"),
        llm_api_url=_env("LLM_API_URL"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL"),
        mock_webhook_secret=_env("MOCK_WEBHOOK_SECRET") or "local-mock-secret",
        database_path=_env("DATABASE_PATH") or "data/deposit_desk.db",
        public_base_url=(_env("PUBLIC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/"),
        rate_limit_enabled=_env("RATE_LIMIT", "on").lower() not in ("off", "0", "false"),
    )
    values.update(overrides)
    return Settings(**values)
