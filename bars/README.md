# bars

`bars` is the one writer for the shared database's `bars` rows. It fetches
minute OHLCV data through Robinhood's official Trading MCP and upserts it into
`../data/trader.sqlite3`.

The service reads `../config/config.json`. It does not store a Robinhood
password. Its ignored `../data/robinhood_oauth.json` file contains the OAuth
refresh token with owner-only permissions.

## Behavior

- During the configured live window, poll once per minute from each ticker's
  last stored bar. A restart or short outage catches up automatically.
- After the close, fetch the trailing 30 days for every ticker. This nightly
  sweep repairs gaps without a separate reconciliation path.
- Drop Robinhood rows marked `interpolated`.
- Drop rows whose open or close lies outside the low-to-high range.
- Upsert by `(ticker, ts)`, so every repeated fetch is safe.
- Append heartbeats, run summaries, and problems to the shared `logs` table.
- Let `launchd` restart the process after login or a crash.

The collector supports one data contract: minute bars with UTC epoch-second
timestamps. The shared schema is in `../config/schema.sql`.

## Configure

Edit `../config/config.json`. It contains the ticker list, early-close dates, live
polling window, sweep length, and provider settings. The default live window is
04:00 Eastern through five minutes after the regular or configured early close.

## Operate

Run these commands from the repository root:

```sh
make auth                    # one-time Robinhood browser approval
make install                 # install and start the LaunchAgent
make status                  # process state plus stored coverage
make logs                    # follow collector logs
make restart                 # reload after a config edit
make once                    # run one live poll now
make sweep                   # run the trailing 30-day sweep now
make uninstall               # stop it; keep the database
```

To import the old `bar` table into the shared schema:

```sh
make migrate LEGACY=/absolute/path/to/bars.sqlite3
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

The stored columns are `ticker`, `ts`, `open`, `high`, `low`, `close`, and
`volume`.
