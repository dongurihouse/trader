# algo

`algo` is the one writer for the shared database's `outputs`, `configs`, and
`trades` rows. It reads minute bars, evaluates every enabled signal and algo,
and stores deterministic JSON output under the config version.

The service has no market clock. Every cycle finds configured ticker/bar pairs
inside the evaluation window that do not yet have every enabled output under
the current version. It processes each ticker oldest first. A config change with
a new version therefore fills the same bounded window without a separate
backtest path.

Only a ticker's newest stored bar can create a trade. Older evaluations write
outputs only. An algo without `"trades": true` also writes outputs only.

The default config enables the two-sided `orb5` opening-range breakout and the
read-only `shape` path forecast.

The service exposes read-only process health at
`http://127.0.0.1:8791/health` for Dash. It stores completed work summaries,
warnings, and errors in `logs`. It does not write heartbeat or idle-cycle rows.

## Config

`signals` and `algos` are objects keyed by node name. Each node has this shape:

```json
{
  "inputs": ["bars"],
  "function": "sma",
  "params": {
    "field": "close",
    "period": 20,
    "include_interpolated": false
  }
}
```

`inputs` is a list. A signal can read `bars`, `bar_metadata`, `events`, and
signals. An algo can read signals and algos. Presence in the map means enabled.
Remove a node and bump the version to disable it. `function` defaults to the
node name, so only a node whose name differs from its code function needs that
field. Dependency cycles, unknown functions, missing nodes, and duplicate
signal/algo names are rejected before evaluation.

A signal with `"inputs": ["bar_metadata"]` reads the exact provider value for
its ticker and timestamp. Its `params.name` selects the indicator and the other
params select its parameter set. Single-output and multi-output indicators both
return JSON objects.

The service has these signal functions:

| function | params | output |
| --- | --- | --- |
| `sma` | `field`, `period`, `include_interpolated` | number or `null` |
| `session` | none | `{date, minute, to_close}` or `null` |
| `opening_range` | `minutes` | `{high, low, range}` or `null` |
| `rvol_open` | `cap_bars`, `baseline_sessions` | number or `null` |
| `last_close` | `include_interpolated` | number or `null` |
| `shape_v1` | `history_sessions`, `min_sessions`, `stride_minutes`, `shape_base_rate_w`, `age_halflife_days`, `support_k` | path funnel, eight shape probabilities, and evidence; otherwise `null` |

Supported fields are `open`, `high`, `low`, `close`, and `volume`. Every read
is at or before the evaluation timestamp. Opening-range and relative-volume
signals ignore interpolated bars. Relative volume uses the median volume for
each elapsed opening slot across the configured number of complete prior data
sessions.

`shape_v1` evaluates regular-session bars on the configured stride. It compares
only the completed prefix with prior sessions, centers historical continuations
to avoid directional bias, and classifies the completed path into eight fixed
shapes. It needs at least `min_sessions` prior sessions. It returns `null` for
all other bars, including extended-hours bars, while the service still stores
that output row. No algo reads this signal, so it cannot create a trade.

It has these algo functions:

| function | inputs | params |
| --- | --- | --- |
| `crossover` | `fast`, `slow` | none |
| `range_breakout` | `session`, `opening_range`, `rvol_open`, `last_close` | `direction`, `target_r`, `min_rvol`, `entry_cutoff_minutes`, `flat_minutes` |

Every algo output is `[is_entry, is_close_all, direction]`. Direction is `1`
for long, `-1` for short, and `0` when quiet. Both actions cannot be true. A
new code function is needed only when a strategy cannot be expressed as
another parameter set of these functions.

`orb5` forms the first five regular-session bars, requires elapsed relative
volume above `1.0`, and enters on the first close outside that range. The
opposite range edge is the stop and the target is `2R`. It enters at most once
per session, blocks new entries inside ten minutes to close, and closes any
open unit five minutes before the configured regular or early close.

Example:

```json
{
  "version": 2,
  "signals": {
    "fast": {
      "inputs": ["bars"],
      "function": "sma",
      "params": {
        "field": "close",
        "period": 5,
        "include_interpolated": false
      }
    },
    "slow": {
      "inputs": ["bars"],
      "function": "sma",
      "params": {
        "field": "close",
        "period": 20,
        "include_interpolated": false
      }
    }
  },
  "algos": {
    "sma_cross": {
      "function": "crossover",
      "inputs": ["fast", "slow"],
      "params": {},
      "trades": false
    }
  }
}
```

The rest of the shared config, including `database`, `tickers`, and `algo`,
stays in the same file. `algo.evaluation_days` bounds the cache fill and
`algo.poll_seconds` sets the service cadence. `algo.api_port` sets the
loopback health API port.

The service stores the full config the first time it sees a version. If the
file later changes without a version bump, it logs a warning and keeps using
the stored content for deterministic output. Bump `version` to apply a change.

## Operate

Run from the repository root:

```sh
make algo-validate
make algo-once
make algo-status
make algo-install
make algo-restart
make algo-logs
make algo-uninstall
```

Run one read-only core call without writing output, trades, configs, or logs:

```sh
python3 algo/algo_service.py core SNDK 2026-08-17T13:30:00Z
```

Restrict that call to one configured algo with `--algo NAME`. The read-only
command returns every enabled signal and only the requested algo outputs.
