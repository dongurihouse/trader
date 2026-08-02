"""Algos workbench handler and self-contained HTML page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import escape
import importlib
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from trader.console.nav import render_nav_html
from trader.console.styles import BASE_CSS


OPERATION = "run_algo"
_ONE_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class _RosterEntry:
    id: str
    factory: str
    status: str
    params: dict[str, Any]


@dataclass(frozen=True)
class _TraderSettings:
    primary_symbol: str
    timezone: str
    rth_open: time
    rth_close: time


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
    """Run one roster algo over one trading day and return an HTTP-ready envelope."""
    try:
        parsed, trading_day = _parse_request(params)
    except _BadRequest as exc:
        return _response(
            400,
            outcome="error",
            params=exc.params,
            data=None,
            message=exc.message,
        )

    try:
        roster = _read_roster(config_dir)
    except Exception as exc:
        return _response(
            200,
            outcome="error",
            params=parsed,
            data=None,
            message=f"algo roster unavailable: {exc}",
        )

    roster_entry = _find_roster_entry(roster, str(parsed["algo_id"]))
    if roster_entry is None:
        return _response(
            400,
            outcome="error",
            params=parsed,
            data=None,
            message=(
                f"unknown algo_id {parsed['algo_id']!r}; expected one of "
                f"{', '.join(entry.id for entry in roster)}"
            ),
        )

    try:
        ProviderMarketData, LookaheadError = _load_provider_runtime()
    except ImportError as exc:
        return _response(
            200,
            outcome="error",
            params=parsed,
            data=None,
            message=f"algos workbench unavailable: {exc}",
        )

    try:
        settings = _read_trader_settings(config_dir)
        market = ProviderMarketData(
            data_root,
            calendar_path=config_dir / "calendar.yaml",
            primary_symbol=settings.primary_symbol,
        )
        algo = _construct_algo(roster_entry)
    except Exception as exc:
        return _response(
            200,
            outcome="error",
            params=parsed,
            data=None,
            message=str(exc),
        )

    try:
        skip_reason = _day_skip_reason(
            market,
            settings.primary_symbol,
            trading_day,
            LookaheadError,
        )
        if skip_reason is not None:
            return _response(
                200,
                outcome="day_skipped",
                params=parsed,
                data=None,
                message=(
                    f"{trading_day.isoformat()} has no previous trading session "
                    "with data; a real backtest session would skip this day "
                    f"(reason: {skip_reason})"
                ),
            )

        algo.warmup(trading_day, market)
        candidates: list[dict[str, object]] = []
        for asof in _iter_rth_minutes(
            trading_day,
            rth_open=settings.rth_open,
            rth_close=settings.rth_close,
            timezone_name=settings.timezone,
        ):
            for intent in algo.on_bar(asof, market) or []:
                candidates.append(_candidate_data(intent))
    except LookaheadError as exc:
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

    return _response(
        200,
        outcome="ok",
        params=parsed,
        data={
            "algo_id": roster_entry.id,
            "status": roster_entry.status,
            "day": trading_day.isoformat(),
            "candidates": candidates,
        },
        message=None,
    )


def _parse_request(params: dict[str, str]) -> tuple[dict[str, object], date]:
    parsed: dict[str, object] = {}
    algo_id = _required(params, "algo_id", parsed).strip()
    parsed["algo_id"] = algo_id

    raw_day = _required(params, "day", parsed).strip()
    try:
        trading_day = date.fromisoformat(raw_day)
    except ValueError:
        raise _BadRequest(parsed.copy(), f"unparseable day: {raw_day!r}") from None
    parsed["day"] = trading_day.isoformat()
    return parsed, trading_day


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


def _read_roster(config_dir: Path) -> list[_RosterEntry]:
    algos_path = Path(config_dir) / "algos.yaml"
    values = yaml.safe_load(algos_path.read_text(encoding="utf-8")) or {}
    roster = values.get("roster") if isinstance(values, dict) else None
    if not isinstance(roster, list):
        raise ValueError(f"missing required list 'roster' in {algos_path}")

    entries: list[_RosterEntry] = []
    for position, raw_entry in enumerate(roster):
        if not isinstance(raw_entry, dict):
            raise ValueError(f"roster entry {position} in {algos_path} is not a mapping")
        params = raw_entry.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"roster entry {position} params must be a mapping")
        entries.append(
            _RosterEntry(
                id=str(_roster_value(raw_entry, "id", position, algos_path)),
                factory=str(_roster_value(raw_entry, "factory", position, algos_path)),
                status=str(_roster_value(raw_entry, "status", position, algos_path)),
                params=params,
            )
        )
    return entries


def _roster_value(
    raw_entry: dict[str, object],
    key: str,
    position: int,
    source: Path,
) -> object:
    if key not in raw_entry:
        raise ValueError(f"roster entry {position} missing {key!r} in {source}")
    return raw_entry[key]


def _find_roster_entry(
    roster: list[_RosterEntry],
    algo_id: str,
) -> _RosterEntry | None:
    for entry in roster:
        if entry.id == algo_id:
            return entry
    return None


def _read_trader_settings(config_dir: Path) -> _TraderSettings:
    trader_path = Path(config_dir) / "trader.yaml"
    values = yaml.safe_load(trader_path.read_text(encoding="utf-8")) or {}
    if not isinstance(values, dict):
        raise ValueError(f"{trader_path} must contain a mapping")
    session = values.get("session")
    if not isinstance(session, dict):
        raise KeyError(f"missing required key 'session' in {trader_path}")
    return _TraderSettings(
        primary_symbol=str(_config_value(values, "primary_symbol", trader_path)),
        timezone=str(_config_value(values, "timezone", trader_path)),
        rth_open=time.fromisoformat(str(_config_value(session, "rth_open", trader_path))),
        rth_close=time.fromisoformat(
            str(_config_value(session, "rth_close", trader_path))
        ),
    )


def _config_value(values: dict[str, object], key: str, source: Path) -> object:
    if key not in values:
        raise KeyError(f"missing required key {key!r} in {source}")
    return values[key]


def _load_provider_runtime() -> tuple[Any, type[Exception]]:
    from trader.provider.market import ProviderMarketData
    from trader.contracts.errors import LookaheadError

    return ProviderMarketData, LookaheadError


def _construct_algo(entry: _RosterEntry) -> Any:
    try:
        if ":" in entry.factory:
            module_name, attribute_name = entry.factory.rsplit(":", 1)
            module = importlib.import_module(module_name)
            factory = getattr(module, attribute_name)
        else:
            module = importlib.import_module("trader.algos")
            factory = getattr(module, entry.factory)
        return factory(id=entry.id, status=entry.status, params=entry.params)
    except Exception as exc:
        raise RuntimeError(
            f"algo factory {entry.factory!r} for {entry.id!r} could not be composed: "
            f"{exc}"
        ) from None


def _day_skip_reason(
    market: Any,
    primary_symbol: str,
    trading_day: date,
    lookahead_error_type: type[Exception],
) -> str | None:
    calendar = market.calendar()
    previous = calendar.prev_session(trading_day)
    if previous is None:
        return "no_prev_session"

    try:
        previous_bars = market.bars_1m(
            primary_symbol,
            asof=calendar.session_close(previous),
            lookback_minutes=1440,
        )
    except lookahead_error_type:
        previous_bars = None
    if previous_bars is None or previous_bars.empty:
        return "no_prev_session"
    return None


def _iter_rth_minutes(
    trading_day: date,
    *,
    rth_open: time,
    rth_close: time,
    timezone_name: str,
) -> list[datetime]:
    local_timezone = ZoneInfo(timezone_name)
    asof = datetime.combine(trading_day, rth_open, tzinfo=local_timezone) + _ONE_MINUTE
    local_close = datetime.combine(trading_day, rth_close, tzinfo=local_timezone)
    if asof > local_close:
        raise ValueError("RTH close must be at least one minute after RTH open")

    minutes: list[datetime] = []
    while asof <= local_close:
        minutes.append(asof.astimezone(timezone.utc))
        asof += _ONE_MINUTE
    return minutes


def _candidate_data(intent: Any) -> dict[str, object]:
    meta = intent.meta or {}
    return {
        "ts": _format_datetime(intent.ts),
        "side": intent.side,
        "action": intent.action,
        "bracket": {
            "instrument": intent.instrument,
            "entry": intent.entry,
            "stop": intent.stop,
            "target": intent.target,
        },
        "confidence": intent.confidence,
        "reason": intent.reason,
        "rule_trace": {
            "setup_id": meta.get("setup_id"),
            "rules_version": meta.get("rules_version"),
            "rules_fired": list(meta.get("rules_fired") or []),
            "direction_votes": list(meta.get("direction_votes") or []),
            "gates_pass": bool(meta.get("gates_pass")),
            "vetoed": meta.get("vetoed"),
            "uncalibrated": bool(meta.get("uncalibrated")),
        },
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


def render_algos_workbench_html(config_dir: Path | None = None) -> str:
    """Return the algos workbench as one self-contained document."""
    algo_control = _render_algo_control_html(config_dir)
    return (
        _ALGOS_WORKBENCH_HTML.replace("__BASE_CSS__", BASE_CSS)
        .replace("__NAV_HTML__", render_nav_html("/workbench/algos"))
        .replace("__ALGO_CONTROL__", algo_control)
    )


def _render_algo_control_html(config_dir: Path | None) -> str:
    if config_dir is None:
        return (
            '<input id="algo-id" name="algo_id" type="text" '
            'autocomplete="off" value="orb5">'
        )
    try:
        roster = _read_roster(config_dir)
    except Exception:
        return (
            '<input id="algo-id" name="algo_id" type="text" '
            'autocomplete="off" value="orb5">'
        )

    options = "\n".join(
        f'              <option value="{escape(entry.id, quote=True)}">'
        f"{escape(entry.id)} ({escape(entry.status)})</option>"
        for entry in roster
    )
    return f'<select id="algo-id" name="algo_id">\n{options}\n            </select>'


_ALGOS_WORKBENCH_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>trader.console algos workbench</title>
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

    #result-outcome.day_skipped {
      border-color: #8fb7ff;
      color: #8fb7ff;
      background: rgba(143, 183, 255, 0.08);
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

    #result-message.day_skipped {
      border-color: #8fb7ff;
      color: #d8e6ff;
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
      <section class="panel" aria-labelledby="algos-form-heading">
        <h2 id="algos-form-heading">Algos</h2>
        <form id="algos-form">
          <label for="algo-id">
            Algo
            __ALGO_CONTROL__
          </label>

          <label for="day">
            Day
            <input id="day" name="day" type="date" value="2026-07-31">
          </label>

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

      const form = document.getElementById("algos-form");
      const submit = document.getElementById("submit");
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
        if (typeof value === "number") return Number.isInteger(value)
          ? String(value)
          : value.toFixed(4).replace(/0+$/, "").replace(/\\.$/, "");
        if (Array.isArray(value)) return value.join(", ");
        return String(value);
      }

      function renderCandidates(candidates) {
        if (!candidates.length) {
          tableWrap.hidden = true;
          message.hidden = false;
          message.className = "ok";
          message.textContent = "OK. No candidates fired for that day.";
          return;
        }

        const columns = [
          "ts",
          "side",
          "instrument",
          "stop",
          "target",
          "confidence",
          "rules_fired",
          "gates_pass",
          "vetoed",
        ];
        table.innerHTML = "<thead><tr>" + columns.map((column) =>
          `<th>${escapeHtml(column)}</th>`).join("") + "</tr></thead><tbody>" +
          candidates.map((candidate) => {
            const trace = candidate.rule_trace || {};
            const bracket = candidate.bracket || {};
            const row = {
              ts: candidate.ts,
              side: candidate.side,
              instrument: bracket.instrument,
              stop: bracket.stop,
              target: bracket.target,
              confidence: candidate.confidence,
              rules_fired: trace.rules_fired || [],
              gates_pass: trace.gates_pass,
              vetoed: trace.vetoed,
            };
            return "<tr>" + columns.map((column) =>
              `<td>${escapeHtml(formatValue(row[column]))}</td>`).join("") +
            "</tr>";
          }).join("") + "</tbody>";
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

        renderCandidates((payload.data && payload.data.candidates) || []);
      }

      async function runQuery(event) {
        event.preventDefault();
        submit.disabled = true;
        outcome.textContent = "running";
        outcome.className = "";
        message.hidden = true;
        tableWrap.hidden = true;
        raw.textContent = "{}";

        try {
          const query = new URLSearchParams(new FormData(form));
          const response = await fetch("/api/workbench/algos?" + query.toString());
          const payload = await response.json();
          renderPayload(payload);
        } catch (error) {
          renderPayload({
            outcome: "error",
            operation: "run_algo",
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


__all__ = ["handle_query", "render_algos_workbench_html"]
