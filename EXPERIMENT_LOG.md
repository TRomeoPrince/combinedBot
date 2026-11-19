# Experiment Log


## XAUUSD M5 / M15 Quasimodo strategy

**Hypothesis.** A session-filtered M15 Quasimodo reversal with a fair value gap, an M5 fakeout sweep and candle rejection, and the specified partial exit and trailing rules yields positive expectancy on at least three of four feeds with at least 100 trades each.

**Operational definitions.** Use confirmed one-bar pivots, a four-swing sequence with a higher neckline low for shorts, and the shoulder's upper wick as the QML zone. The M15 displacement body must span at least 60% of its range and cross the neckline. A confirmed M5 pivot below the short zone must be swept on a bar before the first zone tap. Allow three M5 bars from sweep to tap and 36 M5 bars for the setup. Mirror prices for longs.

**Initial replay.** With displacement restricted to the third FVG candle, three feeds produced zero trades and Exness produced one trade at -1.00R. The final implementation also accepts displacement on the middle candle and waits for the third candle to close before confirming the gap.

**Final replay.** python backtest.py --data-dir ./data/XAUUSD_BARS/

| Feed | Trades | Expectancy | Ending equity |
| --- | ---: | ---: | ---: |
| JustMarkets | 1 | -1.0000R | 9,962.26 |
| XAUUSD_M5_202505090150_202610081640 | 0 | n/a | 10,000.00 |
| Headway | 2 | -1.0000R | 9,926.47 |
| Exness | 1 | -1.0000R | 9,954.17 |

The replay exited successfully. Zero feeds qualified. The sample is too small to estimate expectancy with confidence.

The exports lack timezone and execution metadata. This run treats timestamps as UTC, uses bid OHLC and the exported spread, and assumes 10,000 starting equity, a 100-unit contract and 0.01-lot steps. The replay excludes commissions, swaps and order slippage. Stop-first handling includes collisions with a breakeven stop newly set after a partial.

Controlled checks passed for mirrored M5 triggers, lot sizing, partial exit and breakeven, the EMA stop rule, stop-first handling and the persistent daily lock. The live equity baseline is the first observation on each UTC day and persists across restarts. MT5 connectivity and broker execution remain unverified.
