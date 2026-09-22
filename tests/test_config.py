"""Unit tests for switch_report.config.Settings."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from switch_report.config import Settings

REQUIRED_ENV = {
    "AIRWAVE_URL": "https://192.168.240.196",
    "AIRWAVE_USERNAME": "user",
    "AIRWAVE_PASSWORD": "pass",
}

OPTIONAL_ENV_KEYS = [
    "REPORT_BASE_DIR", "TEMPLATES_DIR", "TIMEZONE",
    "AIRWAVE_POLL_INTERVAL_SECONDS", "AIRWAVE_POLL_TIMEOUT_MINUTES",
    "AIRWAVE_MAX_RETRIES", "GOWA_BASE_URL", "GOWA_TARGET_JID",
    "GOWA_DEVICE_ID", "GOWA_TIMEOUT_SECONDS",
]


def _make(monkeypatch, **overrides) -> Settings:
    # `_env_file=None` keeps the repo's real config/.env out of the test.
    for key in OPTIONAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    env = dict(REQUIRED_ENV)
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    return Settings(_env_file=None)


def test_defaults(monkeypatch):
    s = _make(monkeypatch)
    assert s.AIRWAVE_URL == "https://192.168.240.196"


def test_credentials(monkeypatch):
    s = _make(monkeypatch)
    assert s.AIRWAVE_USERNAME == "user"
    assert s.AIRWAVE_PASSWORD == "pass"


def test_poll_defaults(monkeypatch):
    s = _make(monkeypatch)
    assert s.AIRWAVE_POLL_INTERVAL_SECONDS == 20
    assert s.AIRWAVE_POLL_TIMEOUT_MINUTES == 5
    assert s.AIRWAVE_MAX_RETRIES == 3


def test_paths_are_path(monkeypatch):
    s = _make(monkeypatch)
    assert isinstance(s.REPORT_BASE_DIR, Path)
    assert isinstance(s.TEMPLATES_DIR, Path)
    assert s.REPORT_BASE_DIR == Path("Report/Switch")
    assert s.TEMPLATES_DIR == Path("config/templates")


def test_gowa_optional_defaults_none(monkeypatch):
    s = _make(monkeypatch)
    assert s.GOWA_BASE_URL is None
    assert s.GOWA_TARGET_JID is None
    assert s.GOWA_DEVICE_ID is None
    assert s.GOWA_TIMEOUT_SECONDS == 5.0
    assert s.whatsapp_notifications_enabled is False


def test_whatsapp_notifications_enabled_when_gowa_set(monkeypatch):
    s = _make(monkeypatch, GOWA_BASE_URL="http://gowa.local:8080",
              GOWA_TARGET_JID="628123456789@s.whatsapp.net")
    assert s.whatsapp_notifications_enabled is True


def test_whatsapp_notifications_disabled_without_target(monkeypatch):
    s = _make(monkeypatch, GOWA_BASE_URL="http://gowa.local:8080")
    assert s.whatsapp_notifications_enabled is False


def test_timezone_default(monkeypatch):
    s = _make(monkeypatch)
    assert s.TIMEZONE == "Asia/Jakarta"


def test_missing_required_env_raises(monkeypatch):
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def _write_env(directory: Path, name: str, **values) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8"
    )


def test_discovers_config_dot_env(tmp_path, monkeypatch):
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    _write_env(tmp_path / "config", ".env", **REQUIRED_ENV, TIMEZONE="Asia/Makassar")
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.AIRWAVE_URL == "https://192.168.240.196"
    assert s.TIMEZONE == "Asia/Makassar"


def test_discovers_root_dot_env(tmp_path, monkeypatch):
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    _write_env(tmp_path, ".env", **REQUIRED_ENV)
    monkeypatch.chdir(tmp_path)
    assert Settings().AIRWAVE_USERNAME == "user"


def test_root_dot_env_wins_over_config_dot_env(tmp_path, monkeypatch):
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    _write_env(tmp_path / "config", ".env", **REQUIRED_ENV, TIMEZONE="Asia/Makassar")
    _write_env(tmp_path, ".env", TIMEZONE="UTC")
    monkeypatch.chdir(tmp_path)
    assert Settings().TIMEZONE == "UTC"


def test_process_env_wins_over_dot_env_files(tmp_path, monkeypatch):
    _write_env(tmp_path / "config", ".env", **REQUIRED_ENV, TIMEZONE="Asia/Makassar")
    _write_env(tmp_path, ".env", TIMEZONE="Asia/Jayapura")
    monkeypatch.setenv("TIMEZONE", "Asia/Jakarta")
    monkeypatch.chdir(tmp_path)
    assert Settings().TIMEZONE == "Asia/Jakarta"


def test_derived_dirs(monkeypatch):
    s = _make(monkeypatch, REPORT_BASE_DIR="Report/Switch")
    assert s.raw_dir == Path("Report/Switch/Raw")
    assert s.output_dir == Path("Report/Switch/Output")
    assert s.auth_dir == Path(".auth")
    assert s.monthly_dir(2026, 9) == Path("Report/Switch/2026-09")


def test_overrides(monkeypatch):
    s = _make(
        monkeypatch,
        AIRWAVE_POLL_INTERVAL_SECONDS=5,
        AIRWAVE_MAX_RETRIES=7,
        TIMEZONE="UTC",
    )
    assert s.AIRWAVE_POLL_INTERVAL_SECONDS == 5
    assert s.AIRWAVE_MAX_RETRIES == 7
    assert s.TIMEZONE == "UTC"
