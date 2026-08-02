"""Provider workbench handler and self-contained HTML page."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import yaml

from trader.console.nav import render_nav_html
from trader.console.styles import BASE_CSS


OPERATIONS = frozenset({"bars_1m", "bars_1d", "signal", "event", "calendar"})
EVENT_KINDS = frozenset({"implied_move_pct", "earnings_proximity"})
CALENDAR_OPS = frozenset({"is_session", "prev_session", "session_close"})


class _BadRequest(Exception):
    def __init__(self, operation: str | None, params: dict[str, object], message: str):
        super().__init__(message)
        self.operation = operation
        self.params = params
        self.message = message


def handle_query(
    params: dict[str, str],
    *,
    data_root: Path,
    config_dir: Path,
) -> tuple[int, dict]:
    """Run one provider workbench operation and return an HTTP-ready envelope."""
    try:
        operation, parsed, call = _parse_request(params)
    except _BadRequest as exc:
        return _response(
            400,
            outcome="error",
            operation=exc.operation,
            params=exc.params,
            data=None,
            message=exc.message,
        )

    try:
        ProviderMarketData, registry, LookaheadError = _load_provider_runtime()
    except ImportError as exc:
        return _response(
            200,
            outcome="error",
            operation=operation,
            params=parsed,
            data=None,
            message=f"provider workbench unavailable: {exc}",
        )

    if operation == "signal":
        name = str(parsed["name"])
        if name not in registry:
            return _response(
                400,
                outcome="error",
                operation=operation,
                params=parsed,
                data=None,
                message=(
                    f"unknown signal {name!r}; expected one of "
                    f"{', '.join(sorted(registry))}"
                ),
            )

    try:
        primary_symbol = _read_primary_symbol(config_dir)
        market = ProviderMarketData(
            data_root,
            calendar_path=config_dir / "calendar.yaml",
            primary_symbol=primary_symbol,
        )
        data = call(market, registry)
    except LookaheadError as exc:
        return _response(
            200,
            outcome="lookahead_refused",
            operation=operation,
            params=parsed,
            data=None,
            message=str(exc),
        )
    except Exception as exc:  # Keep the HTTP boundary traceback-free.
        return _response(
            200,
            outcome="error",
            operation=operation,
            params=parsed,
            data=None,
            message=str(exc),
        )

    return _response(
        200,
        outcome="ok",
        operation=operation,
        params=parsed,
        data=data,
        message=None,
    )


def _parse_request(
    params: dict[str, str],
) -> tuple[str, dict[str, object], Callable[[Any, dict[str, Any]], object]]:
    operation = params.get("operation")
    if operation is None or operation == "":
        raise _BadRequest(None, {}, "missing required param 'operation'")
    parsed: dict[str, object] = {"operation": operation}

    if operation not in OPERATIONS:
        raise _BadRequest(
            operation,
            parsed,
            f"unknown operation {operation!r}; expected one of {', '.join(sorted(OPERATIONS))}",
        )

    if operation == "bars_1m":
        symbol = _required(params, "symbol", operation, parsed)
        parsed["symbol"] = symbol
        asof = _parse_asof(
            _required(params, "asof", operation, parsed),
            "asof",
            operation,
            parsed,
        )
        lookback_minutes = _parse_int(
            params.get("lookback_minutes"),
            field="lookback_minutes",
            default=30,
            maximum=2000,
            operation=operation,
            parsed=parsed,
        )
        parsed.update(
            {
                "asof": _format_datetime(asof),
                "lookback_minutes": lookback_minutes,
            }
        )

        def call(market: Any, _registry: dict[str, Any]) -> object:
            frame = market.bars_1m(
                symbol,
                asof=asof,
                lookback_minutes=lookback_minutes,
            )
            return _bar_rows(frame, ts_kind="datetime")

        return operation, parsed, call

    if operation == "bars_1d":
        symbol = _required(params, "symbol", operation, parsed)
        parsed["symbol"] = symbol
        asof = _parse_day(
            _required(params, "asof", operation, parsed),
            "asof",
            operation,
            parsed,
        )
        lookback_days = _parse_int(
            params.get("lookback_days"),
            field="lookback_days",
            default=10,
            maximum=60,
            operation=operation,
            parsed=parsed,
        )
        parsed.update(
            {
                "asof": asof.isoformat(),
                "lookback_days": lookback_days,
            }
        )

        def call(market: Any, _registry: dict[str, Any]) -> object:
            frame = market.bars_1d(
                symbol,
                asof=asof,
                lookback_days=lookback_days,
            )
            return _bar_rows(frame, ts_kind="date")

        return operation, parsed, call

    if operation == "signal":
        name = _required(params, "name", operation, parsed)
        parsed["name"] = name
        asof = _parse_asof(
            _required(params, "asof", operation, parsed),
            "asof",
            operation,
            parsed,
        )
        parsed["asof"] = _format_datetime(asof)

        def call(market: Any, registry: dict[str, Any]) -> object:
            spec = registry[name]
            return {
                "name": name,
                "value": float(market.signal(name, asof=asof)),
                "description": spec.description,
                "units": spec.units,
            }

        return operation, parsed, call

    if operation == "event":
        kind = _required(params, "kind", operation, parsed)
        parsed["kind"] = kind
        asof = _parse_asof(
            _required(params, "asof", operation, parsed),
            "asof",
            operation,
            parsed,
        )
        parsed["asof"] = _format_datetime(asof)
        if kind not in EVENT_KINDS:
            raise _BadRequest(
                operation,
                parsed,
                f"unknown event kind {kind!r}; expected one of {', '.join(sorted(EVENT_KINDS))}",
            )

        def call(market: Any, _registry: dict[str, Any]) -> object:
            return market.event(kind, asof=asof)

        return operation, parsed, call

    calendar_op = _required(params, "calendar_op", operation, parsed)
    parsed["calendar_op"] = calendar_op
    day = _parse_day(
        _required(params, "day", operation, parsed),
        "day",
        operation,
        parsed,
    )
    parsed["day"] = day.isoformat()
    if calendar_op not in CALENDAR_OPS:
        raise _BadRequest(
            operation,
            parsed,
            (
                f"unknown calendar_op {calendar_op!r}; expected one of "
                f"{', '.join(sorted(CALENDAR_OPS))}"
            ),
        )

    def call(market: Any, _registry: dict[str, Any]) -> object:
        calendar = market.calendar()
        if calendar_op == "is_session":
            return bool(calendar.is_session(day))
        if calendar_op == "prev_session":
            previous = calendar.prev_session(day)
            return None if previous is None else previous.isoformat()
        close = calendar.session_close(day)
        return _format_datetime(close)

    return operation, parsed, call


def _required(
    params: dict[str, str],
    key: str,
    operation: str,
    parsed: dict[str, object],
) -> str:
    value = params.get(key)
    if value is None or value == "":
        raise _BadRequest(
            operation,
            parsed.copy(),
            f"missing required param {key!r} for operation {operation!r}",
        )
    return value


def _parse_asof(
    value: str,
    field: str,
    operation: str,
    parsed: dict[str, object],
) -> datetime:
    try:
        parsed_dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _BadRequest(
            operation,
            parsed.copy(),
            f"unparseable {field}: {value!r}",
        ) from None
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    return parsed_dt.astimezone(timezone.utc)


def _parse_day(
    value: str,
    field: str,
    operation: str,
    parsed: dict[str, object],
) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise _BadRequest(
            operation,
            parsed.copy(),
            f"unparseable {field}: {value!r}",
        ) from None


def _parse_int(
    value: str | None,
    *,
    field: str,
    default: int,
    maximum: int,
    operation: str,
    parsed: dict[str, object],
) -> int:
    if value is None or value == "":
        return default
    try:
        parsed_value = int(value)
    except ValueError:
        raise _BadRequest(
            operation,
            parsed.copy(),
            f"unparseable {field}: {value!r}",
        ) from None
    if parsed_value < 1:
        raise _BadRequest(
            operation,
            parsed.copy(),
            f"{field} must be at least 1",
        )
    return min(parsed_value, maximum)


def _read_primary_symbol(config_dir: Path) -> str:
    trader_path = Path(config_dir) / "trader.yaml"
    values = yaml.safe_load(trader_path.read_text(encoding="utf-8")) or {}
    if not isinstance(values, dict) or "primary_symbol" not in values:
        raise KeyError(f"missing required key 'primary_symbol' in {trader_path}")
    return str(values["primary_symbol"])


def _load_provider_runtime() -> tuple[Any, dict[str, Any], type[Exception]]:
    from trader.provider.market import ProviderMarketData
    from trader.provider.signals import REGISTRY
    from trader.contracts.errors import LookaheadError

    return ProviderMarketData, REGISTRY, LookaheadError


def _bar_rows(frame: Any, *, ts_kind: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index_value, row in frame.iterrows():
        if ts_kind == "date":
            ts = index_value.date().isoformat()
        else:
            timestamp = (
                index_value.to_pydatetime()
                if hasattr(index_value, "to_pydatetime")
                else index_value
            )
            ts = _format_datetime(timestamp)
        rows.append(
            {
                "ts": ts,
                "o": float(row["o"]),
                "h": float(row["h"]),
                "l": float(row["l"]),
                "c": float(row["c"]),
                "v": float(row["v"]),
            }
        )
    return rows


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _response(
    status: int,
    *,
    outcome: str,
    operation: str | None,
    params: dict[str, object],
    data: object,
    message: str | None,
) -> tuple[int, dict]:
    return (
        status,
        {
            "outcome": outcome,
            "operation": operation,
            "params": params,
            "data": data,
            "message": message,
        },
    )


def render_provider_workbench_html() -> str:
    """Return the provider workbench as one self-contained document."""
    signal_control = _render_signal_control_html()
    return (
        _PROVIDER_WORKBENCH_HTML.replace("__BASE_CSS__", BASE_CSS)
        .replace("__NAV_HTML__", render_nav_html("/workbench/provider"))
        .replace("__SIGNAL_CONTROL__", signal_control)
    )


def _render_signal_control_html() -> str:
    try:
        _ProviderMarketData, registry, _LookaheadError = _load_provider_runtime()
    except Exception:
        return (
            '<input id="signal-name" name="name" type="text" '
            'autocomplete="off" value="price">'
        )

    options = "\n".join(
        f'              <option value="{escape(name, quote=True)}">'
        f"{escape(name)}</option>"
        for name in sorted(registry)
    )
    return f'<select id="signal-name" name="name">\n{options}\n            </select>'


_PROVIDER_WORKBENCH_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>trader.console provider workbench</title>
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

    .field-group {
      display: grid;
      gap: 10px;
    }

    label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
    }

    input,
    select,
    button {
      width: 100%;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      background: #0d141a;
      color: var(--text);
      font: inherit;
    }

    input,
    select {
      min-height: 36px;
      padding: 7px 9px;
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

    #result-message {
      padding: 10px;
      border: 1px solid var(--panel-edge);
      border-radius: 8px;
      color: var(--muted);
      background: #0d141a;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    #result-message.ok {
      border-color: var(--accent);
      color: var(--text);
    }

    #result-message.lookahead_refused {
      border-color: var(--warning);
      color: var(--warning);
    }

    #result-message.error {
      border-color: var(--danger);
      color: var(--danger);
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
      <section class="panel" aria-labelledby="provider-form-heading">
        <h2 id="provider-form-heading">Provider</h2>
        <form id="provider-form">
          <label for="operation">
            Operation
            <select id="operation" name="operation">
              <option value="bars_1m">bars_1m</option>
              <option value="bars_1d">bars_1d</option>
              <option value="signal">signal</option>
              <option value="event">event</option>
              <option value="calendar">calendar</option>
            </select>
          </label>

          <div class="field-group" data-operations="bars_1m bars_1d">
            <label for="symbol">
              Symbol
              <input id="symbol" name="symbol" type="text" autocomplete="off" value="SNDK">
            </label>
          </div>

          <div class="field-group" data-operations="bars_1m">
            <label for="asof-1m">
              As-of
              <input id="asof-1m" name="asof" type="text" value="2026-07-31T14:35:00Z">
            </label>
            <label for="lookback-minutes">
              Lookback minutes
              <input id="lookback-minutes" name="lookback_minutes" type="number" min="1" max="2000" value="30">
            </label>
          </div>

          <div class="field-group" data-operations="bars_1d" hidden>
            <label for="asof-1d">
              As-of
              <input id="asof-1d" name="asof" type="date" value="2026-07-31">
            </label>
            <label for="lookback-days">
              Lookback days
              <input id="lookback-days" name="lookback_days" type="number" min="1" max="60" value="10">
            </label>
          </div>

          <div class="field-group" data-operations="signal" hidden>
            <label for="signal-name">
              Signal
              __SIGNAL_CONTROL__
            </label>
            <label for="asof-signal">
              As-of
              <input id="asof-signal" name="asof" type="text" value="2026-07-31T14:35:00Z">
            </label>
          </div>

          <div class="field-group" data-operations="event" hidden>
            <label for="event-kind">
              Event
              <select id="event-kind" name="kind">
                <option value="implied_move_pct">implied_move_pct</option>
                <option value="earnings_proximity">earnings_proximity</option>
              </select>
            </label>
            <label for="asof-event">
              As-of
              <input id="asof-event" name="asof" type="text" value="2026-07-31T14:35:00Z">
            </label>
          </div>

          <div class="field-group" data-operations="calendar" hidden>
            <label for="calendar-op">
              Calendar op
              <select id="calendar-op" name="calendar_op">
                <option value="is_session">is_session</option>
                <option value="prev_session">prev_session</option>
                <option value="session_close">session_close</option>
              </select>
            </label>
            <label for="calendar-day">
              Day
              <input id="calendar-day" name="day" type="date" value="2026-07-31">
            </label>
          </div>

          <button id="submit" type="submit">Run</button>
        </form>
      </section>

      <section class="panel result-stack" aria-labelledby="result-heading">
        <h2 id="result-heading">Result</h2>
        <div id="result-outcome" aria-live="polite">idle</div>
        <div id="result-message" hidden></div>
        <div class="table-wrap" id="result-table-wrap" hidden>
          <table id="result-table"></table>
        </div>
        <pre id="result-raw">{}</pre>
      </section>
    </div>
  </main>

  <script>
    (() => {
      "use strict";

      const form = document.getElementById("provider-form");
      const operation = document.getElementById("operation");
      const submit = document.getElementById("submit");
      const groups = Array.from(document.querySelectorAll("[data-operations]"));
      const outcome = document.getElementById("result-outcome");
      const message = document.getElementById("result-message");
      const tableWrap = document.getElementById("result-table-wrap");
      const table = document.getElementById("result-table");
      const raw = document.getElementById("result-raw");

      function escapeHtml(value) {
        const node = document.createElement("span");
        node.textContent = value == null ? "" : String(value);
        return node.innerHTML;
      }

      function formatValue(value) {
        if (value == null) return "";
        if (typeof value === "object") return JSON.stringify(value);
        return String(value);
      }

      function syncFields() {
        const selected = operation.value;
        for (const group of groups) {
          const active = group.dataset.operations.split(" ").includes(selected);
          group.hidden = !active;
          for (const control of group.querySelectorAll("input, select")) {
            control.disabled = !active;
          }
        }
      }

      function renderList(rows) {
        if (!rows.length) {
          tableWrap.hidden = true;
          message.hidden = false;
          message.className = "ok";
          message.textContent = "No rows returned.";
          return;
        }
        const columns = Array.from(rows.reduce((set, row) => {
          Object.keys(row || {}).forEach((key) => set.add(key));
          return set;
        }, new Set()));
        table.innerHTML = "<thead><tr>" + columns.map((column) =>
          `<th>${escapeHtml(column)}</th>`).join("") + "</tr></thead><tbody>" +
          rows.map((row) => "<tr>" + columns.map((column) =>
            `<td>${escapeHtml(formatValue(row[column]))}</td>`).join("") +
          "</tr>").join("") + "</tbody>";
        tableWrap.hidden = false;
      }

      function renderKeyValue(data) {
        const entries = data && typeof data === "object" && !Array.isArray(data)
          ? Object.entries(data)
          : [["value", data]];
        table.innerHTML = "<tbody>" + entries.map(([key, value]) =>
          `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(formatValue(value))}</td></tr>`
        ).join("") + "</tbody>";
        tableWrap.hidden = false;
      }

      function renderPayload(payload) {
        const state = payload && payload.outcome ? payload.outcome : "error";
        raw.textContent = JSON.stringify(payload, null, 2);
        outcome.textContent = state;
        outcome.className = state;
        table.innerHTML = "";
        tableWrap.hidden = true;
        message.hidden = true;
        message.className = "";
        message.textContent = "";

        if (state !== "ok") {
          message.hidden = false;
          message.className = state;
          message.textContent = payload.message || state;
          return;
        }

        if (payload.data == null) {
          message.hidden = false;
          message.className = "ok";
          message.textContent = "OK. No data exists for that as-of.";
          return;
        }

        if (Array.isArray(payload.data)) renderList(payload.data);
        else renderKeyValue(payload.data);
      }

      async function runQuery(event) {
        event.preventDefault();
        syncFields();
        submit.disabled = true;
        outcome.textContent = "running";
        outcome.className = "";
        message.hidden = true;
        tableWrap.hidden = true;
        raw.textContent = "{}";

        try {
          const query = new URLSearchParams(new FormData(form));
          const response = await fetch("/api/workbench/provider?" + query.toString());
          const payload = await response.json();
          renderPayload(payload);
        } catch (error) {
          renderPayload({
            outcome: "error",
            operation: operation.value,
            params: {},
            data: null,
            message: String(error),
          });
        } finally {
          submit.disabled = false;
        }
      }

      operation.addEventListener("change", syncFields);
      form.addEventListener("submit", runQuery);
      syncFields();
    })();
  </script>
</body>
</html>
"""


__all__ = ["handle_query", "render_provider_workbench_html"]
