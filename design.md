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
| events  | events                | algo, dashboard |
| trades  | algo                  | dashboard       |
| outputs | algo                  | dashboard       |
| configs | algo                  | dashboard       |
| logs    | every process, own rows only | dashboard |

One shared config file holds the ticker list, the signal and algo
definitions, the early-close days, and the live-polling window. Inputs: bars
and events take the ticker list; algo takes the ticker list plus the signal
and algo definitions.

There is no calendar table. The early close is the only session fact that
cannot be inferred, and it lives in config. Time of day comes from the
timestamp, a holiday is a day with no bars, and the prior session is a data
walk.

## 1. bars (service)

Collects minute bars from Robinhood and owns their quality. One process
does both jobs: a live poll each minute and a nightly sweep. Writes are
idempotent upserts keyed by ticker and timestamp, so a repeated fetch is
harmless.

Live poll:

- Active from premarket until a set time after the close; the window is in
  config.
- Polls once per minute and writes minute bars.
- Every poll starts from the last stored bar, so an outage catches up by
  itself. No reconcile step exists.

Nightly sweep:

- Once per day, after the close, in bulk: the last 30 days of minute bars
  for every ticker, always, with no gap detection. The sweep is the gap
  repair.
- 30 days sits inside the ~41-day boundary where Robinhood starts to serve
  synthetic bars (marked only by `interpolated: true`), so every fetched bar
  is real.
- Simple validation: drop interpolated rows and rows with unordered prices,
  write the rest.

## 2. events (service)

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

One service runs the algos and contains the algo logic: a stateless core
plus a loop. Input: the ticker list and the signal and algo definitions; the
clock supplies the timestamp. One call carries all work:
`core(ticker, t, algos=None)` returns the signal values at `t` and the entry
and exit points per algo.

### The stateless core

- A pure function; no clock, no side effects. Data input: the bars and
  events tables, read-only.
- Two layers with a strict rule: a signal (vwap, sma, and so on) queries
  bars, the events table, and other signals; an algo queries signal outputs
  only.
- A signal's output is opaque: a number for sma, a pair, anything. The algo
  that reads a signal interprets the output itself.
- Everything reads only values at or before `t` — the whole no-lookahead
  guarantee.
- Each algo's config declares the signals it reads. A new signal or algo is
  a config entry, not an engine change.
- Bulk and real time are the same core with different timestamps.

### Config defines every signal and every algo

- A signal and an algo have the same shape in config: name, parameters,
  inputs.
- All parameters live in config. A complicated node points to a function in
  the code, and the function hard-codes nothing.
- The version is one hash of the whole config file; there is no per-node
  hash. When the algo service sees a new version, it writes the full file
  to the configs table. A stored version therefore always resolves to
  readable parameters, also after the config file has moved on.

### A change is a standard operation

Add a signal, add an algo, or change a parameter: edit the config file.
That is the whole procedure. The algo service reacts on its own:

1. It sees the new file hash and stores the file in configs.
2. It backfills outputs for every enabled node over all stored bars, under
   the new version. The backfill is a bulk run of the loop, and the rows
   are keyed, so the run is idempotent and resumable.
3. The dashboard shows the new version next to the old ones as the rows
   land.

Easy: one file edit, no deploy. Fast: the recompute is a pure function
over a small table. Visible: history for the new version appears without a
live tick. A new function is the one case that also needs a code change.

### The output cache

Every evaluation is stored in outputs: one row per signal and one per algo,
keyed by ticker, timestamp, name, and the config version. A rerun under the
same version updates the row in place; a changed config writes rows under a
new version, and the old rows stay for reference. The dashboard reads the
cache and never writes it.

Every enabled signal is evaluated and stored each tick, whether or not an
algo reads it. A visual-aid signal, such as a shape forecast, is stored the
same way, and the JSON output holds a series or a shape as easily as a
number.

### The loop

- Owns the trading clock. Each minute: call the core, act on the points.
- Consolidates the per-algo points into one decision per ticker and minute,
  and writes it as a trade row: ticker, exact timestamp, entry or exit.
- A trade has no size: every entry and every exit is one unit. Two open
  entries are two units. Performance is the percent result per unit,
  priced from bars, and the account result is the sum over units.
- Writes an exit only while recorded exits are below entries. This is
  enforced in code; a CHECK cannot span rows.
