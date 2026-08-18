# Remaining algo migration results

Measured 2026-08-17 on SNDK regular-session bars through 2026-08-17. The DT
archive supplies fifteen complete sessions from 2026-06-11 through 2026-07-02:
five at two-minute cadence and ten at one-minute cadence. This makes the
fourteen-session ATR usable on 2026-07-06 without inventing one-minute bars.
The ten one-minute sessions also supply the opening-volume baseline; coarser
archive sessions are rejected from slot-by-slot relative-volume calculations.
Returns below subtract 0.10 percentage points per round trip and assume the
same 50% capital cap. They are additive account percentage points, not
compounded returns.

The discovery period ends 2026-07-31. The forward period is 2026-08-03 through
2026-08-17.

| algo | tuned change | discovery trades / return | forward trades / return | all trades / return | win rate | max drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `lateday_momentum` | literal minute-270 late start | 14 / +4.063 | 3 / +0.343 | 17 / +4.406 | 58.8% | -2.096 |
| `failed_gap_reversal` | target 2.0R, not 1.5R | 4 / +8.847 | 1 / +3.504 | 5 / +12.351 | 100.0% | 0.000 |
| `gap_play` | risk 0.20 ATR, not 0.25 ATR | 6 / +10.052 | 3 / -0.378 | 9 / +9.674 | 77.8% | -1.698 |
| `day_extreme_reversal` | literal 0.75-ATR range gate | 8 / +8.728 | 1 / +1.072 | 9 / +9.800 | 55.6% | -1.057 |

The same July 6 replay produced 21 `orb5` trades, +27.669%, a 66.7% win
rate, and -4.725% maximum drawdown. `sentiment_pullback` produced nine trades,
+2.435%, a 66.7% win rate, and -0.861% maximum drawdown.

These are paper candidates. Forty total trades remain a small sample, and the
five perfect `failed_gap_reversal` outcomes have wide uncertainty. Gap
continuation remains negative in the forward period.

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
  170..200 on a 210-minute session.
- Every migrated algo returns quiet for non-SNDK tickers.

## Sentiment overlap

`sentiment_tranches` was not migrated because its counter-impulse path overlaps
`sentiment_pullback`. A one- to five-minute reversal confirmation was tested as
a small exhaustion proxy. The existing algo produced +2.031 in discovery and
+0.404 forward. Every confirmation variant reduced discovery return to +0.568
or less, so `sentiment_pullback` remains unchanged.
