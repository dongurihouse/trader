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
the loopback ports configured by `bars.api_port` and `algo.api_port`.

## Chart

- Opens on the latest one-day range; longer history is available from the range controls.
- One-day, five-session, one-month, three-month, 120-day, and all-history ranges.
- Minute-level bars remain available in every range for zooming and panning.
- Switchable line and candlestick chart styles.
- Automatic entry and exit markers for the configured trading algo, read from `trades`.
- Up/down marker direction for long and short actions, with action details on hover.
- Time zoom with the `+`, `−`, and Reset controls or the mouse wheel.
- Horizontal wheel, trackpad, Shift+wheel, and drag panning across the loaded minute history.
- Ticker switching from the configured ticker list.
- Current price, session change, open, high, low, and volume.
- Automatic refresh every 30 seconds.

## Logs

Open <http://127.0.0.1:8790/logs> to see service logs in Dash. The view reads
the shared SQLite `logs` table, refreshes every five seconds, and filters by
service, level, and row count. It also shows live Bars and Algo health from the
loopback service endpoints.
