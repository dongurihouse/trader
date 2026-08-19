"""Record conservative option shadows after live equity order handling."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


TRADER_ROOT = Path(__file__).resolve().parent.parent
OPTION_WORKER = Path(__file__).resolve().with_name("robinhood_option_shadow.py")
OPTION_PYTHON = TRADER_ROOT / "bars" / ".venv" / "bin" / "python"
OPTION_TIMEOUT_SECONDS = 330


def _clean(value: Any, limit: int = 500) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def _worker(request: Mapping[str, Any], config_path: Path) -> tuple[dict[str, Any], str]:
    try:
        completed = subprocess.run(
            [str(OPTION_PYTHON), str(OPTION_WORKER), str(config_path)],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=OPTION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {}, "timed out after %d seconds" % OPTION_TIMEOUT_SECONDS
    except BaseException as exc:
        return {}, "%s: %s" % (type(exc).__name__, _clean(exc))
    if completed.returncode:
        return {}, _clean(completed.stderr or completed.stdout or "unknown quote error")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}, "worker returned invalid JSON"
    return (payload, "") if isinstance(payload, dict) else ({}, "worker returned invalid data")


def _exit_errors(
    positions: Sequence[Mapping[str, Any]], error: str
) -> list[dict[str, Any]]:
    return [
        {
            "entry_ts": int(position["entry_ts"]),
            "option_id": str(position["option_id"]),
            "status": "exit_error",
            "error": error,
        }
        for position in positions
    ]


def process_option_shadows(
    records: Sequence[Mapping[str, Any]],
    *,
    config_path: Path,
    open_positions: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
    on_entry: Callable[[Mapping[str, Any], Mapping[str, Any]], None],
    on_exits: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]]], None
    ],
    on_result: Callable[[str, str], None],
) -> None:
    """Quote and store one shadow contract for each eligible live trade."""
    for record in records:
        ticker = _clean(record.get("ticker"), 16)
        algo = _clean(record.get("algo"), 64)
        action = record.get("action")
        if action == "entry":
            payload, error = _worker(
                {
                    "action": "entry",
                    "ticker": ticker,
                    "direction": int(record["direction"]),
                    "underlying_price": record.get("price"),
                },
                config_path,
            )
            result = payload.get("result") if isinstance(payload, dict) else None
            if error or not isinstance(result, dict):
                result = {
                    "status": "entry_error",
                    "error": error or "worker returned no entry result",
                }
            try:
                on_entry(record, result)
            except BaseException as exc:
                on_result(
                    "error",
                    "option shadow state failed ticker=%s algo=%s action=entry: %s: %s"
                    % (ticker, algo, type(exc).__name__, _clean(exc)),
                )
                continue
            if result.get("status") == "open":
                on_result(
                    "info",
                    "option shadow opened ticker=%s algo=%s contract=%s %s %s "
                    "ask=%.4f"
                    % (
                        ticker,
                        algo,
                        result.get("expiration_date"),
                        result.get("option_type"),
                        result.get("strike_price"),
                        float(result["entry_ask"]),
                    ),
                )
            else:
                on_result(
                    "error",
                    "option shadow entry failed ticker=%s algo=%s: %s"
                    % (ticker, algo, _clean(result.get("error"))),
                )
            continue

        if action != "exit_all":
            continue
        try:
            positions = tuple(open_positions(record))
        except BaseException as exc:
            on_result(
                "error",
                "option shadow state failed ticker=%s algo=%s action=exit: %s: %s"
                % (ticker, algo, type(exc).__name__, _clean(exc)),
            )
            continue
        if not positions:
            on_result(
                "info",
                "option shadow close skipped ticker=%s algo=%s: no open shadow"
                % (ticker, algo),
            )
            continue
        payload, error = _worker(
            {"action": "exit", "positions": list(positions)}, config_path
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if error or not isinstance(results, list):
            results = _exit_errors(
                positions, error or "worker returned no exit results"
            )
        try:
            on_exits(record, results)
        except BaseException as exc:
            on_result(
                "error",
                "option shadow state failed ticker=%s algo=%s action=exit: %s: %s"
                % (ticker, algo, type(exc).__name__, _clean(exc)),
            )
            continue
        closed = sum(result.get("status") == "closed" for result in results)
        failures = len(results) - closed
        on_result(
            "info" if not failures else "error",
            "option shadow closed ticker=%s algo=%s priced=%d errors=%d"
            % (ticker, algo, closed, failures),
        )
