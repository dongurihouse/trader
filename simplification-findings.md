# Simplification findings

Status: review, 2026-08-19. Author: code audit.

## Summary

The codebase is about 19,600 tracked lines excluding the dependency lock. It is
well structured and the service isolation is clean. The first pass removed 711
net tracked lines. Accept each remaining cleanup only when its measured diff
keeps the overall footprint smaller without changing a supported contract.

Three facts frame every recommendation below:

1. **There is no test suite.** No `test` or `spec` file is tracked. Every change
   must be checked by replay against stored bars, not by a unit test. This raises
   the risk of any refactor and sets the priority order: delete dead code first,
   consolidate duplication second, restructure the engine last.
2. **One rule governs the database.** `AGENTS.md` forbids ad-hoc SQL. All schema
   and data changes go through the service functions and named commands.
3. **The broker path moves real money.** `broker.py`, `robinhood_order.py`, and
   the `broker_positions` write path in `storage.py` are correct and careful. Do
   not simplify them for footprint. They are out of scope for this work.

The largest confirmed win was dead CSS: 690 stylesheet lines were removed in
`fde80de`. The most valuable remaining win is architectural: two signals have
grown bespoke code inside the generic engine, and that breaks the design's
central promise.

## Priority table

| # | Finding | Type | Area | Est. lines | Risk |
|---|---------|------|------|-----------:|------|
| 1 | Dead CSS from a removed UI | completed | dash | -690 | verified |
| 2 | Write-only `shape_v1` output (`funnel`, `evidence`, price projections) | delete | algo | 90–110 | low* |
| 3 | Duplicated health-server `Service` scaffold | reuse | bars+algo | 60–80 | med |
| 4 | Signal-specific warmup/repair inside the generic engine | restructure | algo | 100+ | med |
| 5 | Six trading algos share one entry-gate skeleton | reuse | algo | 60–80 | med |
| 6 | Dead `/api/detail` endpoint and its server support | delete | dash | ~48 | none |
| 7 | Read-only SQLite connect copied in three modules | reuse | all | ~25 | low |
| 8 | Duplicated JS formatters across four files | reuse | dash | 60–85 | low |
| 9 | `_shape_forecast` and `_relative_momentum` server methods near-identical | reuse | dash | 40–55 | low |
| 10 | One-time schema migration code | delete | algo | ~60 | med** |
| 11 | Duplicated helpers between `broker.py` and `notify.py` | reuse | algo | ~12 | low |
| 12 | Reuse the shared `EASTERN` timezone | completed | all | included | verified |

\* Low risk only after you confirm no out-of-repo reader of the persisted output
JSON (see finding 2). \*\* Medium risk only until you confirm the live database
already has the migrated columns (see finding 10).

---

## First-principles findings

These restore the architecture's own rules. They carry the most value because
they stop the same drift from happening again.

### 1. The generic engine contains signal-specific code

`design.md` states the core promise: "A new signal or algo is a config entry,
not an engine change." The engine has drifted from this. Two signals,
`shape_v1` and `relative_momentum`, earned bespoke code inside the generic
service and storage layers.

- [algo_service.py:841](algo/algo_service.py:841) `_warm_primary_shape`
- [algo_service.py:858](algo/algo_service.py:858) `_warm_primary_relative_momentum`
- [algo_service.py:885](algo/algo_service.py:885) `_repair_relative_momentum_gaps`
- [storage.py:1048](algo/storage.py:1048) `_shape_warm_targets`
- [storage.py:1088](algo/storage.py:1088) `_relative_momentum_warm_targets`
- [storage.py:1127](algo/storage.py:1127) `_relative_momentum_has_score`
- [storage.py:546](algo/storage.py:546) `_relative_momentum_repair_candidates`
- [algo_service.py:56](algo/algo_service.py:56) imports two named signal
  functions straight into the engine.

The warmup functions serve a real need: they fill the primary ticker's current
session first, so the dashboard shows a fresh forecast before the deep-history
backfill catches up. The repair function is a one-time data fix for older
momentum rows that lack a continuous score.

Recommendation, in order:

1. Replace the two `_warm_primary_*` functions with one config-declared
   capability. A signal declares `priority: latest_session` in its config
   entry; the engine warms any such signal generically. This removes the two
   hardcoded paths and lets a future signal opt in without an engine change.
2. Retire `_repair_relative_momentum_gaps` and its module-level attempt set
   [algo_service.py:101](algo/algo_service.py:101) after it sweeps the stored
   history once. A permanent repair path for a past format change is not part of
   the steady state.

This is medium risk because it touches the live cycle. Validate by replay on the
primary ticker before and after.

### 2. Most of the shape signal output is never read

