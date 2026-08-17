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

No strategy is enabled in the default config. Add definitions and bump
`version` before installing the service.

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

`inputs` is a list. A signal can read `bars`, `events`, and signals. An algo can
read signals and algos. Presence in the map means enabled. Remove a node and
bump the version to disable it. `function` defaults to the node name, so only a
node whose name differs from its code function needs that field. Dependency
cycles, unknown functions, missing nodes, and duplicate signal/algo names are
rejected before evaluation.

The first service slice has one signal function:

| function | params | output |
| --- | --- | --- |
| `sma` | `field`, `period`, `include_interpolated` | number or `null` |

Supported fields are `open`, `high`, `low`, `close`, and `volume`. Every read
is at or before the evaluation timestamp.

It has one algo function:

| function | inputs | params |
| --- | --- | --- |
| `crossover` | `fast`, `slow` | none |

Its output is `[is_entry, is_close_all]`. Both values cannot be true. Add a
new code function only when a strategy cannot be expressed as another
parameter set of these functions.

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
`algo.poll_seconds` sets the service cadence.

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
