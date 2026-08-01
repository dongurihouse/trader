# trader — architecture

Status: ADOPTED (Dev rulings 2026-08-01, amended same day per §9 A6-A7). This document is
the normative design for the merged `trader` project, successor to three prior projects:
`dt` (live SNDK signal system), `daytrader` (research pipeline), and `win` (data-provider
design, never implemented). The Dev ruled on 2026-08-01: merge on the five-component
redesign (amended where recorded in §9), no behavior-preservation constraint against `dt`
(fresh evidence base), and the three prior repositories are removed once migration is
verified complete (§11).

All code is written by Codex (`gpt-5.6-sol`, reasoning effort xhigh) on small bounded
tasks. Claude subagents dispatch those tasks, run tests, and verify; the controller
authors design, contracts, configuration, and reviews.

## 1. What the system does

Single-ticker intraday program: signals are computed on SNDK; positions are expressed on
leveraged single-stock ETFs (long → SNXX, short → SNDQ). One process per session,
operator-started. Three modes — `backtest`, `paper`, `live` — run the identical decision
path; only the clock source and the broker differ.

## 2. Components

| Component | Package | Role |
|---|---|---|
| Contracts | `trader.contracts` | Typed interfaces, record schemas, errors, testing fakes. The shared import surface. |
| Provider | `trader.provider` | Data ingestion, canonical bar store, market calendar, events, and the signal engine (named, point-in-time-guarded signal computation). |
| Algos | `trader.algos` | Pure strategy library: every trading algorithm, the rules grammar, and bracket construction. No I/O, no clock, no broker knowledge — an algo consumes `MarketData` and returns intents. |
| Execution | `trader.execution` | The orchestrator. Config loading, the virtual clock, the session loop, risk checks, position sizing, brokers (sim / manual / api), real and shadow books, telemetry writing, the `trader run` command. |
| Console | `trader.console` | Real-time dashboard (SSE over a session's telemetry file) and post-session reports. |

There is deliberately no `engines/` folder and no ported-whole predecessor system: `dt`
and `daytrader` are dismantled, and each piece lands in the component that owns that
concern (map in §9 A6).

Dependency rule: provider, algos, and console import only `trader.contracts` (console
additionally only reads session files). Execution is the composition point — it may
import provider (to construct `MarketData`) and instantiate algos from the roster config
via their factory strings — but it consumes both strictly through the contract types.
This is what makes each component buildable and testable completely separately.

## 3. Repository layout

```
trader/
  pyproject.toml            # package `trader`, CLI entry `trader`, deps: pandas, pyarrow, pyyaml
  config/                   # the running configuration (YAML, versioned in git)
    trader.yaml             # identity: symbols, instrument map, session times, data root
    provider.yaml           # vendor, relay, validation thresholds
    algos.yaml              # gates + algo roster (generated from dt sources in Wave 1, then hand-tuned)
    risk.yaml               # equity, slots, rails, mutes
    execution.yaml          # broker selection, fill model, slippage/commission, live interlock
    console.yaml            # host/port
  src/trader/
    contracts/              # Wave 0. Types, protocols, serde, errors, testing fakes + fixtures
    provider/               # Wave 1-A
    algos/                  # Wave 1-B
    execution/              # Wave 1-C (orchestrator + risk + brokers)
    console/                # Wave 1-D
    cli.py                  # argparse entry: fetch, ingest, run, report, console, validate
  tests/
    contracts/  provider/  algos/  execution/  console/  integration/
  data/                     # git-ignored data root (layout in §6)
  docs/
    design/                 # this file, contracts.md
    plans/                  # build-plan.md
    archive/                # read-only knowledge carried from dt / daytrader / win (§11)
```

## 4. Point-in-time rules (normative, all components)

These rules are ported from `dt`, where they were tested behaviors; every implementation
task that touches them cites them:

1. A bar timestamped T (bar covering T..T+1min) is visible only when T+1min <= asof.
2. Signals are computed from bars at or before asof only. The provider raises
   `LookaheadError` on any request that would need later data.
3. Entry fills happen at the next bar's open after the intent bar, never the intent bar.
4. An exit bar that spans both stop and target resolves to the stop (stop wins ties).
   A bar opening beyond a level fills at that bar's open, not at the level.
5. Warm-up context comes from the immediately preceding trading session only; a missing
   previous session skips the day rather than reaching further back.
6. The market calendar is static and ex-ante (holidays and early closes known in advance).

## 5. The session loop (owned by execution)

```
load configs -> resolve mode -> create Clock, MarketData, Broker(s), TelemetryWriter
instantiate algos from roster (status: emitting | probe | disabled)
for each completed 1-minute bar of the primary symbol within session hours:
    asof = bar_close_time
    fills = broker.on_bar(asof, market_data)        # resolve pending entries/stops/targets
    portfolio.apply(fills); emit telemetry
    for algo in active_algos:
        intents = algo.on_bar(asof, market_data)     # may be []
        for intent in intents:
            decision = risk.check_and_size(intent, portfolio, market_data)
            emitting algo + accepted  -> ticket to real broker
            emitting algo + rejected  -> shadow book (tagged rejected)
            probe algo                -> shadow book (tagged probe)
            emit telemetry for every branch
end of session: force-flat both books, write final metrics + state.json + report
```

- `backtest`: the loop iterates stored bars as fast as they read; the clock is synthetic.
- `paper` / `live`: the runner fetches every `cycle_minutes` (default 5), then processes
  all newly completed bars through the identical loop. No daemon, no scheduler: a person
  starts the session in a terminal and ends it with Ctrl-C.
- In backtest mode every telemetry timestamp comes from the synthetic clock (bar time),
  never wall time — the determinism gate (§9 A5) depends on this.
- Every event (session_start with config hash, tick, intent, rejection, ticket, fill,
  exit, metrics snapshot, algo error, session_end) is appended to
  `data/sessions/<session_id>/telemetry.jsonl` as it happens. The console only ever reads
  this file; it never touches component internals.

## 6. Data root layout (file contracts)

```
data/
  bars/1m/<SYMBOL>/<YYYY-MM-DD>.parquet   # columns o,h,l,c,v; UTC DatetimeIndex; premarket included
  bars/1d/<SYMBOL>.parquet
  raw/robinhood/*.json                    # immutable vendor dumps, ingest input
  events/earnings.json
  events/options/*.json                   # implied-move captures (manual procedure)
  news/raw/<YYYY-MM-DD>.jsonl             # optional archive; carries no measured signal
  calendar/market.yaml
  sessions/<session_id>/                  # session_id = <mode>-<YYYYMMDD>-<HHMMSS>
    telemetry.jsonl
    state.json
    report.md
```

Record schemas are normative in [contracts.md](contracts.md).

## 7. Books: real and shadow

The real book holds only fills from emitting algos' accepted tickets. The shadow book
simulates — with the identical fill model — every probe-algo intent and every risk-
rejected intent from emitting algos. Metrics for every algo carry `n_real`, `n_shadow`,
and the shadow-to-real conversion context, because the predecessor program measured that
shadow leaderboards overstate: the apparent edge concentrated in candidates the rules
refused (conversion examples on record: 29 shadow candidates -> 2 emitted trades;
13 -> 1). The console must render shadow metrics with that caveat visible, never as a
forecast of promoted performance.

## 8. Signal engine scope (provider)

The signal catalog contains only signals computable from data that exists:

- Per-bar technicals on 1-minute bars (VWAP and distance, opening range, gap metrics,
  ATR-style ranges, tape/volume features, prior-day levels) — ported from dt's feature
  computation.
- Calendar/event signals: earnings proximity, implied-move percentage from manual options
  captures (day-constant; absent before capture began 2026-08-01).
- Explicitly NOT in the catalog: Level 2 / order-book signals (no such data exists,
  historically or live), NBBO spread history (live-capture only, accrues forward),
  news-sentiment scores (raw news is archived but measured to carry no intraday signal).

Signals are requested by name with an `asof`; the engine computes on demand with an
in-session memo cache. Cross-strategy consistency comes from the shared implementation;
correctness comes from the PIT rules in §4.

## 9. Recorded amendments to the original redesign proposal

- A1 — Data honesty: the proposal's provider listed Level 2 books and sentiment among
  ingested data. Removed (§8): no such source exists for this program, and news was
  measured signal-free. The provider catalog is exactly what §6 stores.
- A2 — Orchestrator is a runner, not a daemon: lifecycle management means in-process
  instantiation of algos from config inside one operator-started session process.
  No process spawning, no standing scheduler.
- A3 — Broker abstraction ships in three grades: `sim` (backtest/paper fills with
  slippage model), `manual` (live default: renders an executable order ticket to the
  terminal and console; the Dev executes at the broker and confirms; fills recorded),
  `api` (interface-complete adapter that raises `BrokerNotConfigured` until a broker
  trading API is wired and the double interlock is set: `execution.yaml
  live_orders: true` AND environment `TRADER_LIVE=1`). This implements the proposal's
  execution abstraction while keeping order transmission a deliberate, Dev-flipped
  switch.
- A4 — Comparison with caveats: the strategy-comparison surface carries shadow/real
  conversion context per §7 rather than a bare leaderboard.
- A5 — Fresh evidence base: dt's replay byte-hash is not carried. Its replacement for
  reproducibility: `session_start` records the sha256 of the resolved config bundle plus
  package version, and backtests over identical stored bars and configs must be
  deterministic (integration-tested in Wave 2).
- A6 — Predecessors are dismantled, never ported whole (Dev direction 2026-08-01):
  there is no `engines/` folder. The dismantling map: dt's feature computation, bar
  store, ingest, calendar, and options capture -> provider; dt's rules grammar, setup
  roster, and bracket formulas -> algos; dt's fill semantics, sizing slots, rails, and
  live-loop pattern -> execution; dt's report shapes and win's uniform-score/digest
  ideas -> console. daytrader's playbooks are not in the initial roster; if they enter,
  they enter as probe algos. daytrader's WAL storage layer is not carried.
- A7 — Execution is the orchestrator (Dev direction 2026-08-01): the proposal's
  component 2 (orchestrator + virtual clock) and component 4 (execution + risk) are one
  component here. `algos` is a pure library the orchestrator loads from the roster;
  nothing else calls it. This trades a component boundary for simplicity; the seam that
  preserves separate testability is the contracts layer, not the process split.

## 10. Testing strategy

- Wave 0 ships `trader.contracts.testing`: deterministic synthetic bar-day generator,
  fake MarketData / Clock / Broker / TelemetryWriter, and golden fixture files (a sample
  bar parquet, intents JSONL, telemetry JSONL) under `tests/fixtures/`.
- Each component's suite runs with only `contracts` + fixtures — no sibling imports:
  provider tests against raw-dump fixtures; algos against a scripted fake MarketData;
  execution against fake MarketData/algos/fixtures (its own loop tested with contract
  fakes end to end); console against a telemetry fixture.
- Wave 2 adds integration tests: a full backtest over migrated real bars must run
  end-to-end, twice, byte-identical telemetry (determinism gate per A5); a mock live
  session (recorded day replayed through the paper path) must reconcile with the
  backtest of the same day.
- Known vendor-data lesson carried forward: ingest validates each bar against its
  neighbors (the predecessor store contains a documented bad tick, SNDK 2026-07-16
  16:27Z printed at 1615.00 against a 1430-1442 tape); ingest must quarantine, not
  ingest, such bars.

## 11. Migration and decommissioning

1. Data: copy bar stores, raw dumps, events/options captures, news archive, and calendar
   from the old repos into §6 layout (Wave 3), with file-count and checksum verification.
2. Knowledge: copy `docs/` of dt and daytrader and all of win's design into
   `docs/archive/{dt,daytrader,win}/`, plus each old repo's `git log --stat` exported to
   a text file there (the old histories have no remotes; this is their surviving record).
3. Removal: only after Wave 4 verification (integration green, console live against a
   real session, migration checksums verified) and an explicit final Dev confirmation in
   chat, the three directories `/Users/xup/dh/win`, `/Users/xup/dh/dt`,
   `/Users/xup/dh/daytrader` are deleted. Nothing is deleted before that point.
