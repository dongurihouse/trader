# bars

`bars` is the one writer for the shared database's `bars` rows. It fetches
minute OHLCV data through Robinhood's official Trading MCP and upserts it into
`../data/trader.sqlite3`.

The service reads `../config/config.json`. It does not store a Robinhood
password. Its ignored `../data/robinhood_oauth.json` file contains the OAuth
refresh token with owner-only permissions.

## Behavior

- Backfill the configured 120-day window once for each ticker. A ticker added
  later gets the same backfill before its next poll or sweep.
- During the configured live window, poll once per minute from each ticker's
  last stored bar. A restart or short outage catches up automatically.
- After the close, fetch the trailing 30 days for every ticker. This nightly
  sweep repairs gaps without a separate reconciliation path.
- Store every Robinhood bar and record its `interpolated` flag.
- Upsert by `(ticker, ts)`, so every repeated fetch is safe.
- Serve process health at `http://127.0.0.1:8789/health` for Dash.
- Append run summaries and problems to the shared `logs` table. Do not write
  heartbeat rows.
- Keep no filesystem log; `make logs` reads the shared table.
- Let `launchd` restart the process after login or a crash.

The collector supports one data contract: minute bars with UTC epoch-second
timestamps. The shared schema is in `../config/schema.sql`.

## Configure

Edit `../config/config.json`. It contains the ticker list, early-close dates,
live polling window, 120-day initial backfill, 30-day sweep, and provider
settings. The default live window is 04:00 Eastern through five minutes after
the regular or configured early close. `bars.api_port` sets the loopback health
API port.

## Operate

Run these commands from the repository root:

```sh
make auth                    # one-time Robinhood browser approval
make install                 # install and start the LaunchAgent
make status                  # process state plus stored coverage
make logs                    # show the latest database logs
make restart                 # reload after a config edit
make once                    # run one live poll now
make backfill                # force the 120-day backfill for every ticker
make sweep                   # run the trailing 30-day sweep now
make uninstall               # stop it; keep the database
```

## Query stored bars

CSV is the default output:

```sh
make query SYMBOL=SNDK ARGS='--start 2026-08-14 --end 2026-08-14'
```

JSON is also available:

```sh
bars/.venv/bin/python bars/bars_service.py query SNDK --limit 5 --format json
```

The stored columns are `ticker`, `ts`, `open`, `high`, `low`, `close`,
`volume`, and `interpolated`.
