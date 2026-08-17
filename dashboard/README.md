# Trader dashboard

This is a local, read-only market chart for the shared trading database. It reads
the database path and ticker list from `config/config.json`. It does not write to
SQLite and does not call any service.

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

## Chart

- Minute OHLCV chart for one day, five sessions, one month, or all stored data.
- Ticker switching from the configured ticker list.
- Current price, session change, open, high, low, and volume.
- Automatic refresh every 30 seconds.
