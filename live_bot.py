"""Run the shared strategy against a logged-in local MT5 terminal."""
import argparse
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from time import sleep, time

from config import BAR_SECONDS, SETTINGS
from mt5_connector import BrokerError, MT5Connector
from strategy import Strategy, Trade, drawdown_hit, partial_volume, round_step


LOG = logging.getLogger(__name__)


@contextmanager
def exclusive_run(path):
    import msvcrt

    with path.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BrokerError(f"Another bot instance holds {path}") from error
        yield


class LiveState:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(self.data, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.path)

    def check_drawdown(self, utc_day, equity, settings):
        guard = self.data.get("guard")
        if not guard or guard["day"] != utc_day:
            guard = self.data["guard"] = {"day": utc_day, "baseline": equity, "locked": False}
            self.save()
            LOG.info("UTC daily equity reference %.2f", equity)
        if not guard["locked"] and drawdown_hit(equity, guard["baseline"], settings):
            guard["locked"] = True
            self.save()
            LOG.error("Daily equity loss limit reached; entries locked for this UTC day")
        return guard["locked"]


class LiveBot:
    def __init__(self, broker, state):
        self.broker = broker
        self.state = state
        self.settings = broker.settings
        self.strategy = Strategy(self.settings)
        self.current_open = None
        self.account_id = account_identity(broker.account())
        self.unconfirmed_close = None

    def update_bars(self):
        bars = self.broker.bars(2)
        if not bars or bars[-1].time == self.current_open:
            return None
        bars = self.broker.bars(self.settings.warmup_bars + 1)
        if len(bars) < self.settings.ema_period * 3 + 1:
            raise BrokerError("Insufficient M5 history to warm up the strategy")
        opening = bars[-1].time
        last_closed = self.strategy.m5[-1].time if self.strategy.m5 else None
        signal = None
        for bar in bars[:-1]:
            if last_closed is None or bar.time > last_closed:
                signal = self.strategy.push(bar)
        self.current_open = opening
        if last_closed is not None and signal and signal.entry_time == opening:
            return signal
        return None

    def remember_position(self, position):
        record = self.state.data.get("trade")
        if record and record["id"] == position.identifier:
            return record
        intent = self.state.data.get("intent")
        if not intent or intent["side"] != self.broker.side(position) or position.time < intent["time"]:
            raise BrokerError(f"Missing original risk state for MT5 position {position.ticket}")
        info = self.broker.info
        closing = partial_volume(position.volume, info.volume_min, info.volume_step, self.settings)
        record = dict(id=position.identifier, stop=intent["stop"], spread=intent["spread"],
                      remaining=round(position.volume - closing, 10), triggered=None)
        self.state.data["trade"] = record
        self.state.data.pop("intent", None)
        self.state.save()
        return record

    def close(self, position, tick, volume=None):
        # Give ambiguous or asynchronous responses time to reconcile against broker state.
        if self.unconfirmed_close:
            ticket, previous_volume, attempted = self.unconfirmed_close
            if ticket == position.ticket and position.volume == previous_volume and time() - attempted < 15:
                return
        orders = self.broker.required(self.broker.api.orders_get(symbol=self.broker.symbol), "active orders")
        if any(order.magic == self.settings.magic_number for order in orders):
            return
        self.unconfirmed_close = (position.ticket, position.volume, time())
        self.broker.close(position, tick, volume)

    def manage(self, position, tick):
        broker = self.broker
        record = self.remember_position(position)
        side = broker.side(position)
        trade = Trade(side, position.price_open, record["stop"], record["spread"])
        if trade.risk <= 0:
            self.close(position, tick)
            return
        quote = broker.exit_quote(side, tick)
        target = broker.target(trade)
        if side * (quote - target) >= 0:
            self.close(position, tick)
            return
        if record["triggered"] is None and side * (quote - trade.level(self.settings.partial_r)) >= 0:
            record["triggered"] = tick.time
            self.state.save()
        if record["triggered"] is not None:
            step = broker.info.volume_step
            to_close = round_step(position.volume - record["remaining"], step)
            if to_close >= broker.info.volume_min:
                self.close(position, tick, to_close)
                return
            ema = self.strategy.ema if (self.strategy.ema_time or 0) > record["triggered"] else None
            stop = trade.managed_stop(position.sl or trade.initial_stop, ema, broker.tick_size)
        else:
            stop = position.sl or trade.initial_stop
        if (stop != position.sl or target != position.tp) and broker.levels_valid(
            side, quote, stop, target, modify=True
        ):
            broker.modify(position, stop, target)

    def step(self):
        broker = self.broker
        account = broker.account()
        if account_identity(account) != self.account_id:
            raise RuntimeError("MT5 switched accounts; restart the bot on the intended account")
        now = time()
        locked = self.state.check_drawdown(int(now) // 86400, account.equity, self.settings)
        positions = broker.positions()
        owned = broker.owned(positions)
        if locked:
            broker.cancel_entries()
        tick = broker.tick()
        tick_at = tick.time_msc / 1000 if tick.time_msc else tick.time
        if not -1 <= time() - tick_at <= self.settings.entry_window_seconds:
            return
        if locked:
            for position in owned:
                self.close(position, tick)
            self.update_bars()
            return
        if owned:
            try:
                self.update_bars()
            finally:
                for position in owned:
                    self.manage(position, tick)
            return
        signal = self.update_bars()
        if not signal or not broker.can_enter(positions, account):
            return
        tick = broker.tick()
        tick_at = tick.time_msc / 1000 if tick.time_msc else tick.time
        if not (signal.entry_time <= tick_at and
                time() - signal.entry_time <= self.settings.entry_window_seconds):
            return
        previous = self.state.data.get("intent")
        if previous and previous["time"] >= signal.entry_time:
            return
        plan = broker.entry_plan(signal, tick, account)
        if plan:
            trade, volume = plan
            self.state.data["intent"] = dict(time=signal.entry_time, side=trade.side,
                                             stop=trade.initial_stop, spread=trade.entry_spread)
            self.state.save()
            broker.open(trade, volume)


def account_identity(account):
    return account.server, account.login


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD", help="Exact broker symbol including suffix")
    parser.add_argument("--terminal", help="Path to terminal64.exe")
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent / ".mt5_state")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with MT5Connector(args.symbol, args.terminal) as broker:
        if broker.info.chart_mode != 0:
            raise BrokerError("The strategy requires bid-based M5 bars")
        args.state_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(repr(account_identity(broker.account())).encode()).hexdigest()[:16]
        with exclusive_run(args.state_dir / f"{key}.lock"):
            bot = LiveBot(broker, LiveState(args.state_dir / f"{key}-{SETTINGS.magic_number}.json"))
            LOG.info("Started for %s on account %s", args.symbol, bot.account_id[1])
            while True:
                try:
                    bot.step()
                except BrokerError as error:
                    LOG.warning("%s", error)
                    sleep(1)
                sleep(SETTINGS.poll_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
