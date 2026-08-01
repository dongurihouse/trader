"""Deterministic post-session Markdown report generation."""

from __future__ import annotations

from pathlib import Path

from trader.contracts import read_jsonl
from trader.console.dashboard import SHADOW_CAVEAT_TEXT


_NO_DATA = "—"
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


def build_report_markdown(session_id: str, events: list[dict]) -> str:
    """Build a deterministic Markdown report from telemetry in file order."""
    session_start = next(
        (record for record in events if record.get("ev") == "session_start"),
        None,
    )
    session_end = next(
        (record for record in reversed(events) if record.get("ev") == "session_end"),
        None,
    )

    roster: dict[str, object] = {}
    if session_start is not None:
        for member in session_start.get("roster", []):
            roster[str(member["id"])] = member.get("status")

    latest_metrics: dict[tuple[str, str], dict] = {}
    rejections: dict[str, list[dict]] = {}
    algo_errors: list[dict] = []
    for record in events:
        event_type = record.get("ev")
        if event_type == "metrics":
            key = (str(record["algo_id"]), str(record["book"]))
            latest_metrics[key] = record
        elif event_type == "rejection":
            rule = str(record.get("rule", _NO_DATA))
            rejections.setdefault(rule, []).append(record)
        elif event_type == "algo_error":
            algo_errors.append(record)

    lines = [
        f"# Trader session: {_format_value(session_id)}",
        "",
        "## Session summary",
        "",
        "| Field | Value |",
        "| --- | --- |",
    ]
    for field in ("mode", "config_sha256", "package_version"):
        value = session_start.get(field) if session_start is not None else None
        lines.append(f"| {field} | {_format_value(value)} |")

    symbols = session_start.get("symbols") if session_start is not None else None
    symbol_text = ", ".join(str(symbol) for symbol in symbols) if symbols else None
    lines.append(f"| symbols | {_format_value(symbol_text)} |")

    if session_end is None:
        lines.extend(["", "Session end was not recorded; final totals are unavailable."])
    else:
        for field in (
            "bars_processed",
            "real_trades",
            "shadow_trades",
            "final_equity",
        ):
            lines.append(f"| {field} | {_format_value(session_end.get(field))} |")

    lines.extend(
        [
            "",
            "## Per-algo metrics",
            "",
            "| Algo | Book | Status | n_real | n_shadow | wins | win_rate | mean_r | expectancy_r | profit_factor | max_drawdown_r | cum_r |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )

    algos_with_metrics = {algo_id for algo_id, _ in latest_metrics}
    for algo_id in sorted(set(roster) - algos_with_metrics):
        cells = [algo_id, _NO_DATA, roster[algo_id], *([None] * len(_METRIC_FIELDS))]
        lines.append(_markdown_row(cells))

    for (algo_id, book), record in sorted(latest_metrics.items()):
        status = record.get("status", roster.get(algo_id))
        cells = [
            algo_id,
            book,
            status,
            *(record.get(field) for field in _METRIC_FIELDS),
        ]
        lines.append(_markdown_row(cells))

    if not roster and not latest_metrics:
        lines.append(f"| {_NO_DATA} | {_NO_DATA} | {_NO_DATA} | " + " | ".join([_NO_DATA] * len(_METRIC_FIELDS)) + " |")

    lines.extend(["", SHADOW_CAVEAT_TEXT, "", "## Rejections by rule", ""])
    if rejections:
        for rule in sorted(rejections):
            records = rejections[rule]
            lines.extend([f"### {_format_value(rule)} ({len(records)})", ""])
            for record in records:
                lines.append(
                    "- Algo: {algo}; instrument: {instrument}; detail: {detail}".format(
                        algo=_format_value(record.get("algo_id")),
                        instrument=_format_value(record.get("instrument")),
                        detail=_format_value(record.get("detail")),
                    )
                )
            lines.append("")
    else:
        lines.extend(["No rejections were recorded.", ""])

    lines.extend(["## Algo errors", ""])
    if algo_errors:
        for record in algo_errors:
            lines.append(
                "- Algo: {algo}; error: {error}".format(
                    algo=_format_value(record.get("algo_id")),
                    error=_format_value(record.get("error")),
                )
            )
    else:
        lines.append("No algo errors were recorded.")

    return "\n".join(lines) + "\n"


def write_report(session_dir: Path) -> str:
    """Write ``report.md`` for a session and return its Markdown content."""
    events = list(read_jsonl(session_dir / "telemetry.jsonl"))
    markdown = build_report_markdown(session_dir.name, events)
    (session_dir / "report.md").write_text(markdown, encoding="utf-8")
    return markdown


def _markdown_row(values: list[object]) -> str:
    return "| " + " | ".join(_format_value(value) for value in values) + " |"


def _format_value(value: object) -> str:
    if value is None:
        return _NO_DATA
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
