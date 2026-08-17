# bars

`bars` is one small, always-on Robinhood minute-bar collector. It reads the
ticker list from `config.json`, fetches OHLCV bars through Robinhood's official
Trading MCP, and upserts them into `data/bars.sqlite3`.

It does not store a Robinhood password. It connects directly to Robinhood's
MCP endpoint with the official MCP Python SDK and calls only
`get_equity_historicals`. A refresh token is stored in the ignored
`data/robinhood_oauth.json` file with owner-only permissions.

## Behavior

- On first start, it walks backward one weekday at a time.
- It drops Robinhood bars marked `interpolated`.
- It stops separately for each ticker after 10 consecutive weekdays with no
  real bars. This discovers the current provider edge instead of assuming a
  fixed date.
- It catches up missed days after later restarts.
- During market hours, it refreshes the current day every five minutes.
- Five minutes after the close, it performs one final refresh.
- SQLite upserts make every fetch safe to repeat and preserve vendor revisions.
- `launchd` keeps the process alive and starts it again after login or a crash.
- No model, prompt, Codex subprocess, or tool-search step is in the data path.
- Requests use small symbol batches because Robinhood can drop larger responses.

Robinhood's minute history is a sliding window. The collector can preserve bars
from this point forward, but it cannot recover a minute that Robinhood no
longer serves. Keep the service running if continuous minute history matters.

## Configure

Edit `config.json`. The required setting is the ticker list:

```json
"tickers": ["SNDK", "SPY", "QQQ"]
```

This first version deliberately supports one contract: regular-hours,
one-minute equity bars. Set `bounds` to `extended` if premarket bars are needed.

## Operate

```sh
make auth                    # one-time Robinhood browser approval
make install                 # install and start the LaunchAgent
make status                  # process state plus stored coverage
make logs                    # follow collector logs
make restart                 # reload after a config edit
make uninstall               # stop it; keep the database
```

If Robinhood revokes the connection, authorize it again and restart:

```sh
make auth
make restart
```

## Query stored bars

CSV is the default output:

```sh
make query SYMBOL=SNDK ARGS='--start 2026-08-14 --end 2026-08-14'
```

JSON is also available:

```sh
.venv/bin/python bars_service.py query SNDK --limit 5 --format json
```

The table key is `(symbol, interval, begins_at)`. Columns are `open`, `high`,
`low`, `close`, `volume`, `session`, and `fetched_at`.
