"""Send trader entry and exit alerts through macOS Messages."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from dotenv import dotenv_values


PT = ZoneInfo("America/Los_Angeles")
TRADER_ROOT = Path(__file__).resolve().parent.parent
LOCAL_ENV = TRADER_ROOT / ".env"
DT_ENV = TRADER_ROOT.parent / "dt" / ".env"
DESTINATION_ENV = "TRADER_SMS_TO"
SHARED_DESTINATION_ENV = "DT_SMS_TO"
ALERT_ACTIONS = frozenset(("entry", "exit_all"))
ALERT_SEND_TIMEOUT_SECONDS = 10

_SCRIPT = """
on run argv
    set destination to item 1 of argv
    set alertText to item 2 of argv
    tell application "Messages"
        set messageService to first service whose service type = iMessage
        set recipient to buddy destination of messageService
        send alertText to recipient
    end tell
end run
""".strip()


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str | None = None


def _env_value(path: Path, name: str) -> str:
    try:
        value = dotenv_values(path).get(name)
    except (OSError, ValueError):
        return ""
    return value.strip() if isinstance(value, str) else ""


def destination() -> str:
    """Read the trader destination, then reuse DT's configured destination."""
    return (
        os.environ.get(DESTINATION_ENV, "").strip()
        or _env_value(LOCAL_ENV, DESTINATION_ENV)
        or os.environ.get(SHARED_DESTINATION_ENV, "").strip()
        or _env_value(DT_ENV, SHARED_DESTINATION_ENV)
    )


def send_trade_alerts(
    records: Sequence[Mapping[str, Any]],
    *,
    on_failure: Callable[[str], None],
) -> int:
    """Queue one Messages alert per entry or exit and return the count."""
    try:
        recipient = destination()
        eligible = [
            dict(record)
            for record in records
            if record.get("action") in ALERT_ACTIONS
        ]
        if not recipient or not eligible:
            return 0
        worker = threading.Thread(
            target=_send_all,
            args=(recipient, eligible, on_failure),
            name="trade-alerts",
            daemon=True,
        )
        worker.start()
        return len(eligible)
    except BaseException as exc:
        _report_failure(on_failure, f"{type(exc).__name__}: {exc}")
        return 0


def _send_all(
    recipient: str,
    records: Sequence[Mapping[str, Any]],
    on_failure: Callable[[str], None],
) -> None:
    for record in records:
        try:
            result = _send_message(
                recipient,
                _format_alert(record),
                timeout=ALERT_SEND_TIMEOUT_SECONDS,
            )
            if not result.ok:
                _report_failure(on_failure, result.error or "unknown alerting failure")
        except BaseException as exc:
            _report_failure(on_failure, f"{type(exc).__name__}: {exc}")


def _send_message(destination_value: str, message: str, *, timeout: float) -> SendResult:
    try:
        subprocess.run(
            ["osascript", "-e", _SCRIPT, destination_value, message],
            timeout=timeout,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return SendResult(
            False,
            f"osascript timed out after {timeout:g}s; a macOS permission dialog may be waiting",
        )
    except subprocess.CalledProcessError as exc:
        stderr = " ".join(str(exc.stderr or "").split())
        detail = stderr or "osascript returned no stderr"
        return SendResult(False, f"osascript exited with status {exc.returncode}: {detail}")
    except BaseException as exc:
        return SendResult(False, f"{type(exc).__name__}: {exc}")
    return SendResult(True)


def _format_alert(record: Mapping[str, Any]) -> str:
    action = "EXIT" if record.get("action") == "exit_all" else "ENTRY"
    algo = _field(record.get("algo"), 32)
    direction = "SHORT" if record.get("direction") == -1 else "LONG"
    ticker = _field(record.get("ticker"), 12)
    price = _field(_price(record.get("price")), 16)
    session_time = _field(_session_time(record.get("ts")), 16)
    return (
        f"trader {action} {algo} {direction} {ticker} @ {price} "
        f"({session_time} PT)"
    )


def _price(value: Any) -> str:
    try:
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError, OverflowError):
        return "n/a"


def _session_time(value: Any) -> str:
    try:
        timestamp = datetime.fromtimestamp(int(value), tz=timezone.utc)
        return timestamp.astimezone(PT).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "time n/a"


def _field(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "n/a").split())
    return text[:limit]


def _report_failure(on_failure: Callable[[str], None], reason: str) -> None:
    try:
        on_failure(" ".join(reason.split()))
    except BaseException:
        pass
