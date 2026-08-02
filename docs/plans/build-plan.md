# trader — build plan

Waves of small Codex tasks. A wave's tasks run inside one component agent sequentially;
the four Wave 1 components run in parallel, one agent + one worktree each. Read
[architecture.md](../design/architecture.md) and [contracts.md](../design/contracts.md)
first; contracts are normative.

## Operating rules (every agent)

1. All code is written by Codex. Every file change under `src/`, `tests/`, `config/`,
   and `pyproject.toml` is produced by a Codex task — the agent never edits those files
   by hand, including one-line fixes (dispatch a fix task instead). Agents may write
   only their own scratch/report files and `docs/` updates explicitly assigned to them.
2. Codex invocation, from inside the worktree:
   `codex exec -m gpt-5.6-sol -c 'model_reasoning_effort="xhigh"' -s workspace-write "<task text>"`
   One bounded task per invocation (one module or one feature plus its tests, target
   10-20 minutes). Give Codex: the goal, exact file paths to create/modify, the
   contract excerpts it must satisfy, port-source paths (read-only) in the old repos,
   and the test command it must leave green.
3. Worktree: `git -C /Users/xup/dh/trader worktree add -b <branch> /Users/xup/dh/wt-<branch> main`.
   Never touch the main checkout. Commit after each Codex task with message
   `<component>: <task-id> <summary>`.
4. Tests run in the foreground, never backgrounded. Task done = its tests pass AND the
   whole component suite passes (`.venv/bin/python -m pytest tests/<component> -q`).
5. Old repos `/Users/xup/dh/dt`, `/Users/xup/dh/daytrader`, `/Users/xup/dh/win` are
   read-only port sources. Never modify them.
6. Report back: STATUS (DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED), branch,
   commits, per-task test evidence (command + pass counts), concerns.

## Wave 0 — scaffold + contracts (one agent, branch `wave0-contracts`, blocks Wave 1)

- T0.1 `pyproject.toml` (package `trader`, deps pandas/pyarrow/pyyaml, dev dep pytest,
  console script `trader = trader.cli:main`), `.venv` created via
  `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`, `src/trader/cli.py` as a
  thin argparse dispatcher that lazily imports `trader.<component>.cli` modules and
  calls their `register(subparsers)` if present (components own their subcommands; no
  shared-file conflicts). Smoke test: `trader --help` exits 0 with no components present.
- T0.2 `trader.contracts` core per contracts.md: types, errors, clock, market, algo,
  intents, orders, broker, risk. Tests: construction, frozen-ness, literal fields.
- T0.3 telemetry records + serde. Tests: round-trip `from(to(x)) == x` for every record
  type; JSONL append/read; ISO-8601 Z timestamps.
- T0.4 `trader.contracts.testing`: `synthetic_day` (deterministic by seed),
  `FakeClock`, `FakeMarketData` (real PIT enforcement), `CollectingTelemetry`,
  `FakeBroker`, `make_fixtures` module; generate and commit `tests/fixtures/`. Tests:
  same-seed determinism, `FakeMarketData` raises `LookaheadError` on future asof.

## Wave 1 — four components in parallel (after Wave 0 merges)

Each agent branches from main, builds only its own package + tests, and may import only
`trader.contracts` (execution additionally composes provider and algos at runtime, but
its unit tests use contract fakes). Port sources are cited per task; port semantics,
not file layout.

### 1-A provider (branch `comp-provider`)

- A1 bar store: parquet day files per §6, `bars_1m`/`bars_1d` with completed-bar
  visibility (PIT rule 1) and `LookaheadError`. Port: `/Users/xup/dh/dt/datalayer/bars.py`,
  cursor honesty from `/Users/xup/dh/dt/datalayer/mock_feed.py`.
- A2 ingest: Robinhood raw JSON dumps -> day parquet; ETF +-2x validation; neighbor
  bad-tick quarantine per provider.yaml. Port: `/Users/xup/dh/dt/datalayer/robinhood_ingest.py`.
  Fixture: craft a small raw dump matching the vendor format (read one file under
  `/Users/xup/dh/dt/data/robinhood_raw/` for the shape).
- A3 calendar: generate `config/calendar.yaml` (versioned; the data/ tree is git-ignored) from
  `/Users/xup/dh/dt/datalayer/market_calendar.py` (static, ex-ante) + MarketCalendar impl.
- A4 signal engine: named signals with per-session memo cache, ported from
  `/Users/xup/dh/dt/engine/features.py`; registry doc generation to `docs/signals.md`
  (catalog style from `/Users/xup/dh/dt/engine/signal_registry.py`, scope per
  architecture §8 — no L2, no NBBO-history, no sentiment). Tests: every signal at t
  equals the same computation on the truncated frame (PIT rule 2).
