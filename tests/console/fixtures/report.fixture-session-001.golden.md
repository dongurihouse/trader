# Trader session: fixture-session-001

## Session summary

| Field | Value |
| --- | --- |
| mode | paper |
| config_sha256 | fixture-config-sha256 |
| package_version | 0.1.0 |
| symbols | SNDK, SNXX |
| bars_processed | 720 |
| real_trades | 1 |
| shadow_trades | 0 |
| final_equity | 100337.500 |

## Per-algo metrics

| Algo | Book | Status | n_real | n_shadow | wins | win_rate | mean_r | expectancy_r | profit_factor | max_drawdown_r | cum_r |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| mean-reversion | — | probe | — | — | — | — | — | — | — | — | — |
| breakout | real | emitting | 1 | 0 | 1 | 1.000 | 1.500 | 1.500 | — | 0.000 | 1.500 |

Shadow metrics simulate every probe-algo intent and every risk-rejected intent from emitting algos. The predecessor program measured that shadow leaderboards overstate the real edge: the apparent edge concentrated in candidates the rules refused (recorded examples: 29 shadow candidates to 2 emitted trades; 13 to 1). Treat shadow numbers as a look at what got refused, never as a forecast of promoted performance.

## Rejections by rule

### confidence_floor (1)

- Algo: mean-reversion; instrument: SNXX; detail: 0.42 is below the required 0.50

## Algo errors

- Algo: mean-reversion; error: fixture signal unavailable
