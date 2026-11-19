"""MetaTrader 5 terminal adapter for quotes, sizing and order execution."""
import logging

from config import SETTINGS
from strategy import Bar, new_trade, risk_volume, round_step


LOG = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    pass


class MT5Connector:
    def __init__(self, symbol, terminal_path=None, settings=SETTINGS):
        import MetaTrader5 as mt5

        self.api = mt5
        self.symbol = symbol
        self.path = terminal_path
        self.settings = settings

    def __enter__(self):
        api = self.api
        if not (api.initialize(self.path) if self.path else api.initialize()):
            raise BrokerError(f"MT5 initialization failed: {api.last_error()}")
        try:
            if not api.symbol_select(self.symbol, True):
                raise BrokerError(f"Cannot select {self.symbol}: {api.last_error()}")
            terminal = self.required(api.terminal_info(), "terminal information")
            if not terminal.trade_allowed or terminal.tradeapi_disabled:
                raise BrokerError("Enable algorithmic trading and external Python trading in MT5")
            if not self.account().trade_allowed or not self.account().trade_expert:
                raise BrokerError("The MT5 account does not allow automated trading")
            self.info = self.required(api.symbol_info(self.symbol), "symbol information")
            self.tick_size = self.info.trade_tick_size or self.info.point
            return self
        except Exception:
            api.shutdown()
            raise

    def __exit__(self, *_):
        self.api.shutdown()

    def required(self, value, operation):
        if value is None:
            raise BrokerError(f"MT5 {operation} failed: {self.api.last_error()}")
        return value

    def account(self):
        return self.required(self.api.account_info(), "account information")

    def tick(self):
        tick = self.required(self.api.symbol_info_tick(self.symbol), "quote")
        if not 0 < tick.bid < tick.ask:
            raise BrokerError(f"Invalid bid/ask for {self.symbol}")
        return tick

    def bars(self, count):
        rates = self.required(self.api.copy_rates_from_pos(self.symbol, self.api.TIMEFRAME_M5, 0, count),
                              "M5 history")
        return [Bar(int(row["time"]), float(row["open"]), float(row["high"]),
                    float(row["low"]), float(row["close"])) for row in rates]

    def positions(self):
        return self.required(self.api.positions_get(), "positions")

    def owned(self, positions):
        return [position for position in positions if position.symbol == self.symbol
                and position.magic == self.settings.magic_number]

    def can_enter(self, positions, account):
        if sum(p.magic == self.settings.magic_number for p in positions) >= self.settings.max_concurrent_trades:
            return False
        if account.margin_mode != self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            if any(p.symbol == self.symbol for p in positions):
                return False
        orders = self.required(self.api.orders_get(symbol=self.symbol), "active orders")
        return not any(o.magic == self.settings.magic_number
                       or account.margin_mode != self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING for o in orders)

    def order_type(self, side):
        return self.api.ORDER_TYPE_BUY if side == 1 else self.api.ORDER_TYPE_SELL

    def side(self, position):
        return 1 if position.type == self.api.POSITION_TYPE_BUY else -1

    def exit_quote(self, side, tick):
        return tick.bid if side == 1 else tick.ask

    def target(self, trade):
        return round_step(trade.level(self.settings.target_r), self.tick_size, up=trade.side == -1)

    def levels_valid(self, side, quote, stop, target, *, modify=False):
        points = self.info.trade_stops_level
        if modify:
            points = max(points, self.info.trade_freeze_level)
        distance = points * self.info.point
        return side * (quote - stop) > distance and side * (target - quote) > distance

    def entry_plan(self, signal, tick, account):
        trade = new_trade(signal, tick.bid, tick.ask - tick.bid, self.tick_size, self.settings)
        if trade is None:
            return None
        target = self.target(trade)
        if not self.levels_valid(trade.side, self.exit_quote(trade.side, tick), trade.initial_stop, target):
            return None
        loss = self.required(
            self.api.order_calc_profit(self.order_type(trade.side), self.symbol, 1.0,
                                       trade.entry, trade.initial_stop), "stop loss calculation"
        )
        volume = risk_volume(account.equity, -loss, self.info.volume_min, self.info.volume_max,
                             self.info.volume_step, self.settings)
        return (trade, volume) if volume else None

    def send_deal(self, side, volume, price, **fields):
        api = self.api
        info = self.info
        instant_or_request = info.trade_exemode in (
            api.SYMBOL_TRADE_EXECUTION_INSTANT, api.SYMBOL_TRADE_EXECUTION_REQUEST
        )
        policies = []
        if instant_or_request or info.filling_mode & 1:
            policies.append(api.ORDER_FILLING_FOK)
        if instant_or_request or info.filling_mode & 2:
            policies.append(api.ORDER_FILLING_IOC)
        if info.trade_exemode != api.SYMBOL_TRADE_EXECUTION_MARKET:
            policies.append(api.ORDER_FILLING_RETURN)
        if not policies:
            raise BrokerError(f"No supported market order filling policy for {self.symbol}")
        request = dict(action=api.TRADE_ACTION_DEAL, symbol=self.symbol, volume=volume,
                       type=self.order_type(side), deviation=self.settings.deviation_points,
                       magic=self.settings.magic_number, **fields)
        if info.trade_exemode != api.SYMBOL_TRADE_EXECUTION_MARKET:
            request["price"] = price
        for policy in policies:
            request["type_filling"] = policy
            check = self.required(api.order_check(request), "order check")
            if check.retcode == api.TRADE_RETCODE_INVALID_FILL:
                continue
            if check.retcode != 0:
                raise BrokerError(f"Order check rejected ({check.retcode}): {check.comment}")
            result = self.required(api.order_send(request), "order send")
            if result.retcode == api.TRADE_RETCODE_INVALID_FILL:
                continue
            if result.retcode in (api.TRADE_RETCODE_DONE, api.TRADE_RETCODE_DONE_PARTIAL,
                                   api.TRADE_RETCODE_PLACED):
                LOG.info("Deal response %s, order %s, deal %s", result.retcode, result.order, result.deal)
                return result
            raise BrokerError(f"Deal rejected ({result.retcode}): {result.comment}")
        raise BrokerError(f"Broker rejected all filling policies for {self.symbol}")

    def open(self, trade, volume):
        return self.send_deal(trade.side, volume, trade.entry, sl=trade.initial_stop,
                              tp=self.target(trade), comment="CombinedBot entry")

    def close(self, position, tick, volume=None):
        side = self.side(position)
        return self.send_deal(-side, position.volume if volume is None else volume,
                              self.exit_quote(side, tick), position=position.ticket,
                              comment="CombinedBot exit")

    def modify(self, position, stop, target):
        api = self.api
        result = self.required(api.order_send(dict(
            action=api.TRADE_ACTION_SLTP, position=position.ticket, symbol=self.symbol,
            sl=stop, tp=target, magic=self.settings.magic_number
        )), "stop/target modification")
        if result.retcode not in (api.TRADE_RETCODE_DONE, api.TRADE_RETCODE_NO_CHANGES):
            raise BrokerError(f"Stop/target modification rejected ({result.retcode}): {result.comment}")

    def cancel_entries(self):
        api = self.api
        orders = self.required(api.orders_get(symbol=self.symbol), "active orders")
        for order in orders:
            if order.magic != self.settings.magic_number:
                continue
            result = self.required(api.order_send(dict(action=api.TRADE_ACTION_REMOVE,
                                                       order=order.ticket)), "order removal")
            if result.retcode != api.TRADE_RETCODE_DONE:
                raise BrokerError(f"Order removal rejected ({result.retcode}): {result.comment}")
