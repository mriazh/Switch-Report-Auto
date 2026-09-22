"""Typed configuration for the Switch-Report-Auto pipeline.

`Settings` is the single source of truth for all environment-driven values.
It is loaded from process env / `.env` via pydantic-settings so no credential
is ever hardcoded.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration for AirWave automation and Excel writing.

    Field names map to uppercase env vars (case-insensitive). `Optional`
    fields accept empty strings as "unset" (GOWA/WhatsApp is genuinely
    optional — the notifier falls back to the console).
    """

    model_config = SettingsConfigDict(
        # Later entries win: root `.env` overrides `config/.env`.
        # Paths are resolved relative to the process working directory.
        env_file=("config/.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # AirWave NMS connection
    AIRWAVE_URL: str
    AIRWAVE_USERNAME: str
    AIRWAVE_PASSWORD: str
    AIRWAVE_POLL_INTERVAL_SECONDS: int = 20
    AIRWAVE_POLL_TIMEOUT_MINUTES: int = 5
    AIRWAVE_MAX_RETRIES: int = 3

    # Paths
    REPORT_BASE_DIR: Path = Path("Report/Switch")
    TEMPLATES_DIR: Path = Path("config/templates")

    # Optional WhatsApp notifications via GOWA gateway
    GOWA_BASE_URL: Optional[str] = Field(default=None)
    GOWA_TARGET_JID: Optional[str] = Field(default=None)
    GOWA_DEVICE_ID: Optional[str] = Field(default=None)
    GOWA_TIMEOUT_SECONDS: float = 5.0

    TIMEZONE: str = "Asia/Jakarta"

    @property
    def raw_dir(self) -> Path:
        """Root for raw zip + extracted CSVs under the report base."""
        return self.REPORT_BASE_DIR / "Raw"

    @property
    def output_dir(self) -> Path:
        """Root for the 8 output workbooks under the report base."""
        return self.REPORT_BASE_DIR / "Output"

    @property
    def auth_dir(self) -> Path:
        """Directory for the reused Playwright session storage."""
        return Path(".auth")

    def monthly_dir(self, year: int, month: int) -> Path:
        """Return `REPORT_BASE_DIR/YYYY-MM` for a given month."""
        return self.REPORT_BASE_DIR / f"{year:04d}-{month:02d}"

    @property
    def whatsapp_notifications_enabled(self) -> bool:
        """True when a GOWA gateway and target JID are both configured."""
        return bool(self.GOWA_BASE_URL and self.GOWA_TARGET_JID)
