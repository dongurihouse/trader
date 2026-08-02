# trader — contracts (normative)

This file specifies `trader.contracts` exactly. Wave 0 implements it verbatim; later
waves may extend it only by adding fields/records, never by changing existing ones
without a recorded design amendment. All timestamps are timezone-aware UTC `datetime`;
all JSONL timestamps serialize as ISO-8601 with `Z`.

## Module layout

```
src/trader/contracts/
  __init__.py      # re-exports everything below
  types.py         # core dataclasses and aliases
  clock.py         # Clock protocol
  market.py        # MarketData protocol, MarketCalendar
  algo.py          # Algo protocol, AlgoSpec
  intents.py       # Intent
  orders.py        # OrderTicket, Fill, Rejection, PositionState, PortfolioState
  broker.py        # Broker protocol
  risk.py          # RiskEngine protocol
  telemetry.py     # event records, TelemetryWriter protocol, AlgoMetrics
  serde.py         # to_jsonl / from_jsonl for every record type
  errors.py        # LookaheadError, ContractViolation, BrokerNotConfigured
  testing/         # fakes + synthetic fixture generator (§Testing)
```

## types.py

- `Mode = Literal["backtest", "paper", "live"]`
- `Side = Literal["long", "short"]`
- `AlgoStatus = Literal["emitting", "probe", "disabled"]`
- `@dataclass(frozen=True) Bar`: `symbol: str`, `ts: datetime` (bar open time),
  `open: float`, `high: float`, `low: float`, `close: float`, `volume: float`.
  A bar covers [ts, ts+1min) and is complete (visible) once ts+1min <= asof.

## clock.py

```python
class Clock(Protocol):
    @property
    def live(self) -> bool: ...
    def now(self) -> datetime: ...
    def sleep_until(self, when: datetime) -> None:  # backtest: advances now, returns immediately
```

## market.py

```python
class MarketCalendar(Protocol):
    def is_session(self, day: date) -> bool: ...
    def session_close(self, day: date) -> datetime: ...   # handles early closes
    def prev_session(self, day: date) -> date | None: ...

class MarketData(Protocol):
    def bars_1m(self, symbol: str, *, asof: datetime,
                lookback_minutes: int | None = None) -> "pd.DataFrame": ...
    def bars_1d(self, symbol: str, *, asof: date, lookback_days: int) -> "pd.DataFrame": ...
    def signal(self, name: str, *, asof: datetime,
               params: Mapping[str, object] | None = None) -> float: ...
    def event(self, kind: str, *, asof: datetime) -> dict | None: ...
    def calendar(self) -> MarketCalendar: ...
```

- `bars_1m` returns only bars complete at `asof` (PIT rule 1), indexed by ts, columns
  `o,h,l,c,v`. Requests that cannot be served point-in-time raise `LookaheadError`.
- `signal` names come from the provider's registry (`docs/signals.md`, generated).
  Signals are computed on the primary symbol unless the name is symbol-qualified.
- `event` kinds: `"earnings_proximity"`, `"implied_move_pct"`; absent data returns None.

## intents.py

`@dataclass(frozen=True) Intent`:

| field | type | notes |
|---|---|---|
| `algo_id` | str | roster id |
| `ts` | datetime | the asof at which the completed bar was evaluated — the bar's CLOSE time (design ruling 2026-08-01: the producing bar spans [ts-1min, ts), so its Bar.ts open-time is ts-1min; any consumer needing that bar queries `bars_1m(asof=intent.ts)`, never `ts+1min`) |
| `action` | `Literal["open", "close"]` | |
| `side` | `Side \| None` | required for open, None for close |
| `signal_symbol` | str | e.g. SNDK |
| `instrument` | str | resolved ETF, e.g. SNXX |
| `entry` | `Literal["market_next_open"]` | the only entry type in v1 (PIT rule 3) |
| `stop` | `float \| None` | on the instrument's price scale |
| `target` | `float \| None` | |
| `confidence` | `float \| None` | uncalibrated unless stated |
| `reason` | str | plain-English rule trace |
| `meta` | `dict` | free-form (setup name, rules version, ...) |

Reserved `meta` keys (amendment A8): `gates_pass: bool` — emitting algos stamp the
gate/veto outcome on every candidate they produce; absent means passed. `vetoed: str` —
the veto rule id when one bound. Execution routes `gates_pass: false` intents to the
shadow book (tag `gate_refused`), never to the real book.

## orders.py

- `@dataclass(frozen=True) OrderTicket`: `ticket_id: str`, `algo_id`, `intent_ts`,
  `instrument`, `side`, `shares: int`, `entry: "market_next_open"`, `stop`, `target`,
  `risk: dict` (slot index, dollars at risk, equity snapshot), `created_ts`.
- `@dataclass(frozen=True) Fill`: `ticket_id`, `ts`, `price: float`, `shares: int`,
  `kind: Literal["entry","stop","target","reversal","eod"]`, `book: Literal["real","shadow"]`.
