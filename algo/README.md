# algo

`algo` is the one writer for the shared database's `outputs`, `signals`,
`algos`, and `trades` rows. It reads minute bars, evaluates
every enabled signal and algo, and stores one unversioned live output per node.

The service has no market clock. Every cycle takes at most 2,000 configured
ticker/bar pairs inside the evaluation window that do not have live outputs.
It shares historical batches across the configured tickers and keeps each
ticker oldest first. Batches with at least 200 pairs across multiple tickers use
one worker process per ticker. Worker calculations run together, while database
writes remain serialized. Small live cycles stay in the service process. All
outputs for a pair are committed together, so one configured output is the
completion marker. A definition change clears the live output cache and fills
the same bounded window without a separate backtest path.

Every entry or exit produced by a configured algo writes a trade, whether the
evaluated bar is historical or new.
Every enabled algo runs for every configured ticker. There is no per-algo ticker
list or ticker gate.

The long-running service sends a macOS Messages alert after it commits a new
entry or exit on the ticker's latest regular-session bar. Historical work,
config backfills, and `make algo-once` never send messages. The trade row is the
at-most-once alert ledger, so a restart cannot resend it. Alert delivery runs in
a background thread and cannot stop evaluation. Set `TRADER_SMS_TO` in the
environment or the repository `.env`; when it is absent, Trader reuses the
existing `DT_SMS_TO` setting from the sibling `dt` checkout.

The same live-only gate submits a Robinhood equity order only when
`broker.enabled` is true and the signal ticker has an `execution_tickers`
mapping. A long entry buys that mapping's `long` symbol; a short entry buys its
`short` symbol; the matching exit sells it. Tickers without both configured
symbols remain paper-and-alert only. Orders use market, regular-hours,
good-for-day terms and a deterministic idempotency key. The account comes from
the environment variable named by `broker.account_env`. The initial config maps
SNDK to SNXX/SNDQ and keeps quantity at zero.

The default config enables `orb5`, `sentiment_pullback`, and four migrations
from the DT roster. The `shape` path forecast remains read-only.

The service exposes process health and the current operation at
`http://127.0.0.1:8791/health` for Dash. Its loopback API accepts
`POST /cycle` and `POST /recalculate`; the named commands below call those
endpoints. A non-empty serial cycle logs progress every 1,000 pairs. Parallel
historical batches log each ticker's completion to the service output. Every
non-empty cycle stores a batch summary. The service stores warnings and errors
in `logs`. It does not write heartbeat or idle-cycle rows.

## Config

`signals` and `algos` are objects keyed by node name. Each node has this shape:

```json
{
  "inputs": ["bars"],
  "function": "sma",
  "params": {
    "field": "close",
    "period": 20,
    "include_interpolated": false
  }
}
```

`inputs` is a list. A signal can read `bars`, `bar_metadata`, `events`, and
signals. An algo can read signals and algos. Presence in the map means enabled.
Remove a node to disable it. `function` defaults to the
node name, so only a node whose name differs from its code function needs that
field. Dependency cycles, unknown functions, missing nodes, and duplicate
signal/algo names are rejected before evaluation.

A signal with `"inputs": ["bar_metadata"]` reads the exact provider value for
its ticker and timestamp. Its `params.name` selects the indicator and the other
params select its parameter set. Single-output and multi-output indicators both
return JSON objects.

The service has these signal functions:

| function | params | output |
| --- | --- | --- |
| `sma` | `field`, `period`, `include_interpolated` | number or `null` |
| `session` | none | `{date, minute, to_close, total, ts, open_ts}` or `null` |
| `atr_session` | `sessions` | mean regular-session true range as a fraction of prior close, or `null` |
| `prior_session` | none | prior close/high/low, opening gap, and live price versus the prior range |
| `first30_ret` | `bars` | return from the prior close through the configured opening bars, or `null` |
| `session_extremes` | none | running high/low, fresh-extreme flags, and range in session ATRs |
| `opening_range` | `minutes` | `{high, low, range}` or `null` |
| `rvol_open` | `cap_bars`, `baseline_sessions` | number or `null` |
| `relative_momentum` | `window_minutes`, `baseline_sessions`, `min_sessions`, `time_tolerance_minutes`, `min_momentum_pct`, `strong_percentile` | direction, current move and duration, relative percentiles, and strength |
| `last_close` | `include_interpolated` | number or `null` |
| `opening_sentiment` | `market_tickers`, `minutes`, `min_market_move_pct`, `require_ticker_agreement` | fixed opening direction, current-ticker agreement, and live pattern validity |
| `pullback` | `early_minutes`, `early_window_minutes`, `late_window_minutes`, `late_market_strength_ratio`, `max_threshold_ratio`, `max_ticker_flip_ratio`, `entry_cutoff_minutes`, `baseline_sessions`, `min_baseline_sessions`, `percentile`, `min_extreme_distance_pct` | current countertrend move, prior-session threshold, and entry trigger |
| `shape_v1` | `history_sessions`, `min_sessions`, `stride_minutes`, `shape_base_rate_w`, `age_halflife_days`, `support_k` | path funnel, eight shape probabilities, and evidence; otherwise `null` |

