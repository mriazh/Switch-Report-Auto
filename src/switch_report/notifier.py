"""WhatsApp + console status notifications (design 2.5).

`Notifier` posts a structured run summary to the GOWA WhatsApp gateway when
`GOWA_BASE_URL`/`GOWA_TARGET_JID` are configured, and falls back to the
console otherwise (or when the API call fails). No secrets are ever logged.
"""
from __future__ import annotations

import sys
from typing import Optional, Sequence

import requests


def format_duration(seconds: float | int) -> str:
    """Format duration in seconds to human readable string.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted duration string: <60s -> '{s}s', <3600s -> '{m}m {s}s', >=3600s -> '{h}h {m}m {s}s'
    """
    total_s = max(0, int(round(seconds)))
    if total_s < 60:
        return f"{total_s}s"
    elif total_s < 3600:
        m = total_s // 60
        s = total_s % 60
        return f"{m}m {s}s"
    else:
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        # Remove zero minutes and seconds for cleaner output
        if m == 0 and s == 0:
            return f"{h}h"
        elif s == 0:
            return f"{h}h {m}m"
        else:
            return f"{h}h {m}m {s}s"


def _format_message(success: bool, mode: str, target_date_str: str,
                    duration_sec: float, files_written: Sequence[str],
                    error: Optional[str]) -> str:
    """Build the notification body in single-line format."""
    if success:
        elapsed = format_duration(duration_sec)
        count = len(files_written)
        return (f"[GMF Switch Report Automation] SUCCESS | mode={mode} | "
                f"date={target_date_str} | elapsed={elapsed} | workbooks={count}")
    else:
        error_summary = error or "unknown error"
        return (f"[GMF Switch Report Automation] FAILED | mode={mode} | "
                f"date={target_date_str} | error={error_summary}")


def _format_start_message(mode: str, target_date_str: str) -> str:
    """Build the START notification message in single-line format."""
    return (f"[GMF Switch Report Automation] START | mode={mode} | "
            f"date={target_date_str}")


class Notifier:
    """Send one run-status notification via the GOWA WhatsApp gateway or
    the console."""

    def __init__(self, base_url: Optional[str], target_jid: Optional[str],
                 *, device_id: Optional[str] = None,
                 timeout_seconds: float = 5.0,
                 mode: str = "", target_date_str: str = ""):
        self._base_url = base_url
        self._target_jid = target_jid
        self._device_id = device_id
        self._timeout = timeout_seconds
        self._mode = mode
        self._target = target_date_str

    # ------------------------------------------------------------------
    def _post_whatsapp(self, text: str) -> bool:
        url = f"{self._base_url.rstrip('/')}/send/message"
        headers = {}
        if self._device_id:
            headers["X-Device-Id"] = self._device_id
        try:
            resp = requests.post(
                url,
                json={"phone": self._target_jid, "message": text},
                headers=headers,
                timeout=self._timeout,
            )
            ok = resp.status_code in (200, 201)
            if not ok:
                # Surface the API error text in the console fallback.
                print(f"[notifier] GOWA post failed ({resp.status_code}): "
                      f"{getattr(resp, 'text', '')}", file=sys.stderr)
            return ok
        except requests.RequestException as err:
            print(f"[notifier] GOWA post error: {err}", file=sys.stderr)
            return False

    def _print_console(self, text: str) -> None:
        print(f"[notifier] {text}")

    # ------------------------------------------------------------------
    def send(self, *, success: bool, duration_sec: float,
             files_written: Sequence[str] = (), error: Optional[str] = None) -> bool:
        """Dispatch one notification. Returns True when the message was
        delivered (GOWA 2xx, or console fallback — the console path
        always succeeds)."""
        text = _format_message(
            success=success, mode=self._mode, target_date_str=self._target,
            duration_sec=duration_sec, files_written=files_written,
            error=error,
        )
        if self._base_url and self._target_jid:
            if self._post_whatsapp(text):
                return True
            # GOWA failed -> also mirror to console so the operator sees it.
            self._print_console(text)
            return False
        self._print_console(text)
        return True

    def send_start(self) -> bool:
        """Send the START notification."""
        text = _format_start_message(mode=self._mode, target_date_str=self._target)
        if self._base_url and self._target_jid:
            if self._post_whatsapp(text):
                return True
            self._print_console(text)
            return False
        self._print_console(text)
        return True


# ---------------------------------------------------------------------------
# Module-level convenience used by the scheduler
# ---------------------------------------------------------------------------


def notify(*, settings, success: bool, mode: str, target_date_str: str,
           duration_sec: float, files_written: Sequence[str] = (),
           error: Optional[str] = None) -> bool:
    """Build a Notifier from `settings` and send one notification."""
    n = Notifier(
        base_url=settings.GOWA_BASE_URL,
        target_jid=settings.GOWA_TARGET_JID,
        device_id=settings.GOWA_DEVICE_ID,
        timeout_seconds=settings.GOWA_TIMEOUT_SECONDS,
        mode=mode,
        target_date_str=target_date_str,
    )
    return n.send(success=success, duration_sec=duration_sec,
                  files_written=files_written, error=error)


def notify_start(*, settings, mode: str, target_date_str: str) -> bool:
    """Send a START notification at the beginning of a run."""
    n = Notifier(
        base_url=settings.GOWA_BASE_URL,
        target_jid=settings.GOWA_TARGET_JID,
        device_id=settings.GOWA_DEVICE_ID,
        timeout_seconds=settings.GOWA_TIMEOUT_SECONDS,
        mode=mode,
        target_date_str=target_date_str,
    )
    return n.send_start()