# Remaining algo migration results

Measured 2026-08-17 on SNDK regular-session minute bars from 2026-07-02
through 2026-08-17. The fourteen-session ATR first becomes usable on
2026-07-27. Returns below subtract 0.10 percentage points per round trip and
assume the same 50% capital cap. They are additive account percentage
points, not compounded returns.

The discovery period ends 2026-08-07. The forward period is 2026-08-10 through
2026-08-17.

| algo | tuned change | discovery trades / return | forward trades / return | all trades / return | win rate | max drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `lateday_momentum` | start at minute 315, not 270 | 3 / +1.204 | 3 / -0.896 | 6 / +0.308 | 33.3% | -0.896 |
| `failed_gap_reversal` | target 2.0R, not 1.5R | 2 / +5.404 | 0 / 0.000 | 2 / +5.404 | 100.0% | 0.000 |
| `gap_play` | risk 0.15 ATR, not 0.25 ATR | 1 / +0.732 | 3 / -0.219 | 4 / +0.513 | 50.0% | -2.117 |
| `day_extreme_reversal` | require a 1.0 ATR range, not 0.75 | 1 / +0.588 | 0 / 0.000 | 1 / +0.588 | 100.0% | 0.000 |

These are paper candidates. Thirteen total trades cannot confirm an edge.
`failed_gap_reversal` supplies 79% of the summed return from only two trades.
The late-day and gap-continuation forward results remain negative.

## Port checks

- The literal, untuned predicates matched the available DT decision archive at
  four `lateday_momentum` signal times, three `gap_play` signal times, and one
  `day_extreme_reversal` signal time. The active archive has no comparable
  `failed_gap_reversal` entry.
- Replacing every bar after a chosen evaluation minute with an extreme future
  price did not change any new signal or algo output at that minute.
- The fifteen-minute opening-range duration and `gap_play.minute_min` are tied
  by config validation.
- The early-close map converts the regular late window from 270..380 to
  170..200 on a 210-minute session. The tuned 315 start maps to minute 185.
- Every migrated algo returns quiet for non-SNDK tickers.

## Sentiment overlap

`sentiment_tranches` was not migrated because its counter-impulse path overlaps
`sentiment_pullback`. A one- to five-minute reversal confirmation was tested as
a small exhaustion proxy. The existing algo produced +2.432 in discovery and
+0.404 forward. Every confirmation variant reduced discovery return to +0.098
or less, so `sentiment_pullback` remains unchanged.
