# Trading system redesign: four parts, one writer per table

Status: proposal, 2026-08-17.

## The isolation rule

Three services and one webserver: bars, events, algo, dashboard. All data
lives in one SQLite database. Each table has exactly one writing service;
every other part reads it read-only. Parts communicate through data, never
through calls between services.

| table   | writing service       | readers         |
| ------- | --------------------- | --------------- |
| bars    | bars                  | algo, dashboard |
| bar_jobs | bars                 | bars            |
| events  | events                | algo, dashboard |
| trades  | algo                  | dashboard       |
| outputs | algo                  | dashboard       |
| configs | algo                  | dashboard       |
| logs    | every service, own rows only | dashboard |

One shared config file holds the ticker list, the signal and algo
definitions, the early-close days, the live-polling window, and the
evaluation window (a day count). Inputs: bars and events take the ticker
list; algo takes the ticker list plus the signal and algo definitions.

There is no calendar table. The early close is the only session fact that
cannot be inferred, and it lives in config. Time of day comes from the
timestamp, a holiday is a day with no bars, and the prior session is a data
walk.

## 1. bars (service)

Collects minute bars from Robinhood and preserves their source quality. One
process does the initial backfill, live poll, and nightly sweep. Writes are
idempotent upserts keyed by ticker and timestamp, so a repeated fetch is
harmless. Every write stamps `fetched_at`, so a reader can see what changed and
when.

Initial backfill:

- Fetch 120 days of minute bars once for each ticker.
- Record the fixed window, per-session progress, and completion in `bar_jobs`.
  A failed backfill resumes after its last committed session, while a completed
  ticker does not repeat the initial fetch.
- A ticker added to config gets the same backfill before its next poll or
  sweep.

Live poll:

- Active from premarket until a set time after the close; the window is in
  config.
- Polls once per minute and writes minute bars.
- Every poll starts from the last stored bar, so an outage catches up by
  itself. Each poll fetches at most seven calendar days, so a long outage
  catches up in bounded steps. No reconcile step exists.
- A response must contain every requested ticker. An omitted ticker fails the
  cycle and retries; it is never treated as successful progress.

Nightly sweep:

- Once per day, after the close, in bulk: the last 30 days of minute bars
  for every ticker, always, with no gap detection. The sweep is the gap
  repair.
- Robinhood can return synthetic gap-fill bars at any age and marks them with
  `interpolated: true`. Older minute requests can contain only these rows.
- Store every returned bar and preserve the `interpolated` flag so each reader
  can decide how to use it.

## 2. events (service)

Deferred: this service is not part of the current build. The section below
is the contract for when it is; nothing reads events until the sentiment
signal exists.

- Collects events, maps each one to a ticker, and defines the date range
  where the event has impact.
- A row: ticker, event, event time, window start, window end, direction,
  strength. Direction is a value from -1 to 1, and 0 is neutral. Strength
  is the size of the impact; a high strength means high expected
  volatility.
- A non-directional event (earnings, implied move) stores direction 0 with
  a high strength.
- Cadence: once per day, before the open. A run upserts the current facts
  and deletes a future row that its source no longer lists. A row with an
  ended window stays forever as history.

The service also defines the impact formula. For a date `d` inside the
window, the weight is 0 at the window edges, 1 at the event time, and
linear between; the impact is the weight times (direction, strength).
Outside the window the impact is (0, 0). The read interface returns each
active event with its impact and does not consolidate across events.
Consolidation belongs to a signal: a planned sentiment signal will produce
one (direction, strength) from events, bars, or both. That signal is a
separate task.

## 3. algo (service)

One service runs the algos and contains the algo logic: a core plus a
loop. Input: the ticker list and the signal and algo definitions; the
bar timestamps supply `t`. One call carries all work:
`core(ticker, t, algos=None)` returns the signal values at `t` and one
`(is_entry, is_close_all)` pair per algo. `algos=None` runs every enabled
algo; a list restricts the call to those algos.

