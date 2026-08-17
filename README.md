# bars

`bars` is one small, always-on Robinhood minute-bar collector. It reads the
ticker list from `config.json`, fetches OHLCV bars through Robinhood's official
Trading MCP, and upserts them into `data/bars.sqlite3`.

It does not store a Robinhood password or token. The fetch subprocess reuses
the Codex CLI OAuth login and can reach only the read-only
`get_equity_historicals` tool. Order tools are not available to it.

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
make install                 # install and start the LaunchAgent
make status                  # process state plus stored coverage
make logs                    # follow collector logs
make restart                 # reload after a config edit
make uninstall               # stop it; keep the database
```

If Robinhood OAuth expires, run this once and then restart the service:

```sh
codex mcp login robinhood-trading
make restart
```

## Query stored bars

CSV is the default output:

```sh
make query SYMBOL=SNDK ARGS='--start 2026-08-14 --end 2026-08-14'
```

JSON is also available:

```sh
/usr/bin/python3 bars_service.py query SNDK --limit 5 --format json
```

The table key is `(symbol, interval, begins_at)`. Columns are `open`, `high`,
`low`, `close`, `volume`, `session`, and `fetched_at`.