- A5 events: earnings calendar reader, options implied-move captures reader (a capture
  is invisible to any asof before its capture time — port
  `/Users/xup/dh/dt/datalayer/options.py` read side), `event()` kinds per contracts.
- A6 fetch/ingest CLI: `trader fetch` via the headless Claude MCP relay (port
  `/Users/xup/dh/dt/datalayer/mcp_relay.py`; unit-test command construction only — the
  relay needs interactive MCP auth), `trader ingest`, `trader validate`.

### 1-B algos (branch `comp-algos`)

- B1 rules grammar evaluator ported from `/Users/xup/dh/dt/engine/rules.py`
  (source/operator/value, `all:` groups, roles gate/veto/direction/confirmation,
  `applies_to`/`except` scoping). Fresh minimal tests per operator and role.
- B2 declarative setup -> `Algo` adapter ported from
  `/Users/xup/dh/dt/engine/algo_executor.py`: phases, minute windows, `one_shot`,
  triggers, ordered sides, named bracket formulas. Algos stay pure: MarketData in,
  intents out, nothing else.
- B3 fill `config/algos.yaml`: replace every PORT marker with values ported from
  `/Users/xup/dh/dt/rules/rules.yaml` (v1.6) and `/Users/xup/dh/dt/rules/algos.yaml`
  (roster v1.2) — every number must trace to those files, none invented. Per-setup
  smoke test: each setup fires on a crafted synthetic day.
- B4 bracket construction: entry/stop/target formulas and SNDK->ETF price translation
  ported from the bracket parts of `/Users/xup/dh/dt/engine/risk.py`; intents carry
  instrument-scale levels per contracts.

### 1-C execution — the orchestrator (branch `comp-execution`)

- X1 config loading: all six YAMLs -> typed objects; `config_sha256` over the resolved
  bundle; unknown keys are errors.
- X2 clocks: `BacktestClock` (synthetic, `sleep_until` advances instantly), `LiveClock`
  (wall time, injectable sleep — port the injection pattern from
  `/Users/xup/dh/dt/live_run.py` `run(sleep=..., now=...)`).
- X3 risk engine: slot sizing (`equity * capital_fraction / day_slots`, floor to whole
  shares, reject over-slot rather than trim) + rails per risk.yaml (entries/day,
  one-position, no-hedge, mutes, reversal cooldown). Port:
  `/Users/xup/dh/dt/engine/risk.py`, rails in `/Users/xup/dh/dt/engine/core.py`.
  Tests: extreme and invalid intents are rejected deterministically, each rail alone.
- X4 sim broker: next-open entry fills, stop/target monitoring (stop wins ties; open
  beyond a level fills at open), era slippage table from execution.yaml (missing
  symbol-month raises), EOD force-flat. Port fill semantics:
  `/Users/xup/dh/dt/engine/core.py` entry at 204-216, exits at 417-450; slippage:
  `/Users/xup/dh/dt/backtest/replay.py` 83-98.
- X5 books + metrics: real and shadow portfolios, R-multiple accounting
  (post-slippage numerator, pre-slippage denominator — convention documented at
  `/Users/xup/dh/dt/backtest/replay.py` 7-25), `AlgoMetrics` computation per book.
- X6 manual broker (live default: boxed terminal ticket render — port
  `/Users/xup/dh/dt/live_run.py` `render_entry`/`_box` — plus `trader fills record` to
  log Dev-confirmed fills) and api broker stub (raises `BrokerNotConfigured` unless
  `live_orders: true` AND env `TRADER_LIVE=1` AND an adapter is wired — there is none).
- X7 session runner per architecture §5 against contract fakes; session directory +
  telemetry JSONL writer (line-flushed). In backtest mode every telemetry envelope `ts`
  is clock time (bar time), never wall time. Tests: scripted fake algo/broker produce
  an exact expected telemetry sequence.
- X8 `trader run`: mode selection, session id `<mode>-<YYYYMMDD>-<HHMMSS>`, paper/live
  cadence loop (`cycle_minutes`), Ctrl-C -> force-flat + `session_end` before exit.
  Composition: build real MarketData from provider, algos from roster factories —
  behind an import boundary so X1-X7 unit tests never need siblings installed.

### 1-D console (branch `comp-console`)

- E1 SSE server on stdlib `http.server`: `/events` tails a session's telemetry.jsonl
  (offset polling), `/sessions` lists session dirs, config from console.yaml,
  localhost only. Tests: ephemeral port, fixture telemetry, assert SSE frames.
