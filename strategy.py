"""Broker-independent closed-bar signal, risk and trade management rules."""
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from config import BAR_SECONDS, SETTINGS, STRUCTURE_SECONDS


@dataclass(frozen=True)
class Bar:
    time: int  # Timestamp in seconds
    open: float
    high: float
    low: float
    close: float
    spread: float = 0.0  # Price units

    def directional(self, direction):
        if direction == -1:
            return self
        return Bar(self.time, -self.open, -self.low, -self.high, -self.close, self.spread)


@dataclass(frozen=True)
class Signal:
    side: int
    head: float
    entry_time: int


@dataclass
class Setup:
    head_time: int
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

    def breakeven_stop(self, current_stop, tick_size):
        breakeven = self.entry + self.side * self.entry_spread
        best = max((current_stop, breakeven), key=lambda price: self.side * price)
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


def fractal_pivot(bars5, high):
    """Confirmed 2-bar fractal pivot (2 bars left, 2 bars right) on M15."""
    b0, b1, center, b3, b4 = bars5
    step = center.time - b1.time
    if step <= 0 or (b1.time - b0.time != step) or (b3.time - center.time != step) or (b4.time - b3.time != step):
        return False
    if high:
        return center.high > max(b0.high, b1.high, b3.high, b4.high)
    return center.low < min(b0.low, b1.low, b3.low, b4.low)


def rejects_zone(previous, bar):
    full_range = bar.high - bar.low
    if full_range <= 0:
        return False
    body = abs(bar.open - bar.close)
    # Classic bearish engulfing (closes below prior candle body/open) or strong breakdown close
    engulfing = (
        bar.close < bar.open
        and bar.close < min(previous.open, previous.close)
        and body >= 0.50 * full_range
    )
    upper_wick = bar.high - max(bar.open, bar.close)
    lower_wick = min(bar.open, bar.close) - bar.low
    close_position = (bar.close - bar.low) / full_range
    pin = (
        upper_wick >= 1.6 * max(body, full_range * 0.05)
        and upper_wick > lower_wick
        and close_position <= 0.40
    )
    return engulfing or pin


class Strategy:
    def __init__(self, settings=SETTINGS, utc_offset_hours=0.0):
        self.settings = settings
        self.utc_offset_seconds = int(round(utc_offset_hours * 3600))
        self.m5 = deque(maxlen=60)
        self.m15 = deque(maxlen=40)
        self.pivots = {side: deque(maxlen=4) for side in (-1, 1)}
        self.setups = {}
        self.ema = None
        self.ema_time = None
        self.ema_samples = 0
        self.last_m15 = None

    def in_session(self, timestamp):
        utc_ts = timestamp - self.utc_offset_seconds
        hour = (utc_ts // 3600) % 24
        return any(start <= hour < end for start, end in self.settings.sessions)

    def ema_exit_triggered(self, side):
        """True when the most recently closed M15 bar closed across the M15 21 EMA."""
        if self.ema is None or self.last_m15 is None:
            return False
        return side * (self.last_m15.close - self.ema) < 0

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
        if len(self.m5) >= 3 and entry_time % STRUCTURE_SECONDS == 0:
            last3 = list(self.m5)[-3:]
            if last3[-1].time - last3[0].time == 2 * BAR_SECONDS:
                self._structure(Bar(
                    last3[0].time, last3[0].open,
                    max(b.high for b in last3),
                    min(b.low for b in last3),
                    last3[-1].close, last3[-1].spread,
                ))
        signals = []
        if len(self.m5) >= 3:
            for side, setup in list(self.setups.items()):
                if self._trigger(side, setup, entry_time):
                    signals.append(Signal(side, setup.head * -side, entry_time))
                    del self.setups[side]
        return signals[0] if len(signals) == 1 and self.in_session(entry_time) else None

    def _structure(self, bar):
        self.m15.append(bar)
        self.last_m15 = bar
        period = self.settings.ema_period
        self.ema_samples += 1
        if self.ema_samples <= period:
            self.ema = ((self.ema or 0) * (self.ema_samples - 1) + bar.close) / self.ema_samples
        else:
            self.ema += 2 / (period + 1) * (bar.close - self.ema)
        self.ema_time = bar.time + STRUCTURE_SECONDS
        if len(self.m15) < 5:
            return

        recent_5 = list(self.m15)[-5:]
        for side, swings in self.pivots.items():
            candles5 = [item.directional(side) for item in recent_5]
            center = candles5[2]
            current = candles5[4]
            high, low = fractal_pivot(candles5, True), fractal_pivot(candles5, False)
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

            dir_m15 = [item.directional(side) for item in self.m15]
            post_head = [c for c in dir_m15 if c.time > head.time]
            if not post_head or current.close >= neck.low:
                continue

            has_bos_impulse = any(
                c.close < neck.low
                and (c.high - c.low) > 0
                and (c.open - c.close) >= self.settings.impulse_body_fraction * (c.high - c.low)
                for c in post_head
            )
            has_fvg = any(
                dir_m15[i - 1].time >= head.time
                and dir_m15[i].high < dir_m15[i - 2].low
                and (dir_m15[i - 1].open - dir_m15[i - 1].close)
                >= self.settings.impulse_body_fraction * max(dir_m15[i - 1].high - dir_m15[i - 1].low, 1e-9)
                for i in range(2, len(dir_m15))
            )
            if has_bos_impulse and has_fvg:
                existing = self.setups.get(side)
                if existing is None or existing.head_time != head.time:
                    zone_low = min(shoulder.open, shoulder.close) if shoulder.close > shoulder.open else max(shoulder.open, shoulder.close)
                    zone_low = min(zone_low, shoulder.high - 0.35 * (shoulder.high - shoulder.low))
                    setup = Setup(
                        head_time=head.time,
                        head=head.high,
                        zone_low=zone_low,
                        zone_high=shoulder.high,
                        formed=self.ema_time,
                    )
                    # Scan post-head M5 bars so fakeouts engineered before BOS completion are captured
                    m5_dir = [b.directional(side) for b in self.m5 if b.time > head.time + STRUCTURE_SECONDS]
                    for idx in range(2, len(m5_dir)):
                        tri = m5_dir[idx - 2: idx + 1]
                        if pivot(tri, True) and tri[1].high < setup.zone_low:
                            setup.fakeout = tri[1].high
                        elif setup.fakeout is not None and tri[2].high > setup.fakeout and tri[2].high < setup.zone_high:
                            setup.swept = tri[2].time
                    self.setups[side] = setup

    def _trigger(self, side, setup, closed_at):
        candles = [bar.directional(side) for bar in list(self.m5)[-3:]]
        _, previous, current = candles

        if (
            current.high >= setup.head
            or closed_at - setup.formed > self.settings.setup_m5_bars * BAR_SECONDS
            or current.close > setup.zone_high
        ):
            del self.setups[side]
            return False

        if current.time < setup.formed:
            return False

        if setup.swept is None:
            if setup.fakeout is not None and current.high > setup.fakeout:
                setup.swept = current.time
            elif pivot(candles, True) and previous.high < setup.zone_low:
                setup.fakeout = previous.high

        if setup.swept is not None:
            if current.time - setup.swept > self.settings.sweep_to_tap_bars * BAR_SECONDS:
                del self.setups[side]
                return False
            if current.high >= setup.zone_low and current.close <= setup.zone_high:
                if rejects_zone(previous, current):
                    return True
        return False