### The core

- No clock, no side effects. Data input: the bars, events, and trades
  tables, read-only.
- Two layers with a strict rule: a signal (vwap, sma, and so on) queries
  bars, the events table, and other signals; an algo queries signal
  outputs, the outputs of other algos, and its own prior outputs.
- Algos are independent. Nothing combines them; to combine behavior,
  compose an algo from other algos. Every entry and exit belongs to
  exactly one algo. An algo that another algo reads evaluates like a
  signal; whether it also trades is its own config flag.
- A signal's output is opaque: a number for sma, a pair, anything. The algo
  that reads a signal interprets the output itself.
- Everything reads only values at or before `t` — the whole no-lookahead
  guarantee.
- Each algo's config declares the signals it reads. A new signal or algo is
  a config entry, not an engine change.
- Bulk and real time are the same core with different timestamps.

### The algo output contract

- Input: besides its signals, an algo receives its open entries at `t`
  from the trades table; entry prices come from bars.
- Output at `t` is one pair of booleans: `(is_entry, is_close_all)`. The
  algo decides among three moves from its open entries: `is_entry` opens
  one more unit, `is_close_all` closes every open unit, and both false is
  quiet. Both true does not occur.
- An exit device such as a trail stop lives inside the algo: the open
  entries give the anchor, and the bars since the anchor give the level.
- Position state lives in the database only: open units are the entries
  since the last `exit_all` in the trades table, regardless of version.
  The loop keeps no position in memory. Work under one ticker runs oldest
  first, so an algo's prior outputs exist when it reads them.
- The pair is the one fixed shape in outputs, because the loop and the
  dashboard both read it. A signal's output stays opaque JSON.

### Config defines every signal and every algo

- A signal and an algo have the same shape in config: an entry in a
  name-keyed map with `inputs` and `params`. Example:

  ```json
  "signals": {
    "sma20": { "inputs": ["bars"], "params": { "length": 20 } },
    "shape": { "inputs": ["bars"], "function": "shape_v1", "params": {} }
  },
  "algos": {
    "dip": { "trades": true, "inputs": ["sma20"],
             "params": { "drop_pct": 1.5, "trail_pct": 0.8 } }
  }
  ```

- The map key is the name, and it is the `kind` value in outputs. Presence
  in the map means enabled; to disable a node, remove the entry and bump
  the version.
- `inputs` declares what a node reads. `trades` marks an algo that writes
  trade rows; an algo without it evaluates like a signal.
- All parameters live in `params`. A complicated node adds `function`,
  which points to a function in the code, and the function hard-codes
  nothing.
- The version is a field in the config file. The user bumps it by hand
  with every change to a signal or an algo. When the algo service sees a
  version that is not in the configs table, it stores the full file under
  that version. A stored version therefore always resolves to readable
  parameters, also after the config file has moved on.
- The service trusts the version field, not the content. When the file
  differs from the stored content under the same version, the loop logs a
  warning; the fix is a version bump.

### A change is a standard operation

Add a signal, add an algo, or change a parameter: edit the config file
and bump the version field. That is the whole procedure. The algo service
reacts on its own:

1. It sees the new version and stores the file in configs.
2. The work rule (see the loop) now matches every pair in the evaluation
   window, because no output rows exist under the new version, and fills
   them. The rows are keyed, so the run is idempotent and resumable.
3. The dashboard shows the new version next to the old ones as the rows
   land.

Easy: one file edit, no deploy. Fast: the work is bounded by the
evaluation window, never by the full history. Visible: history for the
new version appears without a live bar. A new function is the one case
that also needs a code change.

### The output cache

Every evaluation is stored in outputs: one row per signal and one per algo,
keyed by ticker, timestamp, name, and the config version, stamped with
computed_at. A rerun under the same version updates the row in place; a
changed config writes rows under a new version, and the old rows stay for
reference. The dashboard reads the cache and never writes it.