`shape_signal.py` is 771 lines and computes a large output dictionary per bar.
Inside the repository, only one field is read: `top_shapes` (each item's `shape`
and `probability`), consumed by [server.py:896](dash/server.py:896) and rendered
by [app.js:1625](dash/static/app.js:1625).

Every other field is write-only: `funnel`, `evidence` (and its `proximity`,
`shape_method`, `bandwidth`, `dropped_weight`, `quality_mean`, `effective_n`),
`reliability`, `remaining_minutes`, the price-projection quantiles,
`current_return`, and `shapes`. The funnel forecast block
([shape_signal.py:609](algo/shape_signal.py:609)) and its only helper
`_weighted_quantile` ([shape_signal.py:135](algo/shape_signal.py:135)) exist
solely to fill unread output.

Caveat: the output JSON persists to the `outputs` table, so a manual or
out-of-repo reader could exist. Confirm none exists, then reduce `shape_v1` to
the consumed fields. This trims about 90 to 110 lines and shrinks `shape_signal.py`
toward 600.

### 3. The two services duplicate the health-server scaffold

`bars` and `algo` each run a loopback health API with an identical shape: an
`operation_lock` plus status-dict state machine, a `ThreadingHTTPServer` on a
daemon thread, and a `_HealthHandler` with the same `do_GET`, `do_POST`, and
silent `log_message`.

- [bars_service.py:426](bars/bars_service.py:426) `Service` + `_HealthHandler`
- [algo_service.py:1410](algo/algo_service.py:1410) `Service` + `_HealthHandler`

`common/service.py` already owns `request_operation` and `write_json`. Extend it
with an `OperationState` helper and a `HealthServer` bootstrap. Each service then
keeps only its own cycle body. This removes about 60 to 80 lines and gives both
services one health contract.

---

## Reusability findings

### 5. The six trading algos share one entry-gate skeleton

Five of the six algos in `strategies.py` repeat the same steps: unpack inputs by
name, `require_float` each nullable input, return quiet if any is `None`, run an
exit device when a position is open, block a second entry with
`_algo_has_session_entry`, then check the same window and cutoff gate.

The one-entry guard repeats at
[strategies.py:278](algo/strategies.py:278), 334, 412, 550, and 651. The window
call repeats at [strategies.py:280](algo/strategies.py:280), 336, 414, 552, and
653. `_fixed_atr_exit` ([strategies.py:195](algo/strategies.py:195)) already
shares the bracket exit across three algos, which is the right pattern.

Extend that pattern: add one `_standard_entry_gate(context, window, cutoff,
rvol, min_rvol)` helper that returns whether entry is allowed. Each algo then
holds only its own entry and exit conditions. This removes about 60 to 80 lines
and makes each algo read as its actual strategy, not its scaffolding.

The input-unpack idiom `session_name, atr_name, price_name = context.inputs`
followed by `context.inputs[session_name]` is also repeated. `context.inputs` is
already an ordered map, so the values can be read directly.

### 7. Read-only SQLite connect is copied in three modules

The same read-only connection setup — `file:...?mode=ro`, `row_factory =
sqlite3.Row`, a busy timeout, and `query_only` — appears in three places:

- [storage.py:40](algo/storage.py:40) `_connect`
- [bar_store.py:64](bars/bar_store.py:64)
- [server.py:66](dash/server.py:66)

The busy timeout even differs (30000, 30000, 2500), which is an accident, not a
decision. Add one `connect(path, read_only=False)` to `common/`. All three call
it. This removes the copies and makes the timeout a single choice.

### 8 and 9. The dashboard repeats formatters and query methods

The frontend defines the same Pacific-time and signed-number formatters across
four files:

- `pacificDateTime` in [app.js:42](dash/static/app.js:42),
  [logs.js:8](dash/static/logs.js:8), [algos.js:3](dash/static/algos.js:3).
- `humanize` ([algos.js:29](dash/static/algos.js:29)) equals `formatShapeName`
  ([app.js:119](dash/static/app.js:119)); `algorithmDisplayName` is defined
  twice.
- Signed-number helpers `formatSigned`, `formatTradeReturn`, `signed`,
  `returnPoints`, `unitReturn`, `rate` all share one shape.

`common.js` already exists as the shared frontend module. Move the shared
formatters there. This removes 60 to 85 lines.

On the server, `_shape_forecast` ([server.py:861](dash/server.py:861)) and
`_relative_momentum` ([server.py:936](dash/server.py:936)) are near-identical:
find a signal by function name, read one guarded parameter, run the same
`bars`-join-`outputs` query, validate, emit snapshots. One shared snapshot
helper removes 40 to 55 lines.

### 11 and 12. Small shared helpers

- `_env_value` is byte-identical in [broker.py:28](algo/broker.py:28) and
  [notify.py:45](algo/notify.py:45). `_clean` ([broker.py:40](algo/broker.py:40))
  and `_field` ([notify.py:160](algo/notify.py:160)) differ only in a default.
  Extract shared text helpers into `common/`.
