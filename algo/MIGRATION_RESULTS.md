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

## ORB5 win-rate refinement

Measured 2026-08-18 on the complete fixed-history ledger for SNDK, MU, WDC,
SPY, QQQ, and SPCX. CBRS, CRWV, SKHY, ASTS, and RKLB had no stored bars and are
not part of these results. The adopted variant lowers the target from `0.5R`
to `0.25R` and blocks entries at or after minute 30 (10:00 ET).

| params | discovery trades / win rate | forward trades / win rate | all trades / win rate | gross return | cost-aware return / max drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| previous (`0.5R`, no early ceiling) | 40 / 72.5% | 31 / 74.2% | 71 / 73.2% | +35.63% | +14.27% / -4.79% |
| adopted (`0.25R`, minute < 30) | 38 / 86.8% | 26 / 88.5% | 64 / 87.5% | +34.93% | +14.27% / -2.30% |

The cost-aware results subtract 0.10 percentage points per round trip and use
the same 50% capital cap described above. The lower target adds only one winner
over the measured `0.30R` alternative, so smaller targets were not extrapolated.
These remain same-data paper results and require new forward evidence.

## Sentiment overlap

`sentiment_tranches` was not migrated because its counter-impulse path overlaps
`sentiment_pullback`. A one- to five-minute reversal confirmation was tested as
a small exhaustion proxy. The existing algo produced +2.031 in discovery and
+0.404 forward. Every confirmation variant reduced discovery return to +0.568
or less, so no reversal confirmation is used.

## Sentiment pullback entry guard

Measured again on 2026-08-17, the entry guard rejects a counter-move above
1.25 times its historical threshold. In the late regime, it also requires the
current market move to retain its opening magnitude and allows only the first
qualifying setup. It does not delay an entry for reversal confirmation.

On the original three-market replay, this removed the July 7 overshoot and the
net-negative August 14 trade. The result changed from 9 trades, +2.435%, a
66.7% win rate, and -0.861% maximum drawdown to 7 trades, +3.322%, an 85.7%
win rate, and -0.103% maximum drawdown. Discovery return improved from +2.031%
to +2.892%; forward return improved from +0.404% to +0.430%.

On the current fixed-history replay, discovery was unchanged at 5 trades and
+2.020%. Forward changed from 3 trades and -0.110% to 2 trades and +0.430%.
The complete result changed from 8 trades, +1.911%, and -0.540% maximum
drawdown to 7 trades, +2.451%, and -0.179% maximum drawdown. Threshold caps
from 1.25 through 1.40 produced the same SNDK trades and returns. The sample is
still small, so these remain paper results rather than promotion evidence.

## Sentiment pullback ticker-flip guard

Measured on 2026-08-18 with the current six-ticker fixed-history ledger, the
entry guard now rejects a ticker that has crossed the session open against its
fixed opening direction when that opposite-side return reaches 0.5 times its
original five-minute move. The July 21 SPCX short had a 0.802 flip ratio and is
therefore rejected. The calculation uses only the current completed minute and
the fixed opening return.

The exact service replay changed the result from 25 trades, +5.7829% gross,
60.0% wins, and -2.4406% maximum drawdown to 22 trades, +6.9821% gross, 63.6%
wins, and -2.3383% maximum drawdown. Discovery return through July 31 changed
from +1.9181% to +3.1610%. Forward return from August 3 changed from +3.8648%
to +3.8211%. The guard removed the July 21 SPCX loss, the July 24 MU loss, and
an August 12 MU gain of +0.0437%. These results exclude fees, slippage, and
sizing, and the sample remains small.

## Sentiment pullback give-back exit

Measured on 2026-08-18 with the current six-ticker fixed-history ledger, the
exit now tracks the best close-based profit since entry, floored at zero. It
closes when the current close is 0.75 percentage points below that peak. The
existing close-based 1.0% take-profit and 1.5% stop remain unchanged.

Only the two flagged WDC shorts changed. The July 13 exit moved from minute 10
at -0.8778% to minute 7 at +0.1901%. The July 28 exit moved from minute 6 at
-1.5669% to minute 3 at -1.0836%. WDC's five-trade return improved from
-1.5404% to +0.0108%.

The exact service replay kept 22 trades. Win rate changed from 63.6% to 68.2%,
gross return changed from +6.9821% to +8.5333%, and maximum drawdown changed
from -2.3383% to -1.8550%. Discovery return through July 31 changed from
+3.1610% to +4.7122%; forward return from August 3 remained +3.8211%. The
intermediate 0.9 setting produced a 63.6% win rate and +8.1292% gross; tightening
to 0.75 changed only the July 13 exit, adding one win and +0.4041%. These
results exclude fees, slippage, and sizing, and the sample remains small.

## Lateday momentum win-rate refinement

Measured 2026-08-18 on the recorded close-window replay: 2026-07-27 through
2026-08-18, all six tickers, 31 trades. Each trade entered at minute 359 in
the direction of the first-half-hour return. Losses concentrated in trades
with a first-half-hour move below 2%; that band held four wins and five
losses. Trend-confirmation gates were also measured. A same-sign-as-open gate
and a minute-30-to-entry drift gate both lowered the win rate, because the
strongest wins come when the afternoon drifts against the morning move and
the close resumes it.

Two parameter changes follow. `first30_min_pct` rises from 1.0 to 2.0.
`target_r` falls from 1.5 to 0.5. A bracket simulation reproduced all 31
recorded exits before the sweep. Returns below are unweighted sums of
per-trade close-to-close moves. They omit the fee and capital adjustments of
the tables above.

| params | trades | win rate | return |
| --- | ---: | ---: | ---: |
| previous (1.0 / 1.5R) | 31 | 67.7% | +18.50 |
| threshold only (2.0 / 1.5R) | 22 | 77.3% | +18.90 |
| adopted (2.0 / 0.5R) | 22 | 81.8% | +11.34 |

The adopted set trades total return for win rate on request. Three of the
four remaining losses are flat exits smaller than 0.3 points. With the
0.10-point round-trip cost of the tables above, the win rate is 61.3% before
the change and 77.3% after it. The prior 14:45-entry window measured 25%
wins on SNDK. The move to the close window on 2026-08-17 removed that
failure mode; these parameters refine the close window.
