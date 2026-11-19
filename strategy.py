"""Broker-independent closed-bar signal, risk and trade management rules."""
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from config import BAR_SECONDS, SETTINGS, STRUCTURE_SECONDS


@dataclass(frozen=True)
class Bar:
    time: int  # UTC open timestamp
    open: float
    high: float
    low: float
    close: float
    spread: float = 0.0  # Price units

    def directional(self, direction):
        if direction == -1:
            return self
        return Bar(self.time, -self.open, -self.low, -self.high, -self.close)


@dataclass(frozen=True)
class Signal:
    side: int
    head: float
    entry_time: int


@dataclass
class Setup:
    head: float
    zone_low: float
    zone_high: float
    formed: int
    fakeout: float | None = None
    swept: int | None = None


@dataclass(frozen=True)
class Trade:
    side: int
    entry: float
    initial_stop: float
    entry_spread: float

    @property
    def risk(self):
        return self.side * (self.entry - self.initial_stop)

    def level(self, multiple):
        return self.entry + self.side * multiple * self.risk

    def managed_stop(self, current_stop, ema, tick_size):
        breakeven = self.entry + self.side * self.entry_spread
        candidates = [current_stop, breakeven]
        if ema is not None:
            candidates.append(ema)
        best = max(candidates, key=lambda price: self.side * price)
        return round_step(best, tick_size, up=self.side == -1)


def round_step(value, step, *, up=False):
    increment = Decimal(str(step))
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    units = Decimal(str(round(value, 10))) / increment
    return float(units.to_integral_value(rounding=rounding) * increment)


def partial_volume(volume, minimum, step, settings=SETTINGS):
    close = round_step(volume * settings.partial_fraction, step)
    return close if close >= minimum and round(volume - close, 10) >= minimum else 0.0


def risk_volume(equity, loss_per_lot, minimum, maximum, step, settings=SETTINGS):
    if equity <= 0 or loss_per_lot <= 0:
        return 0.0
    budget = equity * settings.risk_percent / 100
    volume = round_step(min(maximum, budget / loss_per_lot), step)
    return volume if partial_volume(volume, minimum, step, settings) else 0.0


def drawdown_hit(equity, baseline, settings=SETTINGS):
    return equity <= baseline * (1 - settings.max_daily_loss_percent / 100)


def new_trade(signal, bid, spread, tick_size, settings=SETTINGS):
    if not settings.accepts_spread(spread):
        return None
    entry = bid + spread if signal.side == 1 else bid
    stop = signal.head - signal.side * spread * settings.stop_spread_multiple
    stop = round_step(stop, tick_size, up=signal.side == -1)
    trade = Trade(signal.side, entry, stop, spread)
    executable_exit = bid if signal.side == 1 else bid + spread
    return trade if trade.risk > 0 and signal.side * (executable_exit - stop) > 0 else None


def pivot(bars, high):
    left, center, right = bars
    if center.time - left.time != right.time - center.time:
        return False
    if high:
        return center.high > max(left.high, right.high)
    return center.low < min(left.low, right.low)


def rejects_zone(previous, bar):
    body = abs(bar.open - bar.close)
    engulfing = (
        previous.close > previous.open and bar.close < bar.open
        and bar.open >= previous.close and bar.close <= previous.open
    )
    upper_wick = bar.high - max(bar.open, bar.close)
    lower_wick = min(bar.open, bar.close) - bar.low
    pin = bar.close < bar.open and upper_wick >= 2 * body and upper_wick > lower_wick
    return engulfing or pin


class Strategy:
    def __init__(self, settings=SETTINGS):
        self.settings = settings
        self.m5 = deque(maxlen=3)
        self.m15 = deque(maxlen=3)
        self.pivots = {side: deque(maxlen=4) for side in (-1, 1)}
        self.setups = {}
        self.ema = None
        self.ema_time = None
        self.ema_samples = 0

    def in_session(self, timestamp):
        return True

    def push(self, bar):
        """Consume one newly closed M5 bar and return at most one next-open signal."""
        if self.m5 and bar.time <= self.m5[-1].time:
            raise ValueError("M5 bars must be strictly increasing")
        if self.m5 and bar.time != self.m5[-1].time + BAR_SECONDS:
            self.m5.clear()
            self.m15.clear()
            self.setups.clear()
            for swings in self.pivots.values():
                swings.clear()
        self.m5.append(bar)
        entry_time = bar.time + BAR_SECONDS
        if len(self.m5) == 3 and entry_time % STRUCTURE_SECONDS == 0:
            first, _, last = self.m5
            self._structure(Bar(first.time, first.open, max(b.high for b in self.m5),
                                min(b.low for b in self.m5), last.close))
        signals = []
        if len(self.m5) == 3:
            for side, setup in list(self.setups.items()):
                if self._trigger(side, setup, entry_time):
                    signals.append(Signal(side, setup.head * -side, entry_time))
                    del self.setups[side]
        return signals[0] if len(signals) == 1 and self.in_session(entry_time) else None

    def _structure(self, bar):
        self.m15.append(bar)
        period = self.settings.ema_period
        self.ema_samples += 1
        if self.ema_samples <= period:
            self.ema = ((self.ema or 0) * (self.ema_samples - 1) + bar.close) / self.ema_samples
        else:
            self.ema += 2 / (period + 1) * (bar.close - self.ema)
        self.ema_time = bar.time + STRUCTURE_SECONDS
        if len(self.m15) < 3:
            return
        for side, swings in self.pivots.items():
            candles = [item.directional(side) for item in self.m15]
            left, center, current = candles
            high, low = pivot(candles, True), pivot(candles, False)
            if high != low:
                if swings and swings[-1][0] == high:
                    prior = swings[-1][1]
                    if (high and center.high > prior.high) or (low and center.low < prior.low):
                        swings[-1] = (high, center)
                else:
                    swings.append((high, center))
            if [kind for kind, _ in swings] != [False, True, False, True]:
                continue
            base, shoulder, neck, head = (candle for _, candle in swings)
            if not (neck.low > base.low and head.high > shoulder.high):
                continue
            if self.ema_time - head.time > self.settings.setup_m5_bars * BAR_SECONDS:
                continue
            impulse = any(
                candle.time > head.time and candle.close < neck.low and candle.high > neck.low
                and candle.open - candle.close
                >= self.settings.impulse_body_fraction * (candle.high - candle.low)
                for candle in (center, current)
            )
            impulse &= current.high < left.low and current.close < neck.low
            if impulse:
                self.setups[side] = Setup(head.high, max(shoulder.open, shoulder.close),
                                          shoulder.high, self.ema_time)

    def _trigger(self, side, setup, closed_at):
        candles = [bar.directional(side) for bar in self.m5]
        left, previous, current = candles
        if (current.high >= setup.head
                or closed_at - setup.formed > self.settings.setup_m5_bars * BAR_SECONDS):
            del self.setups[side]
            return False
        if current.time < setup.formed:
            return False

        if setup.swept is None:
            if setup.fakeout is not None and current.high > setup.fakeout:
                setup.swept = current.time
            elif left.time >= setup.formed and pivot(candles, True):
                setup.fakeout = previous.high

        if setup.swept is not None:
            if current.time - setup.swept > self.settings.sweep_to_tap_bars * BAR_SECONDS:
                del self.setups[side]
                return False
            if current.high >= setup.zone_low:
                if current.close < setup.zone_low:
                    if rejects_zone(previous, current):
                        return True
                if current.close > setup.zone_high:
                    del self.setups[side]
                return False
        elif current.high >= setup.zone_low:
            del self.setups[side]
        return False