- E2 dashboard page: one self-contained HTML+JS file, no external assets: algo
  leaderboard (must show `n_real`, `n_shadow`, and the shadow-caveat text per
  architecture §7), cum-R sparkline (canvas), open positions, intents feed, error
  panel, mode badge. Tests: page serves 200 and contains the required elements.
- E3 post-session report: telemetry -> markdown (per-algo metrics incl. shadow
  caveat, session summary, rejections by rule) as `trader report <session>`; golden
  test on fixture telemetry. Metric shapes reference:
  `/Users/xup/dh/dt/backtest/evaluate.py`.
- E4 `trader console` CLI: serve newest or named session; follow live file growth.

### 1-E console v2 — workbench and results (branch `console-v2`, Dev directive 2026-08-02)

Per amendment A11. Four requirements, in the Dev's words: the live dashboard shows
running events and logs; each component can be exercised individually from the dashboard;
for example a provider test takes typed parameters (ticker, date, signal or event name)
and returns the corresponding data; and the latest backtest results are browsable the way
dt's dashboard worked — executive summary plus drill-down.

- V1 live view: a running session's events stream in as they happen (intents, tickets,
  fills, rejections with their rule, algo errors), plus a raw log pane showing the
  telemetry lines themselves. Auto-follows the newest session; reconnects if the stream
  drops; shows a clear idle state when no session is running.
- V2 workbench — provider panel: operator enters symbol, date/asof, and picks an
  operation (1-minute bars, daily bars, a named signal from the registry, an event kind,
  calendar lookups such as previous session or session close). Results render as a table
  plus the raw JSON. Errors — including `LookaheadError` — render as readable messages,
  never a traceback, and a lookahead refusal is a legitimate, well-labelled outcome.
- V3 workbench — algos panel: pick any roster algo, pick a stored day, run it over that
  day and show every candidate it produced with the rule trace (which clauses fired,
  gate/veto verdict, `gates_pass` stamp) and the resulting bracket.
- V4 workbench — execution panel: hand-enter an intent (algo, side, instrument, entry,
  stop, target) plus account state, and show the risk decision: accepted with the sizing
  arithmetic, or rejected naming the rail. Simulation only, per A11's safety rules.
- V5 results view, modeled on dt's dashboard (read
  `/Users/xup/dh/dt/research/build_dashboard.py` and
  `/Users/xup/dh/dt/docs/runbooks/dashboard.md` for the layout that worked): executive
  summary — window, sessions, trades, win rate, mean R, profit factor, cumulative R, max
  drawdown, final equity — over a master-detail drill-down: days list → that day's chart
  and trades → single-trade detail. A global algo filter applies across all panes.
  Per-algo metrics carry `n_real`, `n_shadow`, and the shadow caveat per architecture §7.
- V6 session picker: switch between past sessions, defaulting to the most recent
  backtest; the results view always states which session it is showing.

## Wave 2 — integration (one agent, after all Wave 1 merges)

- I1 copy a five-session slice of real bars (all seven symbols) from
  `/Users/xup/dh/dt/data/bars/` into the data root; run a real backtest end-to-end.
- I2 determinism gate: same window, same config, run twice -> byte-identical
  telemetry.jsonl (A5).
- I3 paper-mode rehearsal: replay a recorded day through the paper path at speed;
  reconcile its decisions against the backtest of the same day.
- I4 console smoke against the produced session; report generated.
- Fix seams via Codex tasks in the integration worktree.

## Wave 3 — migration + archive (one agent)

- M1 full data migration per architecture §11.1 with file counts + sha256 manifest.
- M2 knowledge archive per §11.2 (docs of dt/daytrader, win design, `git log --stat`
  exports of all three repos).

## Wave 4 — comparison gate, final review, decommission

- C0 comparison gate (Dev directive 2026-08-01): before any decommission, run dt's own
  replay (`dt.py backtest` in /Users/xup/dh/dt) and trader's backtest over the same
  window on the same bars, with trader configured to dt's posture (max_entries_per_day
  3, day_slots 2, dt's slippage eras), and produce a side-by-side report: trades, win
  rate, mean R, profit factor, per-setup real + shadow. The Dev judges whether trader
  is at least somewhat better. Exact equality is not expected (fresh build, "start
  over" ruling); unexplained large divergences are findings to investigate, not
  round-off.
- Final whole-branch review (most capable model), fix wave, then Dev confirmation in
  chat before `/Users/xup/dh/{win,dt,daytrader}` are removed. Removal is the last step
  and never runs before the comparison report is presented and that confirmation given.
