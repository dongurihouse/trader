# bars

`bars` is the one writer for the shared database's `bars` rows. It fetches
minute OHLCV data through Robinhood's official Trading MCP and upserts it into
`../data/trader.sqlite3`.

The service reads `../config/config.json`. It does not store a Robinhood
password. Its ignored `../data/robinhood_oauth.json` file contains the OAuth
refresh token with owner-only permissions.
Every client that uses this token holds a sibling session lock while connected.
This keeps the bar collector and live order worker from racing a rotating
token.

## Behavior

- Backfill from the configured fixed history start once for each ticker. A ticker added
  later gets the same backfill before its next poll or sweep. Failed work
  resumes after its last committed provider session.
- During the configured live window, poll once per minute from each ticker's
  last stored bar. Each poll catches up by at most seven calendar days, so a
  long outage cannot turn one poll into unbounded work.
- After the close, fetch the trailing 30 days for every ticker. This nightly
  sweep repairs gaps without a separate reconciliation path.
- Store every Robinhood bar and record its `interpolated` flag.
- Require a result block for every requested ticker before advancing work.
- Upsert by `(ticker, ts)`, so every repeated fetch is safe.
- Keep per-ticker backfill and scheduled-sweep progress in `bar_jobs`, not log
  text.
- Serve process health and current operation at
  `http://127.0.0.1:8789/health` for Dash.
- Accept named `POST /poll`, `POST /backfill`, and `POST /sweep` work on the
  loopback API. Operational commands submit to this one writer.
- Append run summaries and problems to the shared `logs` table. Do not write
  heartbeat rows.
- Keep no filesystem log; `make logs` reads the shared table.
- Let `launchd` restart the process after login or a crash.

The collector supports one data contract: minute bars with UTC epoch-second
timestamps. The shared schema is in `../config/schema.sql`.

## Configure

Edit `../config/config.json`. It contains the ticker list, early-close dates,
live polling window, fixed initial history start, 30-day sweep, and provider
settings. The default live window is 04:00 Eastern through four hours after
the regular or configured early close. `bars.api_port` sets the loopback health
API port. `bars.poll_catchup_days` bounds one live catch-up cycle.

## Operate

Run these commands from the repository root:

```sh
make auth                    # one-time Robinhood browser approval
make install                 # install and start the LaunchAgent
make status                  # process state plus stored coverage
make logs                    # show the latest database logs
make restart                 # reload after a config edit
make once                    # ask the running service for one live poll
make backfill                # ask it to force the configured history backfill
make sweep                   # ask it to run the trailing 30-day sweep
make uninstall               # stop it; keep the database
```

The three collection commands return after the service accepts the operation.
Use `make status`, `make logs`, or the health endpoint to monitor it. Do not
open the SQLite database or run collection writes from a second process.

## Query stored bars

CSV is the default output:

```sh
make query SYMBOL=SNDK ARGS='--start 2026-08-14 --end 2026-08-14'
```

Bare dates use Eastern calendar days. ISO-8601 timestamps must include their
timezone.

JSON is also available:

```sh
bars/.venv/bin/python bars/bars_service.py query SNDK --limit 5 --format json
```

The stored columns are `ticker`, `ts`, `open`, `high`, `low`, `close`,
`volume`, and `interpolated`.