- `EASTERN` now comes from [config.py:15](common/config.py:15) across the bars,
  algo, and dashboard modules.

---

## Dead-code and ceremony findings

### 1 and 6. Removed dashboard UI

The confirmed dead CSS was removed in `fde80de`. The chart, algorithm, log, and
trade pages passed desktop and mobile visual checks. Live table, status, toast,
chart, ticker, and trade-dashboard rules remain.

The `/api/detail` endpoint is also dead. No client references it.

- [server.py:332](dash/server.py:332) `node_detail`
- [server.py:758](dash/server.py:758) `_node_definition` (only caller is
  `node_detail`)
- [server.py:1134](dash/server.py:1134) the `/api/detail` route

The endpoint still has no in-repository client, but removal requires an explicit
decision that it is not a supported API or future detail-view contract.

### 10. One-time schema migration code

`schema.sql` already declares the `direction`, `real_order`, and
`broker_order_id` columns, the `broker_positions` table, and the `outputs` table
without a `config` column. The migration code that adds those columns and drops
old tables runs only against a database created before those schema changes:

- [storage.py:123](algo/storage.py:123) `_remove_definition_history`
- [storage.py:136](algo/storage.py:136) `_migrate_outputs`
- [storage.py:174](algo/storage.py:174) the `ALTER TABLE trades` guards

Confirm the live database already has the migrated shape, then delete this code.
Medium risk: do not delete it until you confirm the migration has run, because
the database rule forbids checking the columns with ad-hoc SQL. Use a service
status read to confirm the columns exist first.

### Bars service ceremony

The bars audit found several single-use wrappers and repeated operation-name
checks:

- The `("poll", "backfill", "sweep")` allow-list is checked three times and
  dispatched a fourth. The re-checks in `Service.request_operation` and
  `_HealthHandler.do_POST` are redundant because every entry point re-validates.
- `_run_operation` ([bars_service.py:507](bars/bars_service.py:507)) is a
  one-caller dispatcher with an unreachable `else` branch.
- `fetch_range` ([bars_service.py:227](bars/bars_service.py:227)) is a single-use
  wrapper around `_fetch_range`.
- `_bar_row` re-validates a symbol its only caller already validated.

Together these remove about 30 to 40 lines.

### Bespoke memoization in the signals layer

`signals.py` keeps five module-level cache dictionaries, each with a hand-built
tuple key, and `clear_signal_caches` ([signals.py:59](algo/signals.py:59))
clears all five. The pattern "check key, compute, store, return" repeats in
`_complete_session_summary`, `_prior_volume_baseline`,
`_complete_relative_momentum_session`, `_relative_momentum_baseline`,
and `_signal_atr_session`.

A single `session_cache` decorator keyed by `(database, ticker, day, params)`
would remove the repeated bookkeeping. This is a medium-value cleanup; the keys
differ per signal, so measure the saving before committing. Do not change the
cache lifetime, only the plumbing.

---

## What not to touch

- **The broker and order path.** `broker.py`, `robinhood_order.py`, and the
  `broker_positions` writes in `storage.py` move real money and are correct.
- **The individual signal math.** `_signal_relative_momentum` is long because
  the strategy is real, not because the code is padded. Shrinking it risks the
  trading logic.
- **The parallel recalculation path.** The `ProcessPoolExecutor` machinery in
  [algo_service.py:1100](algo/algo_service.py:1100) is heavy but it is a real
  speed optimization for a full recalculation across tickers. Leave it until a
  measurement shows the serial path is fast enough.
- **The `events` scaffolding.** The deferred events service leaves two
  conditionals in the engine and an empty-table read in the dashboard. The cost
  is a few lines; removing it would only have to be rebuilt later.

---

## Suggested order of work

1. Decide whether `/api/detail` is a supported API before deleting it. The dead
   CSS portion is complete. (Findings 1 and 6.)
2. Add `common/` helpers for the read-only connect, the shared text helpers, and
   `EASTERN`; point all call sites at them. (Findings 7, 11, 12.)
3. Consolidate the frontend formatters into `common.js`. (Finding 8.)
4. Confirm no external reader of the shape output JSON, then reduce `shape_v1`
   to its consumed fields. (Finding 2.)
5. Extract the health-server scaffold into `common/service.py`. (Finding 3.)
6. Extract the standard entry gate and merge the dashboard snapshot methods.
   (Findings 5 and 9.)
7. Confirm the live database migration, then delete the migration code.
   (Finding 10.)
8. Generalize the signal warmup into a config capability and retire the momentum
   repair. (Finding 4.)

Validate each engine change (steps 4 through 8) by replay against stored bars,
because there is no automated test to catch a regression.
