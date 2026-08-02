"""Execution workbench handler and self-contained HTML page."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import yaml

from trader.console.nav import render_nav_html
from trader.console.styles import BASE_CSS


OPERATION = "check_and_size"
SIDES = frozenset({"long", "short"})


@dataclass(frozen=True)
class _ParsedRequest:
    params: dict[str, object]
    ts: datetime
    muted_until: datetime | None
    equity: float | None
    capital_fraction: float | None
    day_slots: int | None


@dataclass(frozen=True)
class _Runtime:
    ProviderMarketData: Any
    LookaheadError: type[Exception]
    Intent: Any
    OrderTicket: Any
    PortfolioState: Any
    PositionState: Any
    Rejection: Any
    AccountConfig: Any
    RailsConfig: Any
    DrawdownStopConfig: Any
    RiskConfig: Any
    FillsConfig: Any
    ExecutionConfig: Any
    RiskRails: Any


class _BadRequest(Exception):
    def __init__(self, params: dict[str, object], message: str):
        super().__init__(message)
        self.params = params
        self.message = message


def handle_query(
    params: dict[str, str],
    *,
    data_root: Path,
    config_dir: Path,
) -> tuple[int, dict]:
    """Run one manual intent through the real risk rails in simulation."""
    try:
        parsed_request = _parse_request(params)
    except _BadRequest as exc:
        return _response(
            400,
            outcome="error",
            params=exc.params,
            data=None,
            message=exc.message,
        )

    parsed = parsed_request.params
    try:
        runtime = _load_execution_runtime()
    except ImportError as exc:
        return _response(
            200,
            outcome="error",
            params=parsed,
            data=None,
            message=f"execution workbench unavailable: {exc}",
        )

    try:
        primary_symbol = _read_primary_symbol(config_dir)
        risk_config = _read_risk_config(config_dir, parsed_request, runtime)
        execution_config = _read_execution_config(config_dir, runtime)
        parsed = _with_config_defaults(parsed, primary_symbol, risk_config.account)
        market = runtime.ProviderMarketData(
            data_root,
            calendar_path=config_dir / "calendar.yaml",
            primary_symbol=primary_symbol,
        )
        risk = runtime.RiskRails(risk_config, execution_config)
        intent = runtime.Intent(
            algo_id=str(parsed["algo_id"]),
            ts=parsed_request.ts,
            action="open",
            side=parsed["side"],
            signal_symbol=primary_symbol,
            instrument=str(parsed["instrument"]),
            entry="market_next_open",
            stop=float(parsed["stop"]),
            target=float(parsed["target"]),
            confidence=parsed["confidence"],
            reason=str(parsed["reason"]),
            meta={},
        )
        portfolio = runtime.PortfolioState(
            cash=float(parsed["equity"]),
            equity=float(parsed["equity"]),
            positions=_position_states(
                runtime.PositionState,
                parsed["positions"],
                parsed_request.ts,
            ),
            entries_today=int(parsed["entries_today"]),
            realized_r_today=float(parsed["realized_r_today"]),
            muted_until=parsed_request.muted_until,
        )
        result = risk.check_and_size(intent, portfolio, market)
        if _is_no_price_data(result, runtime):
            _raise_lookahead_if_stale(
                market,
                str(parsed["instrument"]),
                parsed_request.ts,
            )
    except runtime.LookaheadError as exc:
        return _response(
            200,
            outcome="lookahead_refused",
            params=parsed,
            data=None,
            message=str(exc),
        )
    except Exception as exc:  # Keep the HTTP boundary traceback-free.
        return _response(
            200,
            outcome="error",
            params=parsed,
            data=None,
            message=str(exc),
        )

    context = _sizing_context(risk_config.account)
    if isinstance(result, runtime.OrderTicket):
        data = {
            "decision": "accepted",
            "ticket": _ticket_data(result),
            "rejection": None,
            "sizing_context": context,
        }
    else:
        data = {
            "decision": "rejected",
            "ticket": None,
            "rejection": _rejection_data(result),
            "sizing_context": context,
        }

    return _response(
        200,
        outcome="ok",
        params=parsed,
        data=data,
        message=None,
    )


def _parse_request(params: dict[str, str]) -> _ParsedRequest:
    parsed: dict[str, object] = {
        "algo_id": _optional_text(params, "algo_id", "workbench-manual"),
        "action": "open",
    }
    side = _required(params, "side", parsed).strip().lower()
    parsed["side"] = side
    if side not in SIDES:
        raise _BadRequest(
            parsed.copy(),
            "side must be one of 'long' or 'short' for operation 'check_and_size'",
        )

    instrument = _required(params, "instrument", parsed).strip()
    if instrument == "":
        raise _BadRequest(
            parsed.copy(),
            "missing required param 'instrument' for operation 'check_and_size'",
        )
    parsed["instrument"] = instrument

    raw_ts = _required(params, "ts", parsed).strip()
    ts = _parse_datetime(raw_ts, "ts", parsed)
    parsed.update(
        {
            "ts": _format_datetime(ts),
            "entry": "market_next_open",
            "stop": _parse_required_float(params, "stop", parsed),
            "target": _parse_required_float(params, "target", parsed),
            "confidence": _parse_optional_float(params, "confidence", parsed),
            "reason": _optional_text(params, "reason", "manual workbench entry"),
            "meta": {},
        }
    )

    muted_until = _parse_optional_datetime(params, "muted_until", parsed)
    parsed.update(
        {
            "entries_today": _parse_optional_int(
                params,
                "entries_today",
                parsed,
                default=0,
                minimum=0,
            ),
            "positions": _parse_positions(params.get("positions")),
            "muted_until": (
                None if muted_until is None else _format_datetime(muted_until)
            ),
            "realized_r_today": _parse_optional_float(
                params,
                "realized_r_today",
                parsed,
                default=0.0,
            ),
        }
    )

    return _ParsedRequest(
        params=parsed,
        ts=ts,
        muted_until=muted_until,
        equity=_parse_optional_float(params, "equity", parsed),
        capital_fraction=_parse_optional_float(params, "capital_fraction", parsed),
        day_slots=_parse_optional_int(
            params,
            "day_slots",
            parsed,
            default=None,
            minimum=1,
        ),
    )


def _required(
    params: dict[str, str],
    key: str,
    parsed: dict[str, object],
) -> str:
    value = params.get(key)
    if value is None or value == "":
        raise _BadRequest(
            parsed.copy(),
            f"missing required param {key!r} for operation {OPERATION!r}",
        )
    return value


def _optional_text(params: dict[str, str], key: str, default: str) -> str:
    value = params.get(key)
    if value is None or value == "":
        return default
    return value.strip()


def _parse_datetime(
    value: str,
    field: str,
    parsed: dict[str, object],
) -> datetime:
    try:
        parsed_dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _BadRequest(parsed.copy(), f"unparseable {field}: {value!r}") from None
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    return parsed_dt.astimezone(timezone.utc)


def _parse_optional_datetime(
    params: dict[str, str],
    field: str,
    parsed: dict[str, object],
) -> datetime | None:
    value = params.get(field)
    if value is None or value == "":
        return None
    return _parse_datetime(value, field, parsed)


def _parse_required_float(
    params: dict[str, str],
    field: str,
    parsed: dict[str, object],
) -> float:
    return _parse_float(_required(params, field, parsed), field, parsed)


def _parse_optional_float(
    params: dict[str, str],
    field: str,
    parsed: dict[str, object],
    *,
    default: float | None = None,
) -> float | None:
    value = params.get(field)
    if value is None or value == "":
        return default
    return _parse_float(value, field, parsed)


def _parse_float(
    value: str,
    field: str,
    parsed: dict[str, object],
) -> float:
    try:
        return float(value)
    except ValueError:
        raise _BadRequest(parsed.copy(), f"unparseable {field}: {value!r}") from None


def _parse_optional_int(
    params: dict[str, str],
    field: str,
    parsed: dict[str, object],
    *,
    default: int | None,
    minimum: int,
) -> int | None:
    value = params.get(field)
    if value is None or value == "":
        return default
    try:
        parsed_value = int(value)
    except ValueError:
        raise _BadRequest(parsed.copy(), f"unparseable {field}: {value!r}") from None
    if parsed_value < minimum:
        raise _BadRequest(parsed.copy(), f"{field} must be at least {minimum}")
    return parsed_value


def _parse_positions(value: str | None) -> list[str]:
    if value is None or value == "":
        return []
    return [symbol for symbol in (part.strip() for part in value.split(",")) if symbol]


def _load_execution_runtime() -> _Runtime:
    from trader.provider.market import ProviderMarketData
    from trader.contracts.errors import LookaheadError
    from trader.contracts import (
        Intent,
        OrderTicket,
        PortfolioState,
        PositionState,
        Rejection,
    )
    from trader.execution.config import (
        AccountConfig,
        DrawdownStopConfig,
        ExecutionConfig,
        FillsConfig,
        RailsConfig,
        RiskConfig,
    )
    from trader.execution.risk import RiskRails

    return _Runtime(
        ProviderMarketData=ProviderMarketData,
        LookaheadError=LookaheadError,
        Intent=Intent,
        OrderTicket=OrderTicket,
        PortfolioState=PortfolioState,
        PositionState=PositionState,
        Rejection=Rejection,
        AccountConfig=AccountConfig,
        RailsConfig=RailsConfig,
        DrawdownStopConfig=DrawdownStopConfig,
        RiskConfig=RiskConfig,
        FillsConfig=FillsConfig,
        ExecutionConfig=ExecutionConfig,
        RiskRails=RiskRails,
    )


def _read_primary_symbol(config_dir: Path) -> str:
    trader_path = Path(config_dir) / "trader.yaml"
    values = yaml.safe_load(trader_path.read_text(encoding="utf-8")) or {}
    if not isinstance(values, dict) or "primary_symbol" not in values:
        raise KeyError(f"missing required key 'primary_symbol' in {trader_path}")
    return str(values["primary_symbol"])


def _read_risk_config(
    config_dir: Path,
    parsed_request: _ParsedRequest,
    runtime: _Runtime,
) -> Any:
    risk_path = Path(config_dir) / "risk.yaml"
    values = yaml.safe_load(risk_path.read_text(encoding="utf-8")) or {}
    if not isinstance(values, dict):
        raise ValueError(f"{risk_path} must contain a mapping")

    account_values = _mapping_value(values, "account", risk_path)
    rails_values = _mapping_value(values, "rails", risk_path)
    drawdown_stop_values = _mapping_value(values, "drawdown_stop", risk_path)
    if parsed_request.equity is not None:
        account_values["equity"] = parsed_request.equity
    if parsed_request.capital_fraction is not None:
        account_values["capital_fraction"] = parsed_request.capital_fraction
    if parsed_request.day_slots is not None:
        account_values["day_slots"] = parsed_request.day_slots

    account = runtime.AccountConfig(
        equity=float(account_values["equity"]),
        capital_fraction=float(account_values["capital_fraction"]),
        day_slots=int(account_values["day_slots"]),
    )
    return runtime.RiskConfig(
        account=account,
        rails=runtime.RailsConfig(**rails_values),
        drawdown_stop=runtime.DrawdownStopConfig(**drawdown_stop_values),
    )


def _read_execution_config(config_dir: Path, runtime: _Runtime) -> Any:
    execution_path = Path(config_dir) / "execution.yaml"
    values = yaml.safe_load(execution_path.read_text(encoding="utf-8")) or {}
    if not isinstance(values, dict):
        raise ValueError(f"{execution_path} must contain a mapping")

    execution_values = dict(values)
    execution_values["fills"] = runtime.FillsConfig(
        **_mapping_value(values, "fills", execution_path)
    )
    execution_values["slippage_bps"] = _normalize_slippage_bps(
        _mapping_value(values, "slippage_bps", execution_path)
    )
    return _workbench_execution_config(
        runtime.ExecutionConfig(**execution_values),
        runtime,
    )


def _workbench_execution_config(execution_config: Any, runtime: _Runtime) -> Any:
    fills = runtime.FillsConfig(
        commission=execution_config.fills.commission,
        etf_price_basis="real",
        min_intraday_bars=1,
    )
    return replace(execution_config, fills=fills)


def _normalize_slippage_bps(
    values: dict[str, object],
) -> dict[str, dict[str, object]]:
    normalized: dict[str, dict[str, object]] = {}
    for symbol, monthly_values in values.items():
        if not isinstance(monthly_values, dict):
            raise ValueError(f"slippage_bps for {symbol!r} must contain a mapping")
        normalized[str(symbol)] = {
            str(month): value for month, value in monthly_values.items()
        }
    return normalized


def _is_no_price_data(result: Any, runtime: _Runtime) -> bool:
    return isinstance(result, runtime.Rejection) and result.rule == "no_price_data"


def _raise_lookahead_if_stale(
    market: Any,
    instrument: str,
    ts: datetime,
) -> None:
    market.bars_1m(
        instrument,
        asof=ts,
        lookback_minutes=1,
    )


def _mapping_value(
    values: dict[str, object],
    key: str,
    source: Path,
) -> dict[str, object]:
    value = values.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"missing required mapping {key!r} in {source}")
    return dict(value)


def _with_config_defaults(
    parsed: dict[str, object],
    primary_symbol: str,
    account: Any,
) -> dict[str, object]:
    complete = dict(parsed)
    complete.update(
        {
            "signal_symbol": primary_symbol,
            "equity": float(account.equity),
            "capital_fraction": float(account.capital_fraction),
            "day_slots": int(account.day_slots),
        }
    )
    ordered_keys = [
        "algo_id",
        "action",
        "side",
        "signal_symbol",
        "instrument",
        "ts",
        "entry",
        "stop",
        "target",
        "confidence",
        "reason",
        "meta",
        "equity",
        "capital_fraction",
        "day_slots",
        "entries_today",
        "positions",
        "muted_until",
        "realized_r_today",
    ]
    return {key: complete[key] for key in ordered_keys}


def _position_states(
    PositionState: Any,
    instruments: object,
    entry_ts: datetime,
) -> list[Any]:
    return [
        PositionState(
            instrument=str(instrument),
            side="long",
            shares=0,
            entry_price=0.0,
            entry_ts=entry_ts,
            stop=None,
            target=None,
            algo_id="workbench",
        )
        for instrument in instruments
    ]


def _ticket_data(ticket: Any) -> dict[str, object]:
    return {
        "ticket_id": ticket.ticket_id,
        "algo_id": ticket.algo_id,
        "instrument": ticket.instrument,
        "side": ticket.side,
        "shares": ticket.shares,
        "entry": ticket.entry,
        "stop": ticket.stop,
        "target": ticket.target,
        "risk": dict(ticket.risk),
    }


def _rejection_data(rejection: Any) -> dict[str, object]:
    return {
        "rule": rejection.rule,
        "detail": rejection.detail,
    }


def _sizing_context(account: Any) -> dict[str, object]:
    equity = float(account.equity)
    capital_fraction = float(account.capital_fraction)
    day_slots = int(account.day_slots)
    return {
        "equity": equity,
        "capital_fraction": capital_fraction,
        "day_slots": day_slots,
        "slot_capital": equity * capital_fraction / day_slots,
    }


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _response(
    status: int,
    *,
    outcome: str,
    params: dict[str, object],
    data: object,
    message: str | None,
) -> tuple[int, dict]:
    return (
        status,
        {
            "outcome": outcome,
            "operation": OPERATION,
            "params": params,
            "data": data,
            "message": message,
        },
    )


def render_execution_workbench_html(config_dir: Path | None = None) -> str:
    """Return the execution workbench as one self-contained document."""
    defaults = _render_defaults(config_dir)
    return (
        _EXECUTION_WORKBENCH_HTML.replace("__BASE_CSS__", BASE_CSS)
        .replace("__NAV_HTML__", render_nav_html("/workbench/execution"))
        .replace("__EQUITY__", defaults["equity"])
        .replace("__CAPITAL_FRACTION__", defaults["capital_fraction"])
        .replace("__DAY_SLOTS__", defaults["day_slots"])
    )


def _render_defaults(config_dir: Path | None) -> dict[str, str]:
    fallback = {
        "equity": "10000",
        "capital_fraction": "1.0",
        "day_slots": "2",
    }
    if config_dir is None:
        return fallback
    try:
        risk_path = Path(config_dir) / "risk.yaml"
        values = yaml.safe_load(risk_path.read_text(encoding="utf-8")) or {}
        account = values["account"]
        return {
            "equity": escape(str(account["equity"]), quote=True),
            "capital_fraction": escape(str(account["capital_fraction"]), quote=True),
            "day_slots": escape(str(account["day_slots"]), quote=True),
        }
    except Exception:
        return fallback


_EXECUTION_WORKBENCH_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>trader.console execution workbench</title>
  <style>
__BASE_CSS__

    .workbench-grid {
      display: grid;
      grid-template-columns: 390px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }

    form {
      display: grid;
      gap: 12px;
    }

    fieldset {
      display: grid;
      gap: 10px;
      margin: 0;
      padding: 12px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
    }

    legend {
      padding: 0 4px;
      color: var(--accent);
      font-size: 12px;
    }

    label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
    }

    input,
    select,
    textarea,
    button {
      width: 100%;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      background: #0d141a;
      color: var(--text);
      font: inherit;
    }

    input,
    select,
    textarea {
      min-height: 36px;
      padding: 7px 9px;
    }

    textarea {
      min-height: 68px;
      resize: vertical;
    }

    .fixed-field {
      min-height: 36px;
      padding: 7px 9px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      color: var(--text);
      background: #0d141a;
    }

    button {
      min-height: 38px;
      cursor: pointer;
      border-color: var(--accent);
      background: rgba(97, 214, 169, 0.12);
      color: var(--accent);
    }

    button:hover,
    button:focus {
      background: rgba(97, 214, 169, 0.18);
      outline: none;
    }

    .result-stack {
      display: grid;
      gap: 12px;
    }

    #result-outcome {
      min-height: 28px;
      padding: 5px 8px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      color: var(--muted);
    }

    #result-outcome.ok {
      border-color: var(--accent);
      color: var(--accent);
      background: rgba(97, 214, 169, 0.08);
    }

    #result-outcome.lookahead_refused {
      border-color: var(--warning);
      color: var(--warning);
      background: rgba(240, 195, 108, 0.08);
    }

    #result-outcome.error {
      border-color: var(--danger);
      color: var(--danger);
      background: rgba(255, 123, 121, 0.08);
    }

    #result-message,
    .decision-card {
      padding: 10px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      color: var(--muted);
      background: #0d141a;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    #result-message.lookahead_refused {
      border-color: var(--warning);
      color: var(--warning);
    }

    #result-message.error {
      border-color: var(--danger);
      color: var(--danger);
    }

    .decision-card.accepted {
      border-color: var(--accent);
      color: var(--text);
      background: rgba(97, 214, 169, 0.06);
    }

    .decision-card.rejected {
      border-color: var(--warning);
      color: #f7dfac;
      background: rgba(240, 195, 108, 0.06);
    }

    .decision-title {
      margin-bottom: 8px;
      color: var(--text);
      font-weight: 700;
    }

    #result-raw {
      min-height: 220px;
      max-height: 420px;
      margin: 0;
      overflow: auto;
      padding: 10px;
      border: 1px solid var(--panel-edge);
      background: #0d141a;
      color: var(--text);
      white-space: pre-wrap;
    }

    @media (max-width: 900px) {
      main { padding: 14px; }
      header { align-items: flex-start; flex-direction: column; }
      .workbench-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>trader.console</h1>
    </header>

    __NAV_HTML__

    <div class="workbench-grid">
      <section class="panel" aria-labelledby="execution-form-heading">
        <h2 id="execution-form-heading">Execution</h2>
        <form id="execution-form">
          <fieldset>
            <legend>Intent</legend>
            <label for="algo-id">
              Algo
              <input id="algo-id" name="algo_id" type="text" autocomplete="off" value="workbench-manual">
            </label>
            <label for="side">
              Side
              <select id="side" name="side" required>
                <option value="long">long</option>
                <option value="short">short</option>
              </select>
            </label>
            <label for="instrument">
              Instrument
              <input id="instrument" name="instrument" type="text" autocomplete="off" value="SNXX" required>
            </label>
            <label for="ts">
              As-of
              <input id="ts" name="ts" type="text" value="2026-07-31T14:35:00Z" required>
            </label>
            <div id="entry-policy" class="fixed-field">Entry: market_next_open</div>
            <label for="stop">
              Stop
              <input id="stop" name="stop" type="number" step="any" required>
            </label>
            <label for="target">
              Target
              <input id="target" name="target" type="number" step="any" required>
            </label>
            <label for="confidence">
              Confidence
              <input id="confidence" name="confidence" type="number" step="any">
            </label>
            <label for="reason">
              Reason
              <textarea id="reason" name="reason">manual workbench entry</textarea>
            </label>
          </fieldset>

          <fieldset>
            <legend>Account / Portfolio</legend>
            <label for="equity">
              Equity
              <input id="equity" name="equity" type="number" step="any" value="__EQUITY__">
            </label>
            <label for="capital-fraction">
              Capital fraction
              <input id="capital-fraction" name="capital_fraction" type="number" step="any" value="__CAPITAL_FRACTION__">
            </label>
            <label for="day-slots">
              Day slots
              <input id="day-slots" name="day_slots" type="number" min="1" step="1" value="__DAY_SLOTS__">
            </label>
            <label for="entries-today">
              Entries today
              <input id="entries-today" name="entries_today" type="number" min="0" step="1" value="0">
            </label>
            <label for="positions">
              Positions
              <input id="positions" name="positions" type="text" autocomplete="off">
            </label>
            <label for="muted-until">
              Muted until
              <input id="muted-until" name="muted_until" type="text">
            </label>
            <label for="realized-r-today">
              Realized R today
              <input id="realized-r-today" name="realized_r_today" type="number" step="any" value="0">
            </label>
          </fieldset>

          <button id="submit" type="submit">Run</button>
        </form>
      </section>

      <section class="panel result-stack" aria-labelledby="result-heading">
        <h2 id="result-heading">Result</h2>
        <div id="result-outcome" aria-live="polite">idle</div>
        <div id="result-message" hidden></div>
        <div id="result-accepted" class="decision-card accepted" hidden></div>
        <div id="result-rejected" class="decision-card rejected" hidden></div>
        <div class="table-wrap" id="result-context-wrap" hidden>
          <table id="result-context"></table>
        </div>
        <pre id="result-raw">{}</pre>
      </section>
    </div>
  </main>

  <script>
    (() => {
      "use strict";

      const form = document.getElementById("execution-form");
      const submit = document.getElementById("submit");
      const outcome = document.getElementById("result-outcome");
      const message = document.getElementById("result-message");
      const accepted = document.getElementById("result-accepted");
      const rejected = document.getElementById("result-rejected");
      const contextWrap = document.getElementById("result-context-wrap");
      const contextTable = document.getElementById("result-context");
      const raw = document.getElementById("result-raw");

      function escapeHtml(value) {
        const node = document.createElement("span");
        node.textContent = value == null ? "" : String(value);
        return node.innerHTML;
      }

      function formatValue(value) {
        if (value == null) return "";
        if (typeof value === "number") return Number.isInteger(value)
          ? String(value)
          : value.toFixed(4).replace(/0+$/, "").replace(/\\.$/, "");
        if (typeof value === "object") return JSON.stringify(value);
        return String(value);
      }

      function renderKeyValues(table, values) {
        table.innerHTML = "<tbody>" + Object.entries(values || {}).map(([key, value]) =>
          `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(formatValue(value))}</td></tr>`
        ).join("") + "</tbody>";
      }

      function renderPayload(payload) {
        const state = payload && payload.outcome ? payload.outcome : "error";
        const data = payload && payload.data ? payload.data : null;
        raw.textContent = JSON.stringify(payload, null, 2);
        outcome.textContent = state === "ok" && data ? `ok: ${data.decision}` : state;
        outcome.className = state;
        message.hidden = true;
        message.className = "";
        message.textContent = "";
        accepted.hidden = true;
        accepted.innerHTML = "";
        rejected.hidden = true;
        rejected.innerHTML = "";
        contextWrap.hidden = true;
        contextTable.innerHTML = "";

        if (state !== "ok") {
          message.hidden = false;
          message.className = state;
          message.textContent = payload.message || state;
          return;
        }

        if (!data) {
          message.hidden = false;
          message.className = "ok";
          message.textContent = "OK. No risk decision returned.";
          return;
        }

        if (data.decision === "accepted") {
          const ticket = data.ticket || {};
          accepted.hidden = false;
          accepted.innerHTML = `<div class="decision-title">Accepted</div>
            <div>shares: ${escapeHtml(formatValue(ticket.shares))}</div>
            <div>risk dollars: ${escapeHtml(formatValue((ticket.risk || {}).dollars))}</div>
            <div>slot: ${escapeHtml(formatValue((ticket.risk || {}).slot))}</div>`;
        } else {
          const rejection = data.rejection || {};
          rejected.hidden = false;
          rejected.innerHTML = `<div class="decision-title">Rejected</div>
            <div>rule: ${escapeHtml(formatValue(rejection.rule))}</div>
            <div>detail: ${escapeHtml(formatValue(rejection.detail))}</div>`;
        }

        if (data.sizing_context) {
          renderKeyValues(contextTable, data.sizing_context);
          contextWrap.hidden = false;
        }
      }

      async function runQuery(event) {
        event.preventDefault();
        submit.disabled = true;
        outcome.textContent = "running";
        outcome.className = "";
        message.hidden = true;
        accepted.hidden = true;
        rejected.hidden = true;
        contextWrap.hidden = true;
        raw.textContent = "{}";

        try {
          const query = new URLSearchParams(new FormData(form));
          const response = await fetch("/api/workbench/execution?" + query.toString());
          const payload = await response.json();
          renderPayload(payload);
        } catch (error) {
          renderPayload({
            outcome: "error",
            operation: "check_and_size",
            params: {},
            data: null,
            message: String(error),
          });
        } finally {
          submit.disabled = false;
        }
      }

      form.addEventListener("submit", runQuery);
    })();
  </script>
</body>
</html>
"""


__all__ = ["handle_query", "render_execution_workbench_html"]
