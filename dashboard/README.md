# Trader dashboard

This is a local, read-only web view of the shared trading database. It reads the
database path, tickers, current version, evaluation window, signals, and algos
from `config/config.json`. It does not write to SQLite and does not call any
service.

## Run

From the repository root:

```sh
python3 dashboard/server.py
```

Then open <http://127.0.0.1:8787>.

Use another config file or port when needed:

```sh
python3 dashboard/server.py --config /absolute/path/to/config.json --port 9000
```

The server uses only the Python standard library. It opens SQLite in read-only
mode with `PRAGMA query_only = ON`, serves the interface from `dashboard/static`,
and refreshes the live view every 30 seconds.

## Views

- Minute OHLCV chart for one day, five sessions, one month, or all stored data.
- Trade and event overlays on the chart.
- Current quotes and database totals.
- Signal and algo definitions from config, plus their latest cached outputs.
- Click-through output history and stored-version parameters.
- Per-algo performance across config versions, calculated from output pairs and
  matching bar closes.
- Latest service state, run summaries, warnings, and errors from `logs`.