Every enabled signal is evaluated and stored each tick, whether or not an
algo reads it. A visual-aid signal, such as a shape forecast, is stored the
same way, and the JSON output holds a series or a shape as easily as a
number.

### The loop

- No private clock. The loop polls the database and the config file every
  30 seconds and reacts to what changed.
- The work list is every (ticker, timestamp) pair inside the evaluation
  window where outputs has no row under the current version. This one
  rule covers a live bar and a config change; the loop does not tell the
  cases apart.
- Work updates outputs; a rerun is a safe upsert because the rows are
  keyed and the core is deterministic. Only the newest bar of a ticker
  also trades: the loop writes trade rows for its points, and older work
  never writes a trade.
- A trade row is ticker, algo, exact timestamp, action. `is_entry` writes
  an entry row and opens one unit. `is_close_all` writes an `exit_all` row
  and closes every open unit of the algo.
- An algo exits only while it has open units for the ticker. This is
  enforced in code; a CHECK cannot span rows.
- A trade has no size: the unit is the size. Performance is the percent
  result per unit, and the account result is the sum over units. The price
  of a point at `t` is the close of bar `t`.

## 4. dashboard (webserver)

Shows the state as it happens. It reads the whole database read-only,
renders vertical-first so a phone screen works, writes nothing, and triggers
nothing. Data views come from the tables. Service liveness comes from local,
read-only health APIs.

| view                                       | source                                  |
| ------------------------------------------ | --------------------------------------- |
| minute-bar chart over all days             | bars                                    |
| entry and exit overlays on the bars        | trades, per algo                        |
| click-through detail of a signal or algo   | outputs (values) + configs (parameters) |
| algo performance across parameter sets     | outputs across versions, priced by bars |
| visual-aid signals, e.g. a shape forecast  | outputs, like any signal                |
| service status                             | local health APIs                       |
| service history and problems               | logs                                    |

Ad-hoc views may call the core directly; the call writes nothing.

## Service status and logs

Bars exposes `GET /health` on a loopback-only port. Dash calls it to check
process liveness; the call writes nothing. Bars appends only run summaries and
problems to the logs table, not periodic heartbeats. The dashboard reads logs
for history, warnings, and errors. The table remains safe because it is
append-only and each service writes only rows tagged with its own name.

## Database schema

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE bars (
    ticker     TEXT    NOT NULL,
    ts         INTEGER NOT NULL,  -- bar start, epoch seconds, UTC
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     INTEGER NOT NULL,
    interpolated INTEGER NOT NULL DEFAULT 0,  -- Robinhood gap-fill flag
    fetched_at INTEGER NOT NULL,  -- write time, epoch seconds, UTC
    PRIMARY KEY (ticker, ts),
    CHECK (interpolated IN (0, 1))
);

CREATE TABLE bar_jobs (
    kind         TEXT    NOT NULL,  -- backfill or sweep
    scope        TEXT    NOT NULL,  -- ticker, or all for a sweep
    target       TEXT    NOT NULL,  -- day count, or scheduled sweep date
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    progress_ts  INTEGER,           -- last committed provider session
    started_at   INTEGER NOT NULL,
    completed_at INTEGER,
    PRIMARY KEY (kind, scope, target),
    CHECK (kind IN ('backfill', 'sweep')),
    CHECK (window_start <= window_end),
    CHECK (progress_ts IS NULL OR
           (window_start <= progress_ts AND progress_ts <= window_end)),
    CHECK (completed_at IS NULL OR progress_ts IS NOT NULL)
);

