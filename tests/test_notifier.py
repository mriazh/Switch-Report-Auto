"""Unit tests for switch_report.notifier (design 2.5, NFR-4).

GOWA WhatsApp delivery is mocked at the HTTP boundary (`requests.post`);
the console-fallback path is captured with `capsys`. No network in tests.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from switch_report import notifier


def _mk_gowa(monkeypatch: pytest.MonkeyPatch, ok: bool = True) -> MagicMock:
    """Patch requests.post and return the mock."""
    resp = MagicMock()
    resp.status_code = 200 if ok else 400
    resp.json.return_value = {"ok": ok, "description": "done" if ok else "gowa error"}

    def fake_post(url, json=None, timeout=None, **kw):
        fake_post.calls.append((url, json, kw))
        return resp

    fake_post.calls = []
    monkeypatch.setattr(notifier.requests, "post", fake_post)
    return fake_post


# ---------------------------------------------------------------------------
# Notifier.send
# ---------------------------------------------------------------------------


def test_send_whatsapp_when_configured(monkeypatch, capsys):
    fake = _mk_gowa(monkeypatch, ok=True)
    n = notifier.Notifier(
        base_url="http://gowa.local:8080",
        target_jid="628123456789@s.whatsapp.net",
        mode="daily", target_date_str="2026-10-08",
    )
    ok = n.send(success=True, duration_sec=12.5,
                files_written=["a.xlsx", "b.xlsx"], error=None)
    assert ok is True
    # Exactly one HTTP call, to GOWA /send/message, phone JID, message set.
    assert len(fake.calls) == 1
    url, payload, kw = fake.calls[0]
    assert url == "http://gowa.local:8080/send/message"
    assert payload["phone"] == "628123456789@s.whatsapp.net"
    text = payload["message"]
    assert "2026-10-08" in text
    assert "SUCCESS" in text
    assert "mode=daily" in text
    assert "elapsed=12s" in text or "elapsed=13s" in text  # rounding
    assert "workbooks=2" in text
    # No X-Device-Id header when unset.
    assert kw.get("headers", {}) == {}


def test_send_whatsapp_includes_device_id_header(monkeypatch):
    fake = _mk_gowa(monkeypatch, ok=True)
    n = notifier.Notifier(
        base_url="http://gowa.local:8080/",
        target_jid="628123456789@s.whatsapp.net",
        device_id="dev-1",
        mode="daily", target_date_str="2026-10-08",
    )
    n.send(success=True, duration_sec=1.0, files_written=[], error=None)
    url, payload, kw = fake.calls[0]
    assert url == "http://gowa.local:8080/send/message"
    assert kw["headers"] == {"X-Device-Id": "dev-1"}


def test_send_whatsapp_failure_returns_false(monkeypatch):
    fake = _mk_gowa(monkeypatch, ok=False)
    n = notifier.Notifier(
        base_url="http://gowa.local:8080",
        target_jid="628123456789@s.whatsapp.net",
        mode="daily", target_date_str="2026-10-08",
    )
    ok = n.send(success=False, duration_sec=3.0,
                files_written=[], error="poll timeout")
    assert ok is False
    url, payload, _ = fake.calls[0]
    assert "FAILED" in payload["message"]
    assert "mode=daily" in payload["message"]
    assert "error=poll timeout" in payload["message"]


def test_send_whatsapp_failure_returns_false_on_network_error(monkeypatch):
    import requests as real_requests

    def boom(url, json=None, timeout=None, **kw):
        raise real_requests.ConnectionError("no route")

    monkeypatch.setattr(notifier.requests, "post", boom)
    n = notifier.Notifier(
        base_url="http://gowa.local:8080",
        target_jid="628123456789@s.whatsapp.net",
        mode="daily", target_date_str="2026-10-08",
    )
    ok = n.send(success=True, duration_sec=1.0, files_written=["x.xlsx"], error=None)
    assert ok is False


# ---------------------------------------------------------------------------
# Notifier.send_start
# ---------------------------------------------------------------------------


def test_send_start_whatsapp_when_configured(monkeypatch, capsys):
    fake = _mk_gowa(monkeypatch, ok=True)
    n = notifier.Notifier(
        base_url="http://gowa.local:8080",
        target_jid="628123456789@s.whatsapp.net",
        mode="daily", target_date_str="2026-10-08",
    )
    ok = n.send_start()
    assert ok is True
    assert len(fake.calls) == 1
    url, payload, _ = fake.calls[0]
    assert url == "http://gowa.local:8080/send/message"
    assert payload["phone"] == "628123456789@s.whatsapp.net"
    text = payload["message"]
    assert "2026-10-08" in text
    assert "START" in text
    assert "mode=daily" in text


def test_send_start_console_fallback(capsys):
    n = notifier.Notifier(base_url=None, target_jid=None,
                          mode="monthly", target_date_str="2026-09")
    ok = n.send_start()
    assert ok is True
    out = capsys.readouterr().out
    assert "2026-09" in out
    assert "START" in out
    assert "mode=monthly" in out


# ---------------------------------------------------------------------------
# Console fallback
# ---------------------------------------------------------------------------


def test_send_without_gowa_falls_back_to_console(monkeypatch, capsys):
    fake = _mk_gowa(monkeypatch)  # set up but not called
    n = notifier.Notifier(base_url=None, target_jid=None,
                          mode="monthly", target_date_str="2026-09")
    ok = n.send(success=True, duration_sec=99.0,
                files_written=["a.xlsx"], error=None)
    assert ok is True
    assert len(fake.calls) == 0  # no GOWA call
    captured = capsys.readouterr()
    out = captured.out
    assert "2026-09" in out
    assert "SUCCESS" in out
    assert "mode=monthly" in out
    assert "elapsed=1m 39s" in out or "elapsed=99s" in out
    assert "workbooks=1" in out


def test_send_failure_console_fallback_includes_error(capsys):
    n = notifier.Notifier(base_url=None, target_jid=None,
                          mode="daily", target_date_str="2026-10-08")
    ok = n.send(success=False, duration_sec=5.0, files_written=[],
                error="AirWave poll timed out")
    assert ok is True  # console fallback never fails
    out = capsys.readouterr().out
    assert "AirWave poll timed out" in out
    assert "FAILED" in out
    assert "mode=daily" in out


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def test_format_success_message_single_line():
    msg = notifier._format_message(
        success=True, mode="daily", target_date_str="2026-10-08",
        duration_sec=12.5, files_written=["a.xlsx", "b.xlsx", "c.xlsx"],
        error=None,
    )
    assert msg == ("[GMF Switch Report Automation] SUCCESS | mode=daily | "
                   "date=2026-10-08 | elapsed=12s | workbooks=3")


def test_format_failure_message_single_line():
    msg = notifier._format_message(
        success=False, mode="monthly", target_date_str="2026-09",
        duration_sec=2.0, files_written=[], error="boom",
    )
    assert msg == ("[GMF Switch Report Automation] FAILED | mode=monthly | "
                   "date=2026-09 | error=boom")


def test_format_start_message_single_line():
    msg = notifier._format_start_message(mode="daily", target_date_str="2026-10-08")
    assert msg == ("[GMF Switch Report Automation] START | mode=daily | "
                   "date=2026-10-08")


def test_format_duration():
    assert notifier.format_duration(0) == "0s"
    assert notifier.format_duration(30) == "30s"
    assert notifier.format_duration(59) == "59s"
    assert notifier.format_duration(60) == "1m 0s"
    assert notifier.format_duration(61) == "1m 1s"
    assert notifier.format_duration(90) == "1m 30s"
    assert notifier.format_duration(3599) == "59m 59s"
    assert notifier.format_duration(3600) == "1h"
    assert notifier.format_duration(3661) == "1h 1m 1s"
    assert notifier.format_duration(7200) == "2h"
    assert notifier.format_duration(7260) == "2h 1m"
    assert notifier.format_duration(7320) == "2h 2m"