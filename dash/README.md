# Trader dashboard

This is a local, read-only market chart for the shared trading database. It
reads the database path and ticker list from `config/config.json`. It does not
write to SQLite. It calls the Bars and Algo loopback health APIs only for
service status.

## Install as a service

Install the dashboard as a macOS LaunchAgent:

```sh
make -C dash install
```

The service starts at login, restarts after a crash, binds to `0.0.0.0:8790`,
and does not depend on a terminal staying open. Manage it with:

```sh
make -C dash status
make -C dash restart
make -C dash uninstall
```

Open <http://127.0.0.1:8790> from this computer, or use the computer's network
address from another device.

## Run in a terminal

From the repository root:

```sh
python3 dash/server.py --host 0.0.0.0
```

Use another config file or port when needed:

```sh
python3 dash/server.py --config /absolute/path/to/config.json --host 0.0.0.0 --port 9000
```

The server uses only the Python standard library. It opens SQLite in read-only
mode with `PRAGMA query_only = ON`, serves the interface from `dash/static`,
refreshes the live view every 30 seconds, and reads Bars and Algo status from
the loopback ports configured by `bars.api_port` and `algo.api_port`. The Trade
view starts the existing Bars virtual-environment runtime for read-only
Robinhood account snapshots.

## Chart

- Opens on the latest trading date, then fills the date row from five-session
  history chunks with up to three requests in flight.
- Minute-level bars remain available for zooming and panning.
- Switchable line and candlestick chart styles.
- Paired `IN` and `OUT` markers for every stored trading algorithm, read from `trades`.
- Select an `IN` or `OUT` marker to open its algorithm summary.
- Per-algorithm holding-span connectors and realized returns calculated from exact bar-close prices.
- Hover a trade row, marker, or connector to highlight the same trade across the chart; select a connector to open its algorithm summary.
- Up/down arrows identify long and short positions inside each marker, with action details on hover.
- Hover inspection and click-locked bar focus update price, change, time, and shape probabilities.
- Time zoom with the mouse wheel or a two-finger pinch on touch screens.
- Horizontal wheel, trackpad, Shift+wheel, and drag panning across the loaded minute history.
- Ticker switching from the configured ticker list.
- Automatic refresh every 30 seconds.

## Algorithms

Open <http://127.0.0.1:8790/algos> for per-algorithm summaries calculated
from stored trade actions and exact bar-close prices. The page shows gross
unit return, win rate, profit factor, drawdown, holding time, per-ticker
results for every configured ticker, current marks, and all closed units. A
ticker without trades remains visible with zero or unavailable results. Use
the horizontal algorithm scorecards to compare summaries and switch the detail
view. Returns do not include position sizing, fees, or slippage, and trade rows
are grouped by stable internal algorithm ID. The dashboard uses the descriptive
labels in `algo_display_names` without changing stored IDs or recalculating
algorithm output. Select an instrument row to jump to and
highlight its trades; all closed units remain visible. Select a closed unit to
open the SNDK chart, its trading date, and all algorithm overlays.

## Logs

Open <http://127.0.0.1:8790/logs> to see service logs in Dash. The view reads
the shared SQLite `logs` table, refreshes every five seconds, and filters by
service, level, and row count. The Trader menu on every view shows live Bars
and Algo health from the loopback service endpoints.

## Trades

Open <http://127.0.0.1:8790/trades> for the combined strategy trade book. It
shows gross strategy results, active units, and closed units grouped by their
exit market date. Broker-routed entries and closes come from the durable local
broker-position ledger. A separate, manually refreshable section calls
Robinhood for the configured account's current value, buying power, cash,
equity positions, and recent agentic equity orders. Account numbers are masked
before data reaches the browser. The Option shadows section shows every fresh
live near-OTM comparison, including its contract, conservative ask-to-bid
return, one-contract dollar result, and quote failures.