CREATE TABLE events (
    ticker       TEXT    NOT NULL,
    event        TEXT    NOT NULL,  -- kind, e.g. 'earnings'
    event_ts     INTEGER NOT NULL,  -- the event time, the impact peak
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    direction    REAL    NOT NULL DEFAULT 0,  -- -1 to 1; 0 is neutral
    strength     REAL    NOT NULL DEFAULT 0,  -- high = high volatility
    PRIMARY KEY (ticker, event, event_ts),
    CHECK (window_start <= event_ts AND event_ts <= window_end),
    CHECK (direction >= -1 AND direction <= 1),
    CHECK (strength >= 0)
);

CREATE TABLE trades (
    ticker TEXT    NOT NULL,
    algo   TEXT    NOT NULL,
    ts     INTEGER NOT NULL,  -- exact entry or exit time, epoch seconds, UTC
    action TEXT    NOT NULL,
    PRIMARY KEY (ticker, algo, ts, action),
    CHECK (action IN ('entry', 'exit_all'))
);

CREATE TABLE outputs (
    ticker      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    kind        TEXT    NOT NULL,  -- a signal name or an algo name
    config      TEXT    NOT NULL,  -- the config file version
    output      TEXT    NOT NULL,  -- JSON
    computed_at INTEGER NOT NULL,  -- evaluation time, epoch seconds, UTC
    PRIMARY KEY (ticker, ts, kind, config)
);

CREATE TABLE configs (
    version    TEXT    NOT NULL PRIMARY KEY,  -- the version field from the config file
    first_seen INTEGER NOT NULL,              -- epoch seconds, UTC
    content    TEXT    NOT NULL               -- the full config file, JSON
);

CREATE TABLE logs (
    ts      INTEGER NOT NULL,  -- epoch seconds, UTC
    service TEXT    NOT NULL,  -- bars, events, algo
    level   TEXT    NOT NULL,
    message TEXT    NOT NULL,
    CHECK (level IN ('info', 'warn', 'error'))
);
CREATE INDEX logs_service_ts ON logs (service, ts);
```

Notes:

- WAL: readers never block the writer. Each process has its own connection
  and busy timeout. All timestamps are epoch seconds, UTC.
- Bar writes are idempotent upserts; a repeated fetch is harmless.
- `bar_jobs` holds resumable work state. Logs describe runs and problems but
  never control work.
- `bars.interpolated` preserves Robinhood's gap-fill flag; no returned bar is
  discarded at ingest.
- `trades` binds every row to one algo; nothing consolidates across algos.
  No price (recomputable) and no size (a row is one unit).
- `outputs` grows fastest; `logs` grows steadily.
- `configs` maps every stored version back to the full config file.
- `logs` is append-only and the one table with an extra index.
- No other indexes beyond the primary keys. Config stays in files; the
  configs table is a record of versions, not the source of truth.

## Diagram

```mermaid
flowchart TB
    BARS[bars service<br/>minute poll from the last bar<br/>+ nightly 30-day sweep]
    EVENTS[events service<br/>daily — deferred]

    BARS --> BT[(bars)]
    BARS --> BJ[(bar_jobs)]
    EVENTS --> ET[(events)]

    subgraph DB [one database — one writing service per table]
        BT
        BJ
        ET
        TR[(trades)]
        OUT[(outputs)]
        CFG[(configs)]
        LG[(logs)]
    end

    subgraph ALGO [algo service]
        LOOP[loop<br/>polls the db every 30 s] -- work rule --> CORE[core<br/>data → signals → algos<br/>all config-defined]
    end

    BT -. read only .-> CORE
    ET -. read only .-> CORE
    LOOP --> TR
    LOOP --> OUT
    LOOP --> CFG
    BARS & EVENTS & LOOP --> LG
    DB -. read only .-> DASH[dashboard<br/>read-only webserver]
    BARS -. GET /health .-> DASH
```

## Decisions (2026-08-17)

1. No calendar table; the early-close days live in config.
2. No backtest concept: the loop applies the core to timestamps, in bulk
   or in real time. An output updates in place per version; old versions
   stay for reference.
3. No version hash: the user bumps the version field in config by hand.

## Open

1. The prune policy for old output versions.
