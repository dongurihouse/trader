"""Post-session results payload assembly for console Observation views.

Observation code reads one session's ``telemetry.jsonl`` and stays isolated from
provider, algos, and execution packages so a broken sibling component cannot
break the view of a recorded run. The one deliberate exception in this module
is ``build_day_candles``: charts may read the static, git-ignored
``data/bars/1m/<SYMBOL>/<YYYY-MM-DD>.parquet`` file directly with pandas. That
reader intentionally does not import ``trader.provider``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from trader.contracts import read_jsonl
from trader.console.dashboard import SHADOW_CAVEAT_TEXT


_BAR_COLUMNS = ("o", "h", "l", "c", "v")
_METRIC_FIELDS = (
    "n_real",
    "n_shadow",
    "wins",
    "win_rate",
    "mean_r",
    "expectancy_r",
    "profit_factor",
    "max_drawdown_r",
    "cum_r",
)


def build_results_payload(session_dir: Path) -> dict:
    """Assemble the full results payload for one session from its telemetry alone.

    No file other than ``session_dir / "telemetry.jsonl"`` is read. Missing
    telemetry, or telemetry without a ``session_start`` event, raises a clear
    exception naming ``session_dir``.

    Return shape:
    ``{
        "session": {"id", "mode", "config_sha256", "package_version", "symbols"},
        "executive_summary": {
            "window_start", "window_end", "days_count", "days_skipped_count",
            "trades", "win_rate", "mean_r", "profit_factor", "cum_r",
            "max_drawdown_r", "final_equity"
        },
        "algos": [{"id", "status"}, ...],
        "shadow_caveat": SHADOW_CAVEAT_TEXT,
        "days": [{"day", "status", "reason"}, ...],
        "trades": [{
            "id", "algo_id", "side", "instrument", "day", "entry_ts",
            "entry_price", "stop", "target", "exit_ts", "exit_price",
            "exit_kind", "r_multiple", "confidence", "reason", "rule_trace"
        }, ...],
        "per_algo_metrics": [{
            "algo_id", "book", "status", "n_real", "n_shadow", "wins",
            "win_rate", "mean_r", "expectancy_r", "profit_factor",
            "max_drawdown_r", "cum_r"
        }, ...],
    }``.

    ``rule_trace`` is either ``None`` or
    ``{"setup_id", "rules_version", "rules_fired", "direction_votes",
    "gates_pass", "vetoed", "uncalibrated"}``. Roster algos with no metrics
    produce one ``per_algo_metrics`` row with ``book`` set to ``None``,
    count fields set to zero, ratio fields set to ``None``, and ``cum_r`` set
    to ``0.0``.
    """
    session_dir = Path(session_dir)
    events = _read_events(session_dir)
    session_start = next(
        (record for record in events if record.get("ev") == "session_start"),
        None,
    )
    if session_start is None:
        raise ValueError(
            f"telemetry.jsonl has no session_start event for session directory "
            f"{session_dir}"
        )

    algos = [
        {"id": member.get("id"), "status": member.get("status")}
        for member in session_start.get("roster", [])
    ]
    days = _build_days(events)
    trades = _build_trades(events)
    session_end = next(
        (record for record in reversed(events) if record.get("ev") == "session_end"),
        None,
    )

    return {
        "session": {
            "id": session_dir.name,
            "mode": session_start.get("mode"),
            "config_sha256": session_start.get("config_sha256"),
            "package_version": session_start.get("package_version"),
            "symbols": list(session_start.get("symbols", [])),
        },
        "executive_summary": _build_executive_summary(
            days,
            trades,
            final_equity=(
                session_end.get("final_equity") if session_end is not None else None
            ),
        ),
        "algos": algos,
        "shadow_caveat": SHADOW_CAVEAT_TEXT,
        "days": days,
        "trades": trades,
        "per_algo_metrics": _build_per_algo_metrics(events, algos),
    }


def build_day_candles(data_root: Path, symbol: str, day: date) -> list[dict]:
    """Read one static 1-minute bar parquet file as chart candles.

    This is the ONE deliberate exception to Observation's telemetry-only rule.
    It reads ``data/bars/1m/<symbol>/<YYYY-MM-DD>.parquet`` directly with
    ``pandas.read_parquet`` and returns rows in file order as
    ``[{"ts": <UTC ISO Z>, "o": float, "h": float, "l": float, "c": float,
    "v": float}, ...]``. Missing file returns ``[]``.
    """
    import pandas as pd

    path = (
        Path(data_root)
        / "bars"
        / "1m"
        / symbol
        / f"{day.isoformat()}.parquet"
    )
    try:
        frame = pd.read_parquet(path)
    except FileNotFoundError:
        return []

    candles: list[dict] = []
    for index_value, row in frame.iterrows():
        timestamp = pd.Timestamp(index_value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        candles.append(
            {
                "ts": timestamp.isoformat().replace("+00:00", "Z"),
                **{column: float(row[column]) for column in _BAR_COLUMNS},
            }
        )
    return candles


def _read_events(session_dir: Path) -> list[dict]:
    telemetry_path = session_dir / "telemetry.jsonl"
    try:
        return list(read_jsonl(telemetry_path))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"telemetry.jsonl is missing for session directory {session_dir}"
        ) from exc


def _build_days(events: list[dict]) -> list[dict]:
    processed_days: set[str] = set()
    skipped_reasons: dict[str, str | None] = {}
    for record in events:
        event_type = record.get("ev")
        if event_type == "tick":
            # Trading-hours bars in this program never cross UTC midnight, so
            # the UTC calendar date of bar_ts is the session day without a
            # timezone conversion.
            processed_days.add(_parse_utc(str(record["bar_ts"])).date().isoformat())
        elif event_type == "day_skipped":
            skipped_reasons[str(record["day"])] = record.get("reason")

    days: list[dict] = []
    for day in sorted(processed_days | set(skipped_reasons)):
        if day in processed_days:
            days.append({"day": day, "status": "processed", "reason": None})
        else:
            days.append(
                {
                    "day": day,
                    "status": "skipped",
                    "reason": skipped_reasons.get(day),
                }
            )
    return days


def _build_trades(events: list[dict]) -> list[dict]:
    tickets: dict[str, dict] = {}
    intent_by_key: dict[tuple[str, str], dict] = {}
    real_fills_by_ticket: dict[str, list[dict]] = {}
    real_closed_by_key: dict[tuple[str, str], dict] = {}

    for record in events:
        event_type = record.get("ev")
        if event_type == "intent":
            intent_by_key[(str(record["algo_id"]), str(record["ts"]))] = record
        elif event_type == "ticket":
            tickets[str(record["ticket_id"])] = record
        elif event_type == "fill" and record.get("book") == "real":
            real_fills_by_ticket.setdefault(str(record["ticket_id"]), []).append(
                record
            )
        elif event_type == "position_closed" and record.get("book") == "real":
            real_closed_by_key[(str(record["algo_id"]), str(record["ts"]))] = record

    trades: list[dict] = []
    for ticket_id, ticket in tickets.items():
        fills = real_fills_by_ticket.get(ticket_id, [])
        entry_fill = next(
            (fill for fill in fills if fill.get("kind") == "entry"),
            None,
        )
        exit_fill = next(
            (fill for fill in fills if fill.get("kind") != "entry"),
            None,
        )
        if entry_fill is None or exit_fill is None:
            continue

        # Real position_closed telemetry is emitted with the same timestamp as
        # the exit fill that caused it, making this exact key intentional.
        closed = real_closed_by_key.get(
            (str(ticket["algo_id"]), str(exit_fill["ts"]))
        )
        if closed is None:
            continue

        # Shadow episodes never have a ticket event; their synthetic ticket_id
        # is intentionally not parsed here. V5 exposes shadow context through
        # aggregate metrics only, not per-trade shadow drill-down.
        intent = intent_by_key.get(
            (str(ticket["algo_id"]), str(ticket["intent_ts"]))
        )
        trades.append(
            {
                "id": ticket_id,
                "algo_id": ticket.get("algo_id"),
                "side": ticket.get("side"),
                "instrument": ticket.get("instrument"),
                "day": _parse_utc(str(entry_fill["ts"])).date().isoformat(),
                "entry_ts": entry_fill.get("ts"),
                "entry_price": entry_fill.get("price"),
                "stop": ticket.get("stop"),
                "target": ticket.get("target"),
                "exit_ts": exit_fill.get("ts"),
                "exit_price": exit_fill.get("price"),
                "exit_kind": exit_fill.get("kind"),
                "r_multiple": closed.get("r_multiple"),
                "confidence": intent.get("confidence") if intent else None,
                "reason": intent.get("reason") if intent else None,
                "rule_trace": _rule_trace(intent),
            }
        )

    return sorted(trades, key=lambda trade: _parse_utc(str(trade["exit_ts"])))


def _rule_trace(intent: dict | None) -> dict | None:
    if intent is None:
        return None

    meta = intent.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    return {
        "setup_id": meta.get("setup_id"),
        "rules_version": meta.get("rules_version"),
        "rules_fired": _list_value(meta.get("rules_fired")),
        "direction_votes": _list_value(meta.get("direction_votes")),
        "gates_pass": meta.get("gates_pass"),
        "vetoed": meta.get("vetoed"),
        "uncalibrated": meta.get("uncalibrated"),
    }


def _build_executive_summary(
    days: list[dict],
    trades: list[dict],
    *,
    final_equity: object,
) -> dict:
    r_multiples = [float(trade["r_multiple"]) for trade in trades]
    winning_rs = [value for value in r_multiples if value > 0]
    losing_rs = [abs(value) for value in r_multiples if value < 0]
    cum_r = sum(r_multiples)

    return {
        "window_start": days[0]["day"] if days else None,
        "window_end": days[-1]["day"] if days else None,
        "days_count": sum(day["status"] == "processed" for day in days),
        "days_skipped_count": sum(day["status"] == "skipped" for day in days),
        "trades": len(trades),
        "win_rate": len(winning_rs) / len(trades) if trades else None,
        "mean_r": cum_r / len(trades) if trades else None,
        "profit_factor": (
            sum(winning_rs) / sum(losing_rs) if losing_rs else None
        ),
        "cum_r": cum_r,
        "max_drawdown_r": _max_drawdown(r_multiples),
        "final_equity": final_equity,
    }


def _max_drawdown(r_multiples: list[float]) -> float | None:
    if not r_multiples:
        return None

    running = 0.0
    peak = 0.0
    max_drawdown_r = 0.0
    for r_multiple in r_multiples:
        running += r_multiple
        peak = max(peak, running)
        max_drawdown_r = max(max_drawdown_r, peak - running)
    return max_drawdown_r


def _build_per_algo_metrics(events: list[dict], algos: list[dict]) -> list[dict]:
    roster_status = {str(algo["id"]): algo.get("status") for algo in algos}
    latest_metrics: dict[tuple[str, str], dict] = {}
    for record in events:
        if record.get("ev") == "metrics":
            latest_metrics[(str(record["algo_id"]), str(record["book"]))] = record

    rows: list[dict] = []
    algos_with_metrics = {algo_id for algo_id, _ in latest_metrics}
    for algo_id in sorted(set(roster_status) - algos_with_metrics):
        rows.append(_zero_metrics_row(algo_id, roster_status[algo_id]))

    for (algo_id, book), record in sorted(latest_metrics.items()):
        rows.append(
            {
                "algo_id": algo_id,
                "book": book,
                "status": record.get("status") or roster_status.get(algo_id),
                **{field: record.get(field) for field in _METRIC_FIELDS},
            }
        )
    return rows


def _zero_metrics_row(algo_id: str, status: object) -> dict:
    return {
        "algo_id": algo_id,
        "book": None,
        "status": status,
        "n_real": 0,
        "n_shadow": 0,
        "wins": 0,
        "win_rate": None,
        "mean_r": None,
        "expectancy_r": None,
        "profit_factor": None,
        "max_drawdown_r": None,
        "cum_r": 0.0,
    }


def _parse_utc(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _list_value(value: object) -> list:
    return list(value) if isinstance(value, list) else []


__all__ = ["build_day_candles", "build_results_payload"]
