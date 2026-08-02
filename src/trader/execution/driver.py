"""Compose and drive backtest, paper, and live execution sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from datetime import timezone as datetime_timezone
import importlib
from pathlib import Path
import sys
from typing import cast
from zoneinfo import ZoneInfo

from trader.contracts import (
    Broker,
    Clock,
    ContractViolation,
    LookaheadError,
    MarketData,
    Mode,
)
from trader.execution.book import RealBook, ShadowBook
from trader.execution.broker import ApiBroker, ManualBroker, SimBroker
from trader.execution.clock import BacktestClock, LiveClock
from trader.execution.config import ExecutionConfig, ResolvedConfig, load_config
from trader.execution.risk import RiskRails
from trader.execution.session import (
    JsonlTelemetryWriter,
    RosterEntry,
    SessionRunner,
    SessionSummary,
)


_ONE_MINUTE = timedelta(minutes=1)
_MODES = frozenset({"backtest", "paper", "live"})


def generate_session_id(mode: Mode, *, now: datetime) -> str:
    """Return the wall-clock identity for one CLI invocation.

    Wave 2's byte-for-byte determinism gate must construct ``SessionRunner``
    with a fixed session id: separate CLI invocations intentionally receive
    different wall-clock-named ids even when their market data is identical.
    """
    return f"{mode}-{now:%Y%m%d}-{now:%H%M%S}"


def select_clock(
    mode: Mode,
    *,
    start: datetime | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Clock:
    """Select the synthetic or live clock required by ``mode``."""
    if mode == "backtest":
        if start is None:
            raise ContractViolation("backtest clock requires a start timestamp")
        return BacktestClock(start)

    if now_fn is None and sleep is None:
        return LiveClock()
    if now_fn is None:
        return LiveClock(sleep=cast(Callable[[float], None], sleep))
    if sleep is None:
        return LiveClock(now_fn=now_fn)
    return LiveClock(now_fn=now_fn, sleep=sleep)


def select_broker(
    execution_config: ExecutionConfig,
    *,
    mode: Mode,
    state_dir: Path,
    timezone: str,
) -> Broker:
    """Build the mode-safe broker for this execution session."""
    if mode in {"backtest", "paper"}:
        return SimBroker(execution_config, timezone=timezone)

    broker_name = execution_config.broker
    if broker_name == "manual":
        return ManualBroker(state_dir, timezone=timezone)
    if broker_name == "api":
        return ApiBroker(execution_config)
    if broker_name == "sim":
        raise ContractViolation(
            "config/execution.yaml: broker: sim cannot be used with --mode live "
            "(sim would book simulated fills as real trades); set broker: manual "
            "or broker: api"
        )
    raise ContractViolation(f"unknown execution broker {broker_name!r}")


def _first_bar_asof(
    day: date,
    *,
    rth_open: time,
    rth_close: time,
    timezone_name: str,
) -> datetime:
    local_timezone = ZoneInfo(timezone_name)
    local_open = datetime.combine(day, rth_open, tzinfo=local_timezone)
    local_close = datetime.combine(day, rth_close, tzinfo=local_timezone)
    first_asof = local_open + _ONE_MINUTE
    if first_asof > local_close:
        raise ContractViolation(
            "backtest RTH close must be at least one minute after RTH open"
        )
    return first_asof.astimezone(datetime_timezone.utc)


def run_backtest(
    runner: SessionRunner,
    *,
    market_data: MarketData,
    primary_symbol: str,
    trading_days: list[date],
    rth_open: time,
    rth_close: time,
    timezone: str,
) -> SessionSummary:
    """Drive a runner through every completed RTH minute in data time."""
    if not trading_days:
        raise ContractViolation("backtest requires at least one trading day")

    first_asof = _first_bar_asof(
        trading_days[0],
        rth_open=rth_open,
        rth_close=rth_close,
        timezone_name=timezone,
    )
    runner.start_session(
        first_asof,
        qualifying_day_counts=runner.qualifying_day_counts(trading_days),
    )
    last_asof = first_asof
    summary: SessionSummary

    try:
        calendar = market_data.calendar()
        local_timezone = ZoneInfo(timezone)
        for trading_day in trading_days:
            day_first_asof = _first_bar_asof(
                trading_day,
                rth_open=rth_open,
                rth_close=rth_close,
                timezone_name=timezone,
            )
            prev = calendar.prev_session(trading_day)
            if prev is None:
                runner.record_day_skipped(
                    trading_day,
                    ts=day_first_asof,
                    reason="no_prev_session",
                )
                continue
            try:
                previous_bars = market_data.bars_1m(
                    primary_symbol,
                    asof=calendar.session_close(prev),
                    lookback_minutes=1440,
                )
            except LookaheadError:
                previous_bars = None
            if previous_bars is None or previous_bars.empty:
                runner.record_day_skipped(
                    trading_day,
                    ts=day_first_asof,
                    reason="no_prev_session",
                )
                continue

            runner.start_day(trading_day, ts=day_first_asof)
            asof = datetime.combine(
                trading_day,
                rth_open,
                tzinfo=local_timezone,
            ) + _ONE_MINUTE
            local_close = datetime.combine(
                trading_day,
                rth_close,
                tzinfo=local_timezone,
            )
            while asof <= local_close:
                utc_asof = asof.astimezone(datetime_timezone.utc)
                last_asof = utc_asof
                runner.process_bar(utc_asof)
                asof += _ONE_MINUTE
    except KeyboardInterrupt:
        pass
    finally:
        summary = runner.end_session(last_asof)

    return summary

def run_live_cadence(
    runner: SessionRunner,
    *,
    clock: Clock,
    market_data: MarketData,
    primary_symbol: str,
    cycle_minutes: int,
    trading_day: date,
    session_start: datetime,
    session_close: datetime,
    on_cycle: Callable[[], None] = lambda: None,
) -> SessionSummary:
    """Fetch on a fixed cadence and process each newly visible minute bar."""
    if cycle_minutes <= 0:
        raise ContractViolation("live cycle_minutes must be greater than zero")

    runner.start_session(session_start)
    last_asof = session_start
    bar_cursor = session_start
    cycle_delta = timedelta(minutes=cycle_minutes)
    next_cycle = session_start + cycle_delta
    summary: SessionSummary

    try:
        runner.start_day(trading_day, ts=session_start)
        while next_cycle <= session_close:
            clock.sleep_until(next_cycle)
            on_cycle()

            while bar_cursor < session_close:
                asof = bar_cursor + _ONE_MINUTE
                try:
                    visible = market_data.bars_1m(
                        primary_symbol,
                        asof=asof,
                        lookback_minutes=1,
                    )
                except LookaheadError:
                    break
                if visible.empty:
                    break

                last_asof = asof
                runner.process_bar(asof)
                bar_cursor = asof

            next_cycle += cycle_delta
    except KeyboardInterrupt:
        pass
    finally:
        summary = runner.end_session(last_asof)

    return summary


def compose_real_market_data(config: ResolvedConfig) -> MarketData:
    """Build the provider-owned market-data service at command runtime."""
    # Wave 2 must confirm and, if necessary, adjust this placeholder entry point
    # once Wave 1-A's real provider composition API is available.
    try:
        from trader.provider import build_market_data
    except (ImportError, ModuleNotFoundError) as exc:
        raise ContractViolation(
            "trader.provider is not installed in this environment yet; "
            "Wave 1-A is built separately"
        ) from exc
    return build_market_data(config)


def compose_algos_from_roster(config: ResolvedConfig) -> list[RosterEntry]:
    """Resolve configured strategy factories at command runtime."""
    try:
        import trader.algos as algos_package
    except (ImportError, ModuleNotFoundError) as exc:
        raise ContractViolation(
            "trader.algos is not installed in this environment yet; "
            "Wave 1-B is built separately"
        ) from exc

    roster: list[RosterEntry] = []
    for spec in config.algos.roster:
        try:
            if ":" in spec.factory:
                module_name, attribute_name = spec.factory.rsplit(":", 1)
                module = importlib.import_module(module_name)
                factory = getattr(module, attribute_name)
            else:
                factory = getattr(algos_package, spec.factory)
            if not isinstance(spec.params, dict):
                raise TypeError("algo params must be a mapping")
            algo = factory(id=spec.id, status=spec.status, params=spec.params)
        except (
            AttributeError,
            ImportError,
            ModuleNotFoundError,
            TypeError,
            ValueError,
        ) as exc:
            raise ContractViolation(
                f"trader.algos factory {spec.factory!r} for {spec.id!r} "
                "could not be composed"
            ) from exc
        roster.append(RosterEntry(algo=algo, status=spec.status))
    return roster


def _parse_date_range(start_text: str, end_text: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except ValueError as exc:
        raise ContractViolation(
            "--start and --end must use YYYY-MM-DD"
        ) from exc
    if start > end:
        raise ContractViolation("--start must be on or before --end")
    return start, end


def _dates_inclusive(start: date, end: date) -> list[date]:
    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]


def _print_run_error(message: str) -> None:
    print(f"trader run: {message}", file=sys.stderr)


def run_session_command(args) -> int | None:
    """Compose and execute the ``trader run`` command."""
    try:
        resolved = load_config()
    except ContractViolation as exc:
        _print_run_error(str(exc))
        return 2

    raw_mode = getattr(args, "mode", None)
    if raw_mode not in _MODES:
        _print_run_error(f"unsupported mode {raw_mode!r}")
        return 2
    mode = cast(Mode, raw_mode)

    backtest_range: tuple[date, date] | None = None
    if mode == "backtest":
        if args.start is None or args.end is None:
            _print_run_error("backtest mode requires both --start and --end")
            return 2
        try:
            backtest_range = _parse_date_range(args.start, args.end)
        except ContractViolation as exc:
            _print_run_error(str(exc))
            return 2

    session_id = generate_session_id(
        mode,
        now=datetime.now(datetime_timezone.utc),
    )
    state_dir = Path(resolved.trader.data_root) / "sessions" / session_id
    telemetry = JsonlTelemetryWriter(state_dir / "telemetry.jsonl")
    try:
        broker = select_broker(
            resolved.execution,
            mode=mode,
            state_dir=state_dir,
            timezone=resolved.trader.timezone,
        )
    except ContractViolation as exc:
        _print_run_error(str(exc))
        return 2
    risk = RiskRails(
        resolved.risk,
        resolved.execution,
        timezone=resolved.trader.timezone,
    )
    real_book = RealBook(
        resolved.risk,
        resolved.execution,
        commission=resolved.execution.fills.commission,
        timezone=resolved.trader.timezone,
    )
    shadow_book = ShadowBook(resolved.execution)
    market_data = compose_real_market_data(resolved)
    roster = compose_algos_from_roster(resolved)
    runner = SessionRunner(
        session_id=session_id,
        mode=mode,
        market_data=market_data,
        broker=broker,
        risk=risk,
        real_book=real_book,
        shadow_book=shadow_book,
        telemetry=telemetry,
        roster=roster,
        symbols=resolved.trader.symbols,
        config_sha256=resolved.config_sha256,
        package_version=resolved.package_version,
        execution_config=resolved.execution,
        traded_instruments=list(resolved.trader.instrument_map.values()),
        timezone=resolved.trader.timezone,
    )

    if mode == "backtest":
        assert backtest_range is not None
        start, end = backtest_range
        calendar = market_data.calendar()
        trading_days = [
            day for day in _dates_inclusive(start, end) if calendar.is_session(day)
        ]
        try:
            rth_open = time.fromisoformat(resolved.trader.session.rth_open)
            rth_close = time.fromisoformat(resolved.trader.session.rth_close)
            summary = run_backtest(
                runner,
                market_data=market_data,
                primary_symbol=resolved.trader.primary_symbol,
                trading_days=trading_days,
                rth_open=rth_open,
                rth_close=rth_close,
                timezone=resolved.trader.timezone,
            )
        except (ContractViolation, ValueError) as exc:
            _print_run_error(str(exc))
            return 2
    else:
        clock = select_clock(mode)
        session_start = clock.now()
        local_timezone = ZoneInfo(resolved.trader.timezone)
        trading_day = session_start.astimezone(local_timezone).date()
        calendar = market_data.calendar()
        if not calendar.is_session(trading_day):
            _print_run_error(f"{trading_day.isoformat()} is not a trading session")
            return 2
        session_close = calendar.session_close(trading_day)
        summary = run_live_cadence(
            runner,
            clock=clock,
            market_data=market_data,
            primary_symbol=resolved.trader.primary_symbol,
            cycle_minutes=resolved.trader.live.cycle_minutes,
            trading_day=trading_day,
            session_start=session_start,
            session_close=session_close,
        )

    print(
        f"Session {session_id}: {summary.bars_processed} bars, "
        f"{summary.real_trades} real trades, "
        f"{summary.shadow_trades} shadow trades, "
        f"final equity {summary.final_equity:.2f}"
    )
    return None


__all__ = [
    "compose_algos_from_roster",
    "compose_real_market_data",
    "generate_session_id",
    "run_backtest",
    "run_live_cadence",
    "run_session_command",
    "select_broker",
    "select_clock",
]