Supported fields are `open`, `high`, `low`, `close`, and `volume`. Every read
is at or before the evaluation timestamp. Opening-range and relative-volume
signals ignore interpolated bars. Relative volume uses the median volume for
each elapsed opening slot across the configured number of complete prior data
sessions.

`relative_momentum` measures the signed return over its trailing window. During
the opening minutes, the trailing window grows from the available session data
until it reaches the configured length. Its
direction stays active while consecutive rolling returns keep the same sign
and exceed `min_momentum_pct`; this supplies the causal trend duration. At each
regular-session minute, it compares absolute return and duration with one
median observation from the same minute plus or minus
`time_tolerance_minutes` in each complete prior session. The overall strength
is the larger of the magnitude and duration percentiles, so an unusually large
move or an unusually persistent move can qualify. The signal uses no current-
session future bars and returns `null` outside regular hours or without enough
complete history.

The signal publishes a continuous `persistence_score` from the first regular-
session minute. Before the full alignment horizons are available, each horizon
is clamped to the available session length and duplicate horizons collapse.
After 30 minutes, the configured 5-, 20-, and 30-minute horizons are all active.
The score combines 80% joint magnitude/duration strength with 20% directional
agreement across those horizons. Neutral momentum publishes a zero score
instead of a gap. This score measures evidence that the current direction will
stay active; it does not claim that the future direction-adjusted return will
be positive. `persistent` uses the same `strong_percentile` threshold as
`strong`.

`atr_session` also accepts complete, uniform two- or five-minute archive
sessions. It uses only the session high, low, and close, so lower-frequency
source bars can warm the daily ATR without inventing one-minute prices.

Before the regular open, `shape_v1` publishes a prior-session distribution on
every stored bar. It has no price funnel because the session opening price does
not exist yet. During regular hours it evaluates on the configured stride,
compares only the completed prefix with prior sessions, centers historical
continuations to avoid directional bias, and classifies the completed path into
eight fixed shapes. It needs at least `min_sessions` prior sessions and returns
`null` after the close. No algo reads this signal, so it cannot create a trade.

It has these algo functions:

| function | inputs | params |
| --- | --- | --- |
| `crossover` | `fast`, `slow` | none |
| `range_breakout` | `session`, `opening_range`, `rvol_open`, `last_close` | `direction`, `target_r`, `min_rvol`, `entry_cutoff_minutes`, `flat_minutes` |
| `sentiment_pullback` | `session`, `opening_sentiment`, `pullback`, `last_close` | `early_minutes`, `early_hold_minutes`, `late_hold_minutes`, `take_profit_pct`, `stop_loss_pct`, `giveback_pct`, `pattern_exit`, `flat_minutes`, `capital_fraction` |
| `momentum_continuation` | `session`, `first30_ret`, `atr_session`, `rvol_open`, `last_close` | `first30_min_pct`, `risk_atr_frac`, `target_r`, `min_rvol`, `minute_min`, `minute_max`, `entry_cutoff_minutes`, `flat_minutes` |
| `failed_gap` | `session`, `prior_session`, `atr_session`, `last_close` | `gap_min_pct`, `risk_atr_frac`, `target_r`, `minute_min`, `minute_max`, `entry_cutoff_minutes`, `flat_minutes` |
| `gap_continuation` | `session`, `prior_session`, `opening_range`, `atr_session`, `rvol_open`, `last_close` | `gap_min_pct`, `risk_atr_frac`, `target_r`, `min_rvol`, `minute_min`, `minute_max`, `entry_cutoff_minutes`, `flat_minutes` |
| `extreme_fade` | `session`, `session_extremes`, `atr_session`, `rvol_open`, `last_close` | `min_range_atr`, `stop_atr_frac`, `target_r`, `confirmation_bars`, `min_rvol`, `minute_min`, `minute_max`, `entry_cutoff_minutes`, `flat_minutes` |

