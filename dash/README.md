# Trader dashboard

This is a local, read-only market chart for the shared trading database. It reads
the database path and ticker list from `config/config.json`. It does not write to
SQLite and does not call any service.

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
and refreshes the live view every 30 seconds.

## Chart

- Opens with all stored minute data across past and current sessions.
- Optional one-day, five-session, and one-month ranges.
- Switchable line and candlestick chart styles.
- Time zoom with the `+`, `−`, and Reset controls or the mouse wheel.
- Horizontal drag-to-pan across the loaded minute history.
- Ticker switching from the configured ticker list.
- Current price, session change, open, high, low, and volume.
- Automatic refresh every 30 seconds.

## Logs

Open <http://127.0.0.1:8790/logs> to see service logs in Dash. The view reads
the shared SQLite `logs` table, refreshes every five seconds, and filters by
service, level, and row count.
