"""Generate deterministic contract fixtures for component test suites."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from trader.contracts.intents import Intent
from trader.contracts.serde import append_jsonl, record_to_json
from trader.contracts.telemetry import (
    AlgoErrorEvent,
    DataThinEvent,
    DaySkippedEvent,
    FillEvent,
    IntentEvent,
    MetricsEvent,
    PositionClosedEvent,
    RejectionEvent,
    SessionEndEvent,
    SessionStartEvent,
    TicketEvent,
    TickEvent,
)
from trader.contracts.testing import synthetic_day


def generate_fixtures(output_dir: Path) -> None:
    """Write the complete deterministic fixture set beneath ``output_dir``."""
    output_dir = Path(output_dir)
    bars_path = output_dir / "bars" / "SNDK" / "2026-07-01.parquet"
    intents_path = output_dir / "intents.sample.jsonl"
    telemetry_path = output_dir / "telemetry.sample.jsonl"

    output_dir.mkdir(parents=True, exist_ok=True)
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    synthetic_day(
        "SNDK",
        date(2026, 7, 1),
        seed=20260701,
    ).to_parquet(bars_path)

    intents = [
        Intent(
            algo_id="breakout",
            ts=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
            action="open",
            side="long",
            signal_symbol="SNDK",
            instrument="SNXX",
            entry="market_next_open",
            stop=41.0,
            target=46.625,
            confidence=0.82,
            reason="price cleared the opening range",
            meta={"setup": "opening_range", "rules_version": 1},
        ),
        Intent(
            algo_id="mean-reversion",
            ts=datetime(2026, 7, 1, 14, 2, tzinfo=timezone.utc),
            action="open",
            side="short",
            signal_symbol="SNDK",
            instrument="SNXX",
            entry="market_next_open",
            stop=45.5,
            target=42.0,
            confidence=0.68,
            reason="price stretched above the intraday mean",
            meta={"setup": "vwap_reversion", "rules_version": 1},
        ),
        Intent(
            algo_id="breakout",
            ts=datetime(2026, 7, 1, 15, 45, tzinfo=timezone.utc),
            action="close",
            side=None,
            signal_symbol="SNDK",
            instrument="SNXX",
            entry="market_next_open",
            stop=None,
            target=None,
            confidence=None,
            reason="target reached",
            meta={"setup": "opening_range", "exit": "target"},
        ),
        Intent(
            algo_id="mean-reversion",
            ts=datetime(2026, 7, 1, 19, 55, tzinfo=timezone.utc),
            action="close",
            side=None,
            signal_symbol="SNDK",
            instrument="SNXX",
            entry="market_next_open",
            stop=None,
            target=None,
            confidence=None,
            reason="session risk window ended",
            meta={"setup": "vwap_reversion", "exit": "eod"},
        ),
    ]

    session = "fixture-session-001"
    telemetry = [
        SessionStartEvent(
            ev="session_start",
            ts=datetime(2026, 7, 1, 13, 29, tzinfo=timezone.utc),
            session=session,
            mode="paper",
            config_sha256="fixture-config-sha256",
            package_version="0.1.0",
            symbols=["SNDK", "SNXX"],
            etf_price_basis="auto",
            qualifying_day_counts={"SNXX": 0},
            roster=[
                {"id": "breakout", "status": "emitting"},
                {"id": "mean-reversion", "status": "probe"},
            ],
        ),
        TickEvent(
            ev="tick",
            ts=datetime(2026, 7, 1, 13, 31, tzinfo=timezone.utc),
            session=session,
            bar_ts=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
        ),
        DaySkippedEvent(
            ev="day_skipped",
            ts=datetime(2026, 7, 1, 13, 31, 1, tzinfo=timezone.utc),
            session=session,
            day="2026-06-30",
            reason="no_prev_session",
        ),
        DataThinEvent(
            ev="data_thin",
            ts=datetime(2026, 7, 1, 13, 31, 1, tzinfo=timezone.utc),
            session=session,
            symbol="SNXX",
            day="2026-07-01",
            bar_count=42,
            min_intraday_bars=100,
        ),
        IntentEvent(
            ev="intent",
            ts=datetime(2026, 7, 1, 13, 31, 2, tzinfo=timezone.utc),
            session=session,
            algo_id="breakout",
            action="open",
            side="long",
            signal_symbol="SNDK",
            instrument="SNXX",
            entry="market_next_open",
            stop=41.0,
            target=46.625,
            confidence=0.82,
            reason="price cleared the opening range",
            meta={"setup": "opening_range", "rules_version": 1},
        ),
        RejectionEvent(
            ev="rejection",
            ts=datetime(2026, 7, 1, 13, 31, 4, tzinfo=timezone.utc),
            session=session,
            algo_id="mean-reversion",
            action="open",
            side="short",
            signal_symbol="SNDK",
            instrument="SNXX",
            entry="market_next_open",
            stop=45.5,
            target=42.0,
            confidence=0.42,
            reason="price stretched above the intraday mean",
            meta={"setup": "vwap_reversion", "rules_version": 1},
            rule="confidence_floor",
            detail="0.42 is below the required 0.50",
        ),
        TicketEvent(
            ev="ticket",
            ts=datetime(2026, 7, 1, 13, 31, 5, tzinfo=timezone.utc),
            session=session,
            ticket_id="fixture-ticket-001",
            algo_id="breakout",
            intent_ts=datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc),
            instrument="SNXX",
            side="long",
            shares=100,
            entry="market_next_open",
            stop=41.0,
            target=46.625,
            risk={"slot": 1, "dollars": 225.0, "equity": 100_000.0},
            created_ts=datetime(2026, 7, 1, 13, 31, 5, tzinfo=timezone.utc),
        ),
        FillEvent(
            ev="fill",
            ts=datetime(2026, 7, 1, 13, 32, tzinfo=timezone.utc),
            session=session,
            ticket_id="fixture-ticket-001",
            price=43.25,
            shares=100,
            kind="entry",
            book="real",
            price_basis="real",
            tag=None,
        ),
        FillEvent(
            ev="fill",
            ts=datetime(2026, 7, 1, 15, 45, tzinfo=timezone.utc),
            session=session,
            ticket_id="fixture-ticket-001",
            price=46.625,
            shares=100,
            kind="target",
            book="real",
            price_basis="real",
            tag=None,
        ),
        PositionClosedEvent(
            ev="position_closed",
            ts=datetime(2026, 7, 1, 15, 45, 1, tzinfo=timezone.utc),
            session=session,
            algo_id="breakout",
            instrument="SNXX",
            r_multiple=1.5,
            book="real",
            exit_kind="target",
            tag=None,
        ),
        MetricsEvent(
            ev="metrics",
            ts=datetime(2026, 7, 1, 15, 45, 10, tzinfo=timezone.utc),
            session=session,
            book="real",
            algo_id="breakout",
            status="emitting",
            n_real=1,
            n_shadow=0,
            wins=1,
            win_rate=1.0,
            mean_r=1.5,
            expectancy_r=1.5,
            profit_factor=None,
            max_drawdown_r=0.0,
            cum_r=1.5,
            updated_ts=datetime(2026, 7, 1, 15, 45, 10, tzinfo=timezone.utc),
        ),
        AlgoErrorEvent(
            ev="algo_error",
            ts=datetime(2026, 7, 1, 15, 46, tzinfo=timezone.utc),
            session=session,
            algo_id="mean-reversion",
            error="fixture signal unavailable",
            traceback="Traceback (most recent call last): fixture signal unavailable",
        ),
        SessionEndEvent(
            ev="session_end",
            ts=datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
            session=session,
            bars_processed=720,
            real_trades=1,
            shadow_trades=0,
            final_equity=100_337.5,
        ),
    ]

    for jsonl_path, records in (
        (intents_path, intents),
        (telemetry_path, telemetry),
    ):
        jsonl_path.unlink(missing_ok=True)
        for record in records:
            append_jsonl(jsonl_path, record_to_json(record))


def main() -> None:
    """Regenerate the repository's committed fixture files."""
    repository_root = Path(__file__).resolve().parents[4]
    generate_fixtures(repository_root / "tests" / "fixtures")


if __name__ == "__main__":
    main()
