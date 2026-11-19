"""Replay MT5 M5 exports with bid OHLC and a per-bar ask spread."""
import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from config import DATA_DIR, SETTINGS
from strategy import Bar, Strategy, drawdown_hit, new_trade, partial_volume, risk_volume

KNOWN_OFFSETS = {
    "XAUUSDm_M5_Exness.csv": 0.0,
    "XAUUSD.m_JustMarkets.csv": 2.0,
    "XAUUSD_M5_Headway.csv": 2.0,
    "XAUUSD_M5_202505090150_202610081640.csv": 2.0,
}


@dataclass
class Position:
    trade: object
    volume: float
    stop: float
    initial_volume: float
    partial_done: bool = False
    realized: float = 0.0


def load_bars(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        fields = ("<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>")
        for row in reader:
            timestamp = datetime.strptime(
                row["<DATE>"] + " " + row["<TIME>"], "%Y.%m.%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            digits = len(row["<OPEN>"].partition(".")[2])
            point = 10 ** -digits
            yield Bar(
                int(timestamp.timestamp()),
                *(float(row[key]) for key in fields),
                int(row["<SPREAD>"]) * point,
            ), point


def replay(path, utc_offset, starting_equity, contract_size):
    strategy = Strategy(utc_offset_hours=utc_offset)
    balance = peak = baseline = starting_equity
    max_dd_pct = 0.0
    day = position = signal = last = None
    locked = False
    outcomes = []
    lot_min = lot_step = 0.01

    def liquidate(price):
        nonlocal balance, peak, max_dd_pct, position
        trade = position.trade
        profit = trade.side * (price - trade.entry) * position.volume * contract_size
        balance += profit
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd
        outcomes.append((profit + position.realized) / (trade.risk * position.initial_volume * contract_size))
        position = None

    for bar, point in load_bars(path):
        if last and bar.time <= last.time:
            raise ValueError(f"{path.name}: duplicate or unordered bars at {bar.time}")
        new_day = (bar.time - int(round(utc_offset * 3600))) // 86400
        if position:
            trade = position.trade
            shift = bar.spread if trade.side == -1 else 0
            exit_open = bar.open + shift
            equity = balance + trade.side * (exit_open - trade.entry) * position.volume * contract_size
        else:
            equity = balance
        if new_day != day:
            baseline, day, locked = equity, new_day, False
        if drawdown_hit(equity, baseline):
            locked = True
            if position:
                liquidate(exit_open)
        if position is None and signal and signal.entry_time == bar.time and not locked:
            trade = new_trade(signal, bar.open, bar.spread, point)
            if trade:
                volume = risk_volume(balance, trade.risk * contract_size, lot_min, 100, lot_step)
                if volume:
                    position = Position(trade, volume, trade.initial_stop, volume)

        if position:
            trade = position.trade
            side = trade.side
            shift = bar.spread if side == -1 else 0
            exit_open = bar.open + shift

            # Pure M15 21 EMA close exit for runner (no tight 1-bar trailing stop)
            if position.partial_done and strategy.ema_time == bar.time:
                if strategy.ema_exit_triggered(side):
                    liquidate(exit_open)

        if position:
            trade = position.trade
            side = trade.side
            shift = bar.spread if side == -1 else 0
            exit_open = bar.open + shift
            adverse = (bar.low if side == 1 else bar.high) + shift
            favorable = (bar.high if side == 1 else bar.low) + shift
            stop = position.stop

            guard_equity = baseline * (1 - SETTINGS.max_daily_loss_percent / 100)
            guard = trade.entry + side * (guard_equity - balance) / (position.volume * contract_size)
            first_stop = max((stop, guard), key=lambda price: side * price)
            if side * (adverse - first_stop) <= 0:
                locked |= side * (guard - stop) >= 0
                liquidate(min((exit_open, first_stop), key=lambda price: side * price))
            elif side * (exit_open - trade.level(SETTINGS.target_r)) >= 0:
                liquidate(trade.level(SETTINGS.target_r))
            else:
                partial = trade.level(SETTINGS.partial_r)
                target = trade.level(SETTINGS.target_r)
                if not position.partial_done and side * (favorable - partial) >= 0:
                    volume = partial_volume(position.volume, lot_min, lot_step)
                    price = max((exit_open, partial), key=lambda value: side * value)
                    gain = side * (price - trade.entry) * volume * contract_size
                    balance += gain
                    position.realized += gain
                    position.volume = round(position.volume - volume, 10)
                    position.partial_done = True
                    position.stop = trade.breakeven_stop(stop, point)
                    if side * (adverse - position.stop) <= 0:
                        liquidate(position.stop)
                if position and side * (favorable - target) >= 0:
                    liquidate(target)

        locked |= drawdown_hit(balance, baseline) if position is None else False
        signal = strategy.push(bar)
        last = bar

    if position:
        liquidate(last.close + (last.spread if position.trade.side == -1 else 0))

    wins = sum(1 for r in outcomes if r > 0)
    gross_win = sum(r for r in outcomes if r > 0)
    gross_loss = abs(sum(r for r in outcomes if r < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    win_rate = (wins / len(outcomes) * 100) if outcomes else 0.0
    net_r = sum(outcomes)
    return len(outcomes), win_rate, net_r, mean(outcomes) if outcomes else 0.0, pf, max_dd_pct, balance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--utc-offset", type=float, default=None,
                        help="Override UTC offset for all feeds (defaults to known broker offsets)")
    parser.add_argument("--feed-offset", action="append", default=[], metavar="FILE=HOURS",
                        help="Override the timestamp offset for a particular CSV filename")
    parser.add_argument("--equity", type=float, default=10000)
    parser.add_argument("--contract-size", type=float, default=100)
    args = parser.parse_args()
    offsets = {name: float(hours) for name, hours in (item.rsplit("=", 1) for item in args.feed_offset)}
    feeds = sorted(args.data_dir.glob("*.csv"))
    if not feeds or args.equity <= 0 or args.contract_size <= 0:
        parser.error("provide a data directory with CSV feeds and positive equity and contract size")
    passed = 0
    for path in feeds:
        offset = offsets.get(path.name, args.utc_offset if args.utc_offset is not None else KNOWN_OFFSETS.get(path.name, 0.0))
        count, win_rate, net_r, expectancy, pf, max_dd, balance = replay(
            path, offset, args.equity, args.contract_size
        )
        qualifies = count >= 100 and expectancy > 0
        passed += qualifies
        expected = f"{expectancy:+.4f}R" if count else "n/a"
        print(
            f"{path.name} (UTC+{offset:g}): {count} trades | WR {win_rate:.1f}% | Net {net_r:+.2f}R "
            f"| Avg {expected} | PF {pf:.2f} | DD {max_dd:.2f}% | Eq {balance:.2f} | {'PASS' if qualifies else 'FAIL'}"
        )
    print(f"Validation: {'PASS' if len(feeds) == 4 and passed >= 3 else 'FAIL'}"
          f" ({passed}/{len(feeds)} qualifying feeds; require 3 of 4)")


if __name__ == "__main__":
    main()
