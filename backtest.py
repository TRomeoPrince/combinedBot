"""Replay MT5 M5 exports with bid OHLC and a per-bar ask spread."""
import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

from config import DATA_DIR, SETTINGS
from strategy import Bar, Strategy, drawdown_hit, new_trade, partial_volume, risk_volume


@dataclass
class Position:
    trade: object
    volume: float
    stop: float
    initial_volume: float
    partial_done: bool = False
    realized: float = 0.0


def load_bars(path, utc_offset):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        fields = ("<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>")
        zone = timezone(timedelta(hours=utc_offset))
        for row in reader:
            timestamp = datetime.strptime(
                row["<DATE>"] + " " + row["<TIME>"], "%Y.%m.%d %H:%M:%S"
            ).replace(tzinfo=zone)
            digits = len(row["<OPEN>"].partition(".")[2])
            point = 10 ** -digits
            yield Bar(int(timestamp.timestamp()), *(float(row[key]) for key in fields),
                      int(row["<SPREAD>"]) * point), point


def replay(path, utc_offset, starting_equity, contract_size):
    strategy = Strategy()
    balance = baseline = starting_equity
    day = position = signal = last = None
    locked = False
    outcomes = []
    lot_min = lot_step = 0.01

    def liquidate(price):
        nonlocal balance, position
        trade = position.trade
        profit = trade.side * (price - trade.entry) * position.volume * contract_size
        balance += profit
        outcomes.append((profit + position.realized) / (trade.risk * position.initial_volume * contract_size))
        position = None

    for bar, point in load_bars(path, utc_offset):
        if last and bar.time <= last.time:
            raise ValueError(f"{path.name}: duplicate or unordered bars at {bar.time}")
        new_day = bar.time // 86400
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
            adverse = (bar.low if side == 1 else bar.high) + shift
            favorable = (bar.high if side == 1 else bar.low) + shift
            stop = position.stop
            if position.partial_done and strategy.ema_time == bar.time:
                proposed = trade.managed_stop(stop, strategy.ema, point)
                if side * (exit_open - proposed) > 0:
                    stop = position.stop = proposed
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
                    position.stop = trade.managed_stop(stop, None, point)
                    # Intrabar ordering is unknown. Favor the new stop if both levels were reached.
                    if side * (adverse - position.stop) <= 0:
                        liquidate(position.stop)
                if position and side * (favorable - target) >= 0:
                    liquidate(target)
        locked |= drawdown_hit(balance, baseline) if position is None else False
        signal = strategy.push(bar)
        last = bar
    if position:
        liquidate(last.close + (last.spread if position.trade.side == -1 else 0))
    return len(outcomes), mean(outcomes) if outcomes else 0.0, balance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--utc-offset", type=float, default=0,
                        help="Hours east of UTC in the exported timestamps")
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
        count, expectancy, balance = replay(
            path, offsets.get(path.name, args.utc_offset), args.equity, args.contract_size
        )
        qualifies = count >= 100 and expectancy > 0
        passed += qualifies
        expected = f"{expectancy:+.4f}R" if count else "n/a"
        print(f"{path.name}: {count} trades | expectancy {expected} | equity {balance:.2f}"
              f" | {'PASS' if qualifies else 'FAIL'}")
    print(f"Validation: {'PASS' if len(feeds) == 4 and passed >= 3 else 'FAIL'}"
          f" ({passed}/{len(feeds)} qualifying feeds; require 3 of 4)")


if __name__ == "__main__":
    main()