- There is no separate backtest. The loop applies the core to timestamps,
  in bulk over a past range or in real time each minute. A bulk run writes
  outputs and no trades; the rows are keyed and the core is deterministic,
  so a rerun is a safe upsert.

## 4. dashboard (webserver)

Shows the state as it happens. It reads the whole database read-only,
renders vertical-first so a phone screen works, writes nothing, and triggers
nothing. No service exposes an API; every view is served by the tables.

| view                                       | source                                  |
| ------------------------------------------ | --------------------------------------- |
| minute-bar chart over all days             | bars                                    |
| entry and exit overlays on the bars        | trades; outputs for the per-algo points |
| click-through detail of a signal or algo   | outputs (values) + configs (parameters) |
| algo performance across parameter sets     | outputs across versions, priced by bars |
| visual-aid signals, e.g. a shape forecast  | outputs, like any signal                |
| service status, history, problems          | logs                                    |

Ad-hoc views may call the core directly; a pure call is a read.

## Service status (logs)

Every process appends to the logs table: a heartbeat each cycle, a summary
row per run (bars fetched, rows rejected), and a row per problem. The
dashboard derives everything from it: status is the freshest heartbeat per
process, history is the run summaries, and problems are the warn and error
rows. This is the one shared-writer table; it is safe because it is
append-only and each process writes only rows tagged with its own name.

## Database schema

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE bars (
    ticker  TEXT    NOT NULL,
    ts      INTEGER NOT NULL,  -- bar start, epoch seconds, UTC
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  INTEGER NOT NULL,
    PRIMARY KEY (ticker, ts)
) WITHOUT ROWID;

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
) WITHOUT ROWID;

CREATE TABLE trades (
    ticker TEXT    NOT NULL,
    ts     INTEGER NOT NULL,  -- exact entry or exit time, epoch seconds, UTC
    action TEXT    NOT NULL,
    PRIMARY KEY (ticker, ts, action),
    CHECK (action IN ('entry', 'exit'))
) WITHOUT ROWID;

CREATE TABLE outputs (
    ticker TEXT    NOT NULL,
    ts     INTEGER NOT NULL,
    kind   TEXT    NOT NULL,  -- a signal name or an algo name
    config TEXT    NOT NULL,  -- the config file version
    output TEXT    NOT NULL,  -- JSON
    PRIMARY KEY (ticker, ts, kind, config)
) WITHOUT ROWID;

CREATE TABLE configs (
    version    TEXT    NOT NULL PRIMARY KEY,  -- hash of the whole config file
    first_seen INTEGER NOT NULL,              -- epoch seconds, UTC
    content    TEXT    NOT NULL               -- the full config file, JSON
) WITHOUT ROWID;

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
- `bars` needs no quality flag: bad rows are rejected at ingest.
- `trades` has no algo column (a trade is the consolidated decision), no
  price (recomputable), and no size (a row is one unit). Per-algo
  attribution is in outputs.
- `outputs` grows fastest; `logs` grows steadily.
- `configs` maps every stored version hash back to the full config file.
- `logs` is append-only and the one table with a rowid and an extra index.
- No other indexes beyond the primary keys. Config stays in files; the
  configs table is a record of versions, not the source of truth.

## Diagram

```mermaid
flowchart TB
    BARS[bars service<br/>minute poll from the last bar<br/>+ nightly 30-day sweep]
    EVENTS[events service<br/>daily]

    BARS --> BT[(bars)]
    EVENTS --> ET[(events)]

    subgraph DB [one database — one writing service per table]
        BT
        ET
        TR[(trades)]
        OUT[(outputs)]
        CFG[(configs)]
        LG[(logs)]
    end

    subgraph ALGO [algo service]
        LOOP[loop<br/>owns the clock] -- each minute --> CORE[stateless core<br/>data → signals → algos<br/>all config-defined]
    end

    BT -. read only .-> CORE
    ET -. read only .-> CORE
    LOOP --> TR
    LOOP --> OUT
    LOOP --> CFG
    BARS & EVENTS & LOOP --> LG
    DB -. read only .-> DASH[dashboard<br/>read-only webserver]
```

## Decisions (2026-08-17)

1. No calendar table; the early-close days live in config.
2 No backtest concept: the loop applies the core to timestamps, in bulk
    or in real time. An output updates in place per version; old versions
    stay for reference.

## Open

1. The consolidation rule. Default: enter when any algo enters, exit when
   any algo exits and a position is open.
2. The prune policy for old output versions.
