"""Submit live SNDK signal orders through the Robinhood MCP connection."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from dotenv import dotenv_values


TRADER_ROOT = Path(__file__).resolve().parent.parent
LOCAL_ENV = TRADER_ROOT / ".env"
BROKER_WORKER = Path(__file__).resolve().with_name("robinhood_order.py")
BROKER_PYTHON = TRADER_ROOT / "bars" / ".venv" / "bin" / "python"
BROKER_TIMEOUT_SECONDS = 330
ORDER_ACTIONS = frozenset(("entry", "exit_all"))


def _env_value(path: Path, name: str) -> str:
    try:
        value = dotenv_values(path).get(name)
    except (OSError, ValueError):
        return ""
    return value.strip() if isinstance(value, str) else ""


def _account_number(name: str) -> str:
    return os.environ.get(name, "").strip() or _env_value(LOCAL_ENV, name)


def _clean(value: Any, limit: int = 1_000) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit]


def _request(
    record: Mapping[str, Any],
    *,
    account_number: str,
    quantity: str,
    long_symbol: str,
    short_symbol: str,
) -> dict[str, str]:
    direction = int(record["direction"])
    action = str(record["action"])
    symbol = long_symbol if direction == 1 else short_symbol
    side = "buy" if action == "entry" else "sell"
    identity = ":".join(
        (
            "trader",
            str(record["ticker"]),
            str(record["algo"]),
            str(int(record["ts"])),
            action,
            str(direction),
            symbol,
            side,
        )
    )
    return {
        "account_number": account_number,
        "symbol": symbol,
        "side": side,
        "type": "market",
        "quantity": quantity,
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "ref_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
    }


def _label(record: Mapping[str, Any], request: Mapping[str, str]) -> str:
    return (
        "algo=%s action=%s symbol=%s side=%s quantity=%s"
        % (
            _clean(record.get("algo"), 64),
            _clean(record.get("action"), 16),
            request["symbol"],
            request["side"],
            request["quantity"],
        )
    )


def _submit_one(
    record: Mapping[str, Any],
    request: Mapping[str, str],
    config_path: Path,
    on_result: Callable[[str, str], None],
) -> None:
    label = _label(record, request)
    try:
        completed = subprocess.run(
            [str(BROKER_PYTHON), str(BROKER_WORKER), str(config_path)],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=BROKER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        on_result(
            "error",
            "broker order failed %s: timed out after %d seconds"
            % (label, BROKER_TIMEOUT_SECONDS),
        )
        return
    except BaseException as exc:
        on_result(
            "error",
            "broker order failed %s: %s: %s"
            % (label, type(exc).__name__, _clean(exc)),
        )
        return

    if completed.returncode:
        reason = _clean(completed.stderr or completed.stdout or "unknown broker error")
        on_result("error", "broker order rejected %s: %s" % (label, reason))
        return
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        response = {}
    data = response.get("data", {}) if isinstance(response, dict) else {}
    order = data.get("order", data) if isinstance(data, dict) else {}
    order_id = _clean(order.get("id"), 80) if isinstance(order, dict) else ""
    state = _clean(order.get("state"), 40) if isinstance(order, dict) else ""
    suffix = ""
    if order_id or state:
        suffix = " order_id=%s state=%s" % (order_id or "n/a", state or "n/a")
    on_result("info", "broker order submitted %s%s" % (label, suffix))


def _submit_all(
    records: Sequence[Mapping[str, Any]],
    *,
    account_number: str,
    quantity: str,
    config_path: Path,
    long_symbol: str,
    short_symbol: str,
    on_result: Callable[[str, str], None],
) -> None:
    for record in records:
        request = _request(
            record,
            account_number=account_number,
            quantity=quantity,
            long_symbol=long_symbol,
            short_symbol=short_symbol,
        )
        _submit_one(record, request, config_path, on_result)


def send_broker_orders(
    records: Sequence[Mapping[str, Any]],
    *,
    config_path: Path,
    account_env: str,
    quantity: str,
    target_ticker: str,
    long_symbol: str,
    short_symbol: str,
    on_result: Callable[[str, str], None],
) -> int:
    """Queue one real broker order per eligible live SNDK trade record."""
    eligible = [
        dict(record)
        for record in records
        if record.get("ticker") == target_ticker
        and record.get("action") in ORDER_ACTIONS
        and record.get("direction") in (-1, 1)
    ]
    if not eligible:
        return 0
    account_number = _account_number(account_env)
    if not account_number:
        on_result(
            "error",
            "broker order failed: %s is not configured" % account_env,
        )
        return 0
    worker = threading.Thread(
        target=_submit_all,
        kwargs={
            "records": eligible,
            "account_number": account_number,
            "quantity": quantity,
            "config_path": config_path,
            "long_symbol": long_symbol,
            "short_symbol": short_symbol,
            "on_result": on_result,
        },
        name="broker-orders",
        daemon=True,
    )
    worker.start()
    return len(eligible)
