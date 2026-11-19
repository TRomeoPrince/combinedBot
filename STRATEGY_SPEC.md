# STRATEGY_SPEC.md - Live Python MT5 Trading Bot (XAUUSD)
## 1. Architecture
- Build a live MetaTrader 5 Python trading bot (not just a backtester) where strategy.py is shared identically by live_bot.py and acktest.py.
- Files: config.py, strategy.py, mt5_connector.py, live_bot.py, acktest.py, equirements.txt.

## 2. Live Execution & Risk Management (mt5_connector.py & live_bot.py)
- Connect to local MT5 terminal using MetaTrader5 Python package; support configurable broker symbol suffix (XAUUSD, XAUUSDm, XAUUSD.m).
- Position sizing: Auto-calculate lot size from isk_percent of account equity and exact SL distance (respecting olume_min, olume_max, and olume_step).
- Prop-firm guardrails: Hard daily drawdown kill-switch (max_daily_loss_percent), max_spread_points filter, max_concurrent_trades = 1, and magic_number isolation.
- Trade management: Close 50% volume at +1.5R and move SL to breakeven (+ spread); trail remaining 50% on M15 21 EMA close (hard TP cap at 4.0R).

## 3. Strategy Rules (strategy.py)
- Execution TF: M5 (evaluate strictly on closed bars; enter at next bar open). Structure TF: M15 (resampled cleanly without lookahead).
- Sessions (UTC): London (07:00-11:00) and New York (13:00-17:00) only.
- Bearish Setup (mirror for Bullish): M15 Left Shoulder (QML high) -> Neckline (HL) -> Higher High (Head) -> impulsive M15 body close below Neckline with FVG -> Fakeout High formed and swept right before price taps the QML zone.
- Entry Trigger: Closed M5 bearish engulfing or bearish pin bar rejecting the QML zone (no blind limit orders).
- Stop-Loss: Above the Head (HH) + 1.5x current spread.