- `@dataclass(frozen=True) Rejection`: `intent: Intent`, `rule: str`, `detail: str`.
- `@dataclass PositionState`: `instrument`, `side`, `shares`, `entry_price`,
  `entry_ts`, `stop`, `target`, `algo_id`.
- `@dataclass PortfolioState`: `cash: float`, `equity: float`,
  `positions: list[PositionState]`, `entries_today: int`, `realized_r_today: float`,
  `muted_until: datetime | None`.

## broker.py / risk.py

```python
class Broker(Protocol):
    def submit(self, ticket: OrderTicket) -> None: ...
    def on_bar(self, asof: datetime, data: MarketData) -> list[Fill]: ...
    def cancel_open(self, reason: str) -> None: ...

class RiskEngine(Protocol):
    def check_and_size(self, intent: Intent, portfolio: PortfolioState,
                       data: MarketData) -> OrderTicket | Rejection: ...
```

`Broker.on_bar` resolves pending entries (next-bar open) and monitors stops/targets per
PIT rule 4. The `api` broker grade must raise `BrokerNotConfigured` from `submit` unless
config `live_orders: true` AND environment `TRADER_LIVE=1` AND an adapter is wired.

## algo.py

```python
class Algo(Protocol):
    @property
    def id(self) -> str: ...
    def warmup(self, day: date, data: MarketData) -> None: ...
    def on_bar(self, asof: datetime, data: MarketData) -> list[Intent]: ...
```

`@dataclass AlgoSpec` (one roster entry, parsed from `config/algos.yaml`):
`id: str`, `factory: str` ("module:Class" or builtin name), `params: dict`,
`status: AlgoStatus`.

An `Algo` is pure strategy: it may call only the `MarketData` it is handed, holds no
file/network/clock access, and communicates exclusively by returning intents.

## telemetry.py

Every record is a flat JSON object with common envelope fields
`{"ev": <type>, "ts": <iso8601>, "session": <session_id>}` plus per-type payload:

| ev | payload |
|---|---|
| `session_start` | `mode`, `config_sha256`, `package_version`, `symbols`, `roster: [{id,status}]` |
| `tick` | `bar_ts` |
| `day_skipped` | `day`, `reason` (e.g. `no_prev_session` — PIT rule 5; ports dt's skipped_no_prev) |
| `intent` | full Intent fields |
| `rejection` | intent fields + `rule`, `detail` |
| `ticket` | full OrderTicket fields |
| `fill` | full Fill fields + `tag` (`null` for real-book records; `probe`, `rejected`, or `gate_refused` for shadow records) |
| `position_closed` | `algo_id`, `instrument`, `r_multiple`, `book`, `exit_kind`, `tag` (`null` for real-book records; `probe`, `rejected`, or `gate_refused` for shadow records) |
| `metrics` | one AlgoMetrics object |
| `algo_error` | `algo_id`, `error`, `traceback` |
| `session_end` | `bars_processed`, `real_trades`, `shadow_trades`, `final_equity` |

```python
class TelemetryWriter(Protocol):
    def emit(self, record: dict) -> None: ...   # append one JSONL line, flushed
```

`@dataclass AlgoMetrics`: `algo_id`, `status`, `n_real: int`, `n_shadow: int`,
`wins: int`, `win_rate: float | None`, `mean_r: float | None`,
`expectancy_r: float | None`, `profit_factor: float | None`,
`max_drawdown_r: float | None`, `cum_r: float`, `updated_ts`.
Metrics are per-book: real-book metrics only ever include real fills; shadow metrics are
labeled `book: "shadow"` in the serialized record.

## serde.py

`record_to_json(obj) -> dict` and `record_from_json(dict) -> obj` for every dataclass
above, plus `append_jsonl(path, record)` and `read_jsonl(path) -> Iterator[dict]`.
Round-trip property: `from(to(x)) == x` for every record type (tested).

## errors.py

- `LookaheadError(Exception)` — a request would require data after its asof.
- `ContractViolation(Exception)` — schema/field misuse detected at a boundary.
- `BrokerNotConfigured(Exception)` — api broker used without wiring + interlock.

## testing/ (part of Wave 0)

- `synthetic_day(symbol, day, seed) -> pd.DataFrame` — deterministic 1-minute OHLCV for
  04:00-16:00 ET (premarket included), plausible continuity (each bar's open = prior
  close), volume positive; same seed -> byte-identical frame.
- `FakeClock(start)` — scriptable; `FakeMarketData(frames, signals)` — serves provided
  frames/values with real PIT enforcement; `CollectingTelemetry` — in-memory list;
  `FakeBroker` — fills every entry at next bar open with zero slippage.
- Fixture files under `tests/fixtures/`: `bars/SNDK/2026-07-01.parquet` (synthetic),
  `intents.sample.jsonl`, `telemetry.sample.jsonl` — generated by a
  `python -m trader.contracts.testing.make_fixtures` entry so they can be regenerated,
  and committed so component suites need no generation step.
