"""Submit mapped live signal orders through the Robinhood MCP connection."""

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
REJECTED_ORDER_STATES = frozenset(
    ("cancelled", "failed", "locate_failed", "rejected", "voided")
)
_SUBMISSION_LOCK = threading.Lock()


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
    dollar_amount: str,
    symbol: str,
) -> dict[str, Any]:
    direction = int(record["direction"])
    action = str(record["action"])
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
            ",".join(str(value) for value in record.get("entry_order_ids", ())),
        )
    )
    request = {
        "account_number": account_number,
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "ref_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
    }
    if action == "entry":
        request["dollar_amount"] = dollar_amount
    else:
        request["entry_order_ids"] = list(record["entry_order_ids"])
    return request


def _label(record: Mapping[str, Any], request: Mapping[str, Any]) -> str:
    size = (
        "dollar_amount=%s" % request["dollar_amount"]
        if "dollar_amount" in request
        else "entry_orders=%d" % len(request["entry_order_ids"])
    )
    return (
        "algo=%s action=%s symbol=%s side=%s %s"
        % (
            _clean(record.get("algo"), 64),
            _clean(record.get("action"), 16),
            request["symbol"],
            request["side"],
            size,
        )
    )


def _submit_one(
    record: Mapping[str, Any],
    request: Mapping[str, Any],
    config_path: Path,
    on_result: Callable[[str, str], None],
    on_submitted: Callable[[Mapping[str, Any], str], None],
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
    skipped = _clean(response.get("skipped")) if isinstance(response, dict) else ""
    if skipped:
        on_result("warn", "broker close skipped %s: %s" % (label, skipped))
        return
    data = response.get("data", {}) if isinstance(response, dict) else {}
    order = data.get("order", data) if isinstance(data, dict) else {}
    order_id = _clean(order.get("id"), 80) if isinstance(order, dict) else ""
    state = _clean(order.get("state"), 40) if isinstance(order, dict) else ""
    if state in REJECTED_ORDER_STATES:
        reason = _clean(order.get("reject_reason")) if isinstance(order, dict) else ""
        on_result(
            "error",
            "broker order rejected %s: state=%s%s"
            % (label, state, " reason=%s" % reason if reason else ""),
        )
        return
    if not order_id:
        on_result(
            "error",
            "broker order failed %s: response did not contain an order id" % label,
        )
        return
    try:
        on_submitted(record, order_id)
    except BaseException as exc:
        on_result(
            "error",
            "broker order state failed %s order_id=%s: %s: %s"
            % (label, order_id, type(exc).__name__, _clean(exc)),
        )
        return
    suffix = ""
    if order_id or state:
        suffix = " order_id=%s state=%s" % (order_id or "n/a", state or "n/a")
    on_result("info", "broker order submitted %s%s" % (label, suffix))


def _submit_all(
    records: Sequence[Mapping[str, Any]],
    *,
    account_number: str,
    dollar_amount: str,
    config_path: Path,
    execution_tickers: Mapping[str, Mapping[str, str]],
    entry_order_ids: Callable[[Mapping[str, Any]], Sequence[str]],
    on_result: Callable[[str, str], None],
    on_submitted: Callable[[Mapping[str, Any], str], None],
) -> None:
    with _SUBMISSION_LOCK:
        for record in records:
            mapping = execution_tickers[str(record["ticker"])]
            symbol = mapping["long" if int(record["direction"]) == 1 else "short"]
            if record["action"] == "entry":
                actionable = ({**record, "symbol": symbol},)
            else:
                try:
                    order_ids = tuple(entry_order_ids(record))
                except BaseException as exc:
                    on_result(
                        "error",
                        "broker close state failed algo=%s symbol=%s: %s: %s"
                        % (
                            _clean(record.get("algo"), 64),
                            symbol,
                            type(exc).__name__,
                            _clean(exc),
                        ),
                    )
                    continue
                if not order_ids:
                    on_result(
                        "info",
                        "broker close skipped algo=%s symbol=%s: no accepted real entry"
                        % (_clean(record.get("algo"), 64), symbol),
                    )
                    continue
                actionable = (
                    {
                        **record,
                        "symbol": symbol,
                        "entry_order_ids": order_ids,
                    },
                )
            for actionable_record in actionable:
                request = _request(
                    actionable_record,
                    account_number=account_number,
                    dollar_amount=dollar_amount,
                    symbol=symbol,
                )
                _submit_one(
                    actionable_record,
                    request,
                    config_path,
                    on_result,
                    on_submitted,
                )


def _complete(
    records: Sequence[Mapping[str, Any]],
    on_complete: Callable[[Sequence[Mapping[str, Any]]], None] | None,
    on_result: Callable[[str, str], None],
) -> None:
    if on_complete is None:
        return
    try:
        on_complete(records)
    except BaseException as exc:
        on_result(
            "error",
            "post-broker processing failed: %s: %s"
            % (type(exc).__name__, _clean(exc)),
        )


def _submit_all_then_complete(
    records: Sequence[Mapping[str, Any]],
    *,
    account_number: str,
    dollar_amount: str,
    config_path: Path,
    execution_tickers: Mapping[str, Mapping[str, str]],
    entry_order_ids: Callable[[Mapping[str, Any]], Sequence[str]],
    on_result: Callable[[str, str], None],
    on_submitted: Callable[[Mapping[str, Any], str], None],
    on_complete: Callable[[Sequence[Mapping[str, Any]]], None] | None,
) -> None:
    try:
        _submit_all(
            records,
            account_number=account_number,
            dollar_amount=dollar_amount,
            config_path=config_path,
            execution_tickers=execution_tickers,
            entry_order_ids=entry_order_ids,
            on_result=on_result,
            on_submitted=on_submitted,
        )
    finally:
        _complete(records, on_complete, on_result)


def send_broker_orders(
    records: Sequence[Mapping[str, Any]],
    *,
    config_path: Path,
    account_env: str,
    dollar_amount: str,
    execution_tickers: Mapping[str, Mapping[str, str]],
    entry_order_ids: Callable[[Mapping[str, Any]], Sequence[str]],
    on_result: Callable[[str, str], None],
    on_submitted: Callable[[Mapping[str, Any], str], None],
    on_complete: Callable[[Sequence[Mapping[str, Any]]], None] | None = None,
) -> int:
    """Queue mapped live orders and skip tickers without an execution mapping."""
    eligible = [
        dict(record)
        for record in records
        if record.get("ticker") in execution_tickers
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
        threading.Thread(
            target=_complete,
            args=(eligible, on_complete, on_result),
            name="post-broker-processing",
            daemon=True,
        ).start()
        return 0
    worker = threading.Thread(
        target=_submit_all_then_complete,
        kwargs={
            "records": eligible,
            "account_number": account_number,
            "dollar_amount": dollar_amount,
            "config_path": config_path,
            "execution_tickers": execution_tickers,
            "entry_order_ids": entry_order_ids,
            "on_result": on_result,
            "on_submitted": on_submitted,
            "on_complete": on_complete,
        },
        name="broker-orders",
        daemon=True,
    )
    worker.start()
    return len(eligible)