Every algo output is `[is_entry, is_close_all, direction]`. Direction is `1`
for long, `-1` for short, and `0` when quiet. Both actions cannot be true. A
new code function is needed only when a strategy cannot be expressed as
another parameter set of these functions.

`orb5` forms the first ten regular-session bars, requires elapsed relative
volume above `1.0`, and enters on the first close outside that range. The
opposite range edge is the stop and the target is `0.5R`. It enters at most once
per session, blocks new entries inside ten minutes to close, and closes any
open unit five minutes before the configured regular or early close.

`sentiment_pullback` fixes the opening direction after five minutes from the
median SPY and QQQ return and requires the ticker being evaluated to agree. It
then enters immediately on an unusually large move against that direction. The
move uses a five-minute window inside the first 30 minutes and a 30-minute
window later. Its threshold uses only complete prior sessions. A move more than
`max_threshold_ratio` times that threshold is treated as an overshoot instead
of a pullback. If the ticker crosses the session open against its fixed opening
direction, the opposite-side return must remain below
`max_ticker_flip_ratio` times the magnitude of its original five-minute move.
A late entry also requires the current market return to retain at least
`late_market_strength_ratio` times its opening magnitude. Only the first
statistically extreme move in each regime can enter, so a rejected setup cannot
become a later chase entry. It enters at most once per session, never adds to an
open unit, and exits on its configured close-based profit, close-based loss,
give-back from its best close since entry, early or late time limit,
market-pattern, or end-of-session rule. The best profit is floored at zero, so
the give-back rule also limits a trade that never becomes profitable. One unit
maps to at most 50% of capital for performance reporting; broker submission
uses its separately configured fixed quantity.

The four migrated algorithms run on every configured ticker and enter at most
once per regular session. `lateday_momentum` follows a large first-half-hour
move late in the session. `failed_gap_reversal` fades a gap after price returns
inside the prior range. `gap_play` follows a gap through the fifteen-minute
opening range. `day_extreme_reversal` arms at a qualifying fresh high or low,
then enters only when a later closed bar within `confirmation_bars` closes
beyond the extreme bar's opposite edge.
Their brackets use the entry price and the day-constant fourteen-session ATR.
All exits use the minute close. The algo context exposes regular bars through a
read-only accessor capped at the evaluation timestamp; the extreme fade uses
it to freeze the session extreme that existed when the position opened.

Example:

```json
{
  "signals": {
    "fast": {
      "inputs": ["bars"],
      "function": "sma",
      "params": {
        "field": "close",
        "period": 5,
        "include_interpolated": false
      }
    },
    "slow": {
      "inputs": ["bars"],
      "function": "sma",
      "params": {
        "field": "close",
        "period": 20,
        "include_interpolated": false
      }
    }
  },
  "algos": {
    "sma_cross": {
      "function": "crossover",
      "inputs": ["fast", "slow"],
      "params": {}
    }
  }
}
```

The rest of the shared config, including `database`, `tickers`, and `algo`,
stays in the same file. `algo.evaluation_days` bounds the cache fill and
`algo.poll_seconds` sets the service cadence. `algo.api_port` sets the
loopback health API port.

The service applies a valid config change immediately. The `algos` table holds
each current definition with the signal and algo definitions it depends on.
When an effective algorithm definition changes or is removed, the corresponding
live row changes or is deleted. Git and `config/config.json` are the definition
history; the database holds current state only.

Any signal or effective algorithm definition change starts the standard
recalculation path. In one transaction, that path deletes the derived live
outputs immediately before it inserts the first recalculated outputs. It also
deletes each affected algorithm's trades immediately before inserting that
algorithm's first recalculated trade rows. Either delete can affect zero rows.
The rest of the bounded recalculation is resumable. Git and
`config/config.json` remain the definition history; the database stores only
the current definition and results.

## Operate

Run from the repository root:

```sh
make algo-validate
make algo-once                 # ask the running service for one cycle
make algo-recalculate          # replace all calculated algo results
make algo-status
make algo-install
make algo-restart
make algo-logs
make algo-uninstall
```

Do not open the SQLite database or delete rows by hand. Use
`make algo-recalculate` for an explicit rebuild. Editing an effective signal or
algorithm definition invokes the same recalculation automatically.

Run one read-only core call without writing output, trades, definitions, or logs:

```sh
python3 algo/algo_service.py core SNDK 2026-08-17T13:30:00Z
```

Restrict that call to one configured algo with `--algo NAME`. The read-only
command returns every enabled signal and only the requested algo outputs.
