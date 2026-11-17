"""Executes parsed signals on MetaTrader 5."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from .config import Mt5Cfg, TradingCfg
from .parser import Command, Signal

log = logging.getLogger("copier.trader")

PLAUSIBLE = 0.05   # a new SL/TP further than 5% from price is treated as a typo


class Skip(Exception):
    """The signal was understood but deliberately not traded."""


class Trader:
    def __init__(self, cfg: TradingCfg, mt5_cfg: Mt5Cfg, mt5=None):
        if mt5 is None:
            import MetaTrader5 as mt5  # Windows only; imported lazily so the parser works anywhere
        self.mt5, self.cfg, self.mt5_cfg = mt5, cfg, mt5_cfg
        self._be_wanted: set[int] = set()   # positions waiting for price to allow a breakeven stop

    def connect(self) -> str:
        c, kw = self.mt5_cfg, {}
        if c.path:
            kw["path"] = c.path
        if c.login:
            kw.update(login=int(c.login), password=c.password, server=c.server)
        if not self.mt5.initialize(**kw):
            raise RuntimeError(f"MT5 initialize failed: {self.mt5.last_error()}")
        acc = self.mt5.account_info()
        if acc is None:
            raise RuntimeError(f"MT5 not logged in: {self.mt5.last_error()}")
        term = self.mt5.terminal_info()
        if term is not None and not term.trade_allowed and not self.cfg.dry_run:
            raise RuntimeError("Algo Trading is disabled in the MT5 terminal - enable it in the toolbar")
        return f"{acc.login} @ {acc.server}, balance {acc.balance:.2f} {acc.currency}"

    # ------------------------------------------------------------------ helpers
    def _ensure_connected(self) -> None:
        """Reconnect if the terminal was restarted; refuse to act without a broker connection."""
        term = self.mt5.terminal_info()
        if term is None:
            log.warning("Lost the MT5 terminal - reconnecting")
            self.connect()
            term = self.mt5.terminal_info()
        if term is not None and not term.connected:
            raise Skip("MT5 terminal has no connection to the broker")

    def _submit(self, req: dict):
        """order_send, retrying with the other filling modes if the broker rejects ours."""
        mt5 = self.mt5
        res = mt5.order_send(req)
        if res is not None and res.retcode == 10030 and "type_filling" in req:   # unsupported filling mode
            for mode in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
                if mode != req["type_filling"]:
                    res = mt5.order_send({**req, "type_filling": mode})
                    if res is None or res.retcode != 10030:
                        break
        return res

    def _broker_symbol(self, symbol: str) -> str:
        return self.cfg.symbol_map.get(symbol, symbol) + self.cfg.symbol_suffix

    def _open_count(self) -> int:
        mine = lambda items: sum(1 for i in items or () if i.magic == self.cfg.magic)
        return mine(self.mt5.positions_get()) + mine(self.mt5.orders_get())

    def _total_volume(self, name: str, is_buy: bool, price: float, sl: float) -> float:
        if self.cfg.risk_mode == "fixed":
            vol = self.cfg.fixed_lot
        else:
            otype = self.mt5.ORDER_TYPE_BUY if is_buy else self.mt5.ORDER_TYPE_SELL
            loss = self.mt5.order_calc_profit(otype, name, 1.0, price, sl)
            if loss is None or loss >= 0:
                raise Skip(f"could not calculate risk for {name}: {self.mt5.last_error()}")
            balance = self.mt5.account_info().balance
            vol = balance * self.cfg.risk_percent / 100 / abs(loss)
        return min(vol, self.cfg.max_lot)

    def daily_loss_percent(self) -> float:
        """How far the account (closed trades + open ones) is down since midnight, in %."""
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deals = self.mt5.history_deals_get(midnight, datetime.now() + timedelta(days=1)) or ()
        realized = sum(d.profit + d.swap + d.commission + getattr(d, "fee", 0.0)
                       for d in deals if d.type in (0, 1))          # buy/sell deals, not deposits
        acc = self.mt5.account_info()
        start = acc.balance - realized
        equity = acc.equity - getattr(acc, "credit", 0.0)          # broker credit is not your money
        return (start - equity) / start * 100 if start > 0 else 0.0

    def _filling(self, info) -> int:
        if info.filling_mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if info.filling_mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    # ------------------------------------------------------------------ execute
    def execute(self, sig: Signal, tag: str = "tg-copier") -> list[str]:
        """Place the orders for one signal. Returns report lines; raises Skip.

        `tag` is stored as the order comment so follow-up messages that reply to
        the signal ("set breakeven", "cancel it") can find its trades.
        """
        mt5, cfg = self.mt5, self.cfg
        self._ensure_connected()
        if cfg.allowed_symbols and sig.symbol not in cfg.allowed_symbols:
            raise Skip(f"{sig.symbol} is not in allowed_symbols")
        if cfg.require_tp and not sig.tps:
            raise Skip("no take profit")

        name = self._broker_symbol(sig.symbol)
        info = mt5.symbol_info(name)
        if info is None:
            raise Skip(f"symbol {name} not found at broker (check symbol_suffix / symbol_map)")
        if not info.visible and not mt5.symbol_select(name, True):
            raise Skip(f"cannot enable {name} in Market Watch")
        tick = mt5.symbol_info_tick(name)
        if tick is None or not tick.ask or not tick.bid:
            raise Skip(f"no price for {name} (market closed?)")

        if self._open_count() >= cfg.max_open_trades:
            raise Skip(f"max_open_trades ({cfg.max_open_trades}) reached")
        if cfg.max_daily_loss_percent > 0:
            down = self.daily_loss_percent()
            if down >= cfg.max_daily_loss_percent:
                raise Skip(f"daily loss limit reached (down {down:.1f}% today, "
                           f"limit {cfg.max_daily_loss_percent}%)")

        is_buy = sig.side == "buy"
        market = tick.ask if is_buy else tick.bid
        pending, price = sig.pending, sig.entry
        if not pending and sig.entry is not None:
            d = 1 if is_buy else -1
            stop = abs(sig.entry - sig.sl)
            low = sig.entry if sig.entry_low is None else sig.entry_low
            high = sig.entry if sig.entry_high is None else sig.entry_high
            near = high if is_buy else low      # edge of the zone price reaches first on a pullback
            if (sig.entry - market) * d > cfg.max_entry_drift * stop:
                raise Skip(f"price {market} moved too far from signal entry {sig.entry} towards the stop")
            if (market - near) * d > cfg.market_entry_tolerance * stop:
                # Price is on the profit side and has not come back to the zone:
                # wait for it with a limit order at the edge it will touch first.
                if not cfg.limit_if_away_from_entry:
                    raise Skip(f"price {market} is not at the entry zone {low}-{high}")
                pending, price = True, near
        if not pending:
            price = market

        if (sig.sl < price) != is_buy:
            raise Skip(f"price {price} is already past the stop loss {sig.sl}")
        tps = [tp for tp in sig.tps if (tp > price) == is_buy]
        if sig.tps and not tps:
            raise Skip("price is already beyond every take profit")
        for level in (sig.sl, *sig.tps):
            if abs(level - price) / price > 2 * PLAUSIBLE:
                raise Skip(f"level {level} is implausibly far from the price {price} - typo in the signal?")

        # Size the whole signal, then split it across the take profits.
        step, vmin = info.volume_step, info.volume_min
        total = min(self._total_volume(name, is_buy, price, sig.sl), info.volume_max)
        n = min(max(len(tps), 1), cfg.max_tps, int(total / vmin + 1e-9))
        if n < 1:
            raise Skip(f"calculated size {total:.4f} lots is below the broker minimum {vmin}")
        volume = round(math.floor(total / n / step + 1e-9) * step, 8)
        targets = tps[:n] if tps else [0.0]

        if pending:
            action = mt5.TRADE_ACTION_PENDING
            if is_buy:
                otype = mt5.ORDER_TYPE_BUY_LIMIT if price < market else mt5.ORDER_TYPE_BUY_STOP
            else:
                otype = mt5.ORDER_TYPE_SELL_LIMIT if price > market else mt5.ORDER_TYPE_SELL_STOP
            filling = mt5.ORDER_FILLING_RETURN
        else:
            action = mt5.TRADE_ACTION_DEAL
            otype = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
            filling = self._filling(info)

        # Let the broker expire unfilled orders if it supports that (works even when the
        # bot is off); expire_pending() below covers brokers that do not.
        broker_expiry = (pending and cfg.pending_expiry_minutes > 0
                         and getattr(info, "expiration_mode", 0) & 4)
        report = []
        for tp in targets:
            if not pending:                     # fresh price for every market order
                now = mt5.symbol_info_tick(name)
                if now is not None and now.ask and now.bid:
                    price = now.ask if is_buy else now.bid
            req = {
                "action": action, "symbol": name, "volume": volume, "type": otype,
                "price": round(price, info.digits), "sl": round(sig.sl, info.digits),
                "tp": round(tp, info.digits), "deviation": cfg.deviation_points,
                "magic": cfg.magic,
                "comment": f"{tag}-{sig.tps.index(tp) + 1 if tp else 1}",     # e.g. tg-1234-2 = TP2
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
            }
            if broker_expiry:
                req["type_time"] = mt5.ORDER_TIME_SPECIFIED
                req["expiration"] = int(tick.time) + cfg.pending_expiry_minutes * 60
            kind = f"{sig.side.upper()} LIMIT/STOP" if pending else sig.side.upper()
            desc = f"{kind} {volume} {name} @ {req['price']} sl={req['sl']} tp={req['tp'] or '-'}"

            if cfg.dry_run:
                res = mt5.order_check(req)
                ok = res is not None and res.retcode == 0
                report.append(f"[DRY RUN] {desc} -> " + ("broker would accept" if ok else
                              f"broker would REJECT: {getattr(res, 'comment', mt5.last_error())}"))
                continue

            res = self._submit(req)
            if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                report.append(f"OK {desc} (ticket {res.order})")
            else:
                detail = f"{res.retcode} {res.comment}" if res is not None else str(mt5.last_error())
                report.append(f"FAILED {desc}: {detail}")
        for line in report:
            log.info(line)
        return report

    # --------------------------------------------------------------- management
    def _mine(self, items, tag: str | None):
        """The bot's own trades; with a tag ("tg-1234") only those of that signal."""
        return [i for i in items or () if i.magic == self.cfg.magic
                and (tag is None or i.comment == tag or i.comment.startswith(tag + "-"))]

    @staticmethod
    def _signal_of(comment: str) -> str:
        return "-".join(comment.split("-")[:2])            # "tg-1234-2" -> "tg-1234"

    @staticmethod
    def _tp_index(comment: str) -> int | None:
        parts = comment.split("-")
        return int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

    def _is_buy(self, p) -> bool:
        return p.type == self.mt5.POSITION_TYPE_BUY

    def _secured(self, p) -> bool:
        """The stop is already at breakeven or better."""
        return bool(p.sl) and ((p.sl >= p.price_open) if self._is_buy(p) else (p.sl <= p.price_open))

    def _try(self, req: dict) -> tuple[bool, str]:
        if self.cfg.dry_run:
            return True, ""
        res = self._submit(req)
        if res is not None and res.retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED):
            return True, ""
        return False, f"{res.retcode} {res.comment}" if res is not None else str(self.mt5.last_error())

    def _send(self, req: dict, desc: str) -> str:
        if self.cfg.dry_run:
            return f"[DRY RUN] would {desc}"
        ok, detail = self._try(req)
        return f"OK {desc}" if ok else f"FAILED {desc}: {detail}"

    # ---- breakeven
    def _can_breakeven(self, p) -> bool:
        """The broker only accepts a stop at the entry once price has moved past it."""
        tick, info = self.mt5.symbol_info_tick(p.symbol), self.mt5.symbol_info(p.symbol)
        if tick is None or info is None:
            return False
        gap = getattr(info, "trade_stops_level", 0) * getattr(info, "point", 0.0)
        return tick.bid > p.price_open + gap if self._is_buy(p) else tick.ask < p.price_open - gap

    def _be_request(self, p) -> dict:
        return {"action": self.mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                "sl": p.price_open, "tp": p.tp}

    def breakeven(self, tag: str | None = None) -> list[str]:
        """Move the stop loss to the entry price on the bot's open positions."""
        out = []
        for p in self._mine(self.mt5.positions_get(), tag):
            if self._secured(p):
                continue
            if not self._can_breakeven(p):
                self._be_wanted.add(p.ticket)
                out.append(f"breakeven queued for {p.symbol} #{p.ticket}: price is not past the entry "
                           f"{p.price_open} yet - it will be set as soon as it is")
                continue
            line = self._send(self._be_request(p), f"move SL to breakeven {p.price_open} on {p.symbol} #{p.ticket}")
            if line.startswith("FAILED"):
                self._be_wanted.add(p.ticket)          # retried by housekeeping()
            out.append(line)
        return out or ["no open positions to move to breakeven"]

    def retry_breakeven(self) -> list[str]:
        if not self._be_wanted:
            return []
        positions = self.mt5.positions_get()
        if positions is None:                          # terminal unavailable: keep waiting
            return []
        mine, out = {p.ticket: p for p in self._mine(positions, None)}, []
        for ticket in list(self._be_wanted):
            p = mine.get(ticket)
            if p is None or self._secured(p):          # closed, or already protected
                self._be_wanted.discard(ticket)
            elif self._can_breakeven(p) and self._try(self._be_request(p))[0]:
                self._be_wanted.discard(ticket)
                out.append(f"OK moved SL to breakeven {p.price_open} on {p.symbol} #{p.ticket} (was queued)")
        return out

    # ---- stop / target changes
    def move_sl(self, price: float, tag: str | None = None) -> list[str]:
        """"Move SL at 3950": trail the stop. Only ever tightens it."""
        out, positions = [], self._mine(self.mt5.positions_get(), tag)
        for p in positions:
            sl = round(price, self.mt5.symbol_info(p.symbol).digits)
            if abs(sl - p.price_open) / p.price_open > PLAUSIBLE:
                out.append(f"ignored: {price} is not a plausible stop for {p.symbol} #{p.ticket}")
            elif not p.sl or ((sl > p.sl) if self._is_buy(p) else (sl < p.sl)):
                req = {"action": self.mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                       "sl": sl, "tp": p.tp}
                out.append(self._send(req, f"move SL to {sl} on {p.symbol} #{p.ticket}"))
        if not positions:
            return ["no open positions to move the stop on"]
        return out or [f"stops are already at {price} or better"]

    def _close(self, p) -> str:
        mt5, was_buy = self.mt5, self._is_buy(p)
        tick, info = mt5.symbol_info_tick(p.symbol), mt5.symbol_info(p.symbol)
        req = {"action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol,
               "volume": p.volume, "type": mt5.ORDER_TYPE_SELL if was_buy else mt5.ORDER_TYPE_BUY,
               "price": tick.bid if was_buy else tick.ask, "deviation": self.cfg.deviation_points,
               "magic": self.cfg.magic, "comment": "tg-close", "type_filling": self._filling(info)}
        return self._send(req, f"close {p.volume} {p.symbol} #{p.ticket}")

    def take_tp(self, n: int, price: float | None = None, tag: str | None = None) -> list[str]:
        """"Take TP1 now" closes the TP1 position; "Take TP1 at 3868" moves its target."""
        mt5, out = self.mt5, []
        for p in self._mine(mt5.positions_get(), tag):
            if self._tp_index(p.comment) != n:
                continue
            if price is None:
                out.append(self._close(p))
                continue
            tick, info = mt5.symbol_info_tick(p.symbol), mt5.symbol_info(p.symbol)
            buy = self._is_buy(p)
            market = tick.bid if buy else tick.ask
            tp = round(price, info.digits)
            if abs(tp - market) / market > PLAUSIBLE:
                out.append(f"ignored: {price} is not a plausible take profit for {p.symbol} #{p.ticket}")
            elif (market >= tp) if buy else (market <= tp):
                out.append(self._close(p))             # price is already there: take it
            else:
                req = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                       "sl": p.sl, "tp": tp}
                out.append(self._send(req, f"move TP{n} to {tp} on {p.symbol} #{p.ticket}"))
        if price is not None:
            for o in self._mine(mt5.orders_get(), tag):
                if self._tp_index(o.comment) != n or abs(price - o.price_open) / o.price_open > PLAUSIBLE:
                    continue
                tp = round(price, mt5.symbol_info(o.symbol).digits)
                req = {"action": mt5.TRADE_ACTION_MODIFY, "order": o.ticket, "price": o.price_open,
                       "sl": o.sl, "tp": tp, "type_time": o.type_time, "expiration": o.time_expiration}
                out.append(self._send(req, f"move TP{n} to {tp} on pending {o.symbol} #{o.ticket}"))
        return out or [f"no open TP{n} position"]

    def activate(self, side: str | None = None, tag: str | None = None) -> list[str]:
        """"Buy now": the channel entered at market. Turn our waiting limit orders into
        market orders, provided price is still close to where they were waiting."""
        mt5, cfg, out = self.mt5, self.cfg, []
        for o in self._mine(mt5.orders_get(), tag):
            is_buy = o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP)
            if side and (side == "buy") != is_buy:
                continue
            tick, info = mt5.symbol_info_tick(o.symbol), mt5.symbol_info(o.symbol)
            if tick is None or info is None or not o.sl:
                continue
            market, d = (tick.ask, 1) if is_buy else (tick.bid, -1)
            market = round(market, info.digits)
            too_far = abs(market - o.price_open) > cfg.activate_max_distance * abs(o.price_open - o.sl)
            if too_far or (market - o.sl) * d <= 0 or (o.tp and (o.tp - market) * d <= 0):
                out.append(f"left pending {o.symbol} #{o.ticket}: price {market} is too far from its entry {o.price_open}")
                continue
            ok, detail = self._try({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            if not ok:
                out.append(f"FAILED to replace pending {o.symbol} #{o.ticket}: {detail}")
                continue
            word = "BUY" if is_buy else "SELL"
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": o.symbol, "volume": o.volume_current,
                   "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL, "price": market,
                   "sl": o.sl, "tp": o.tp, "deviation": cfg.deviation_points, "magic": cfg.magic,
                   "comment": o.comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info)}
            out.append(self._send(req, f"enter now: {word} {o.volume_current} {o.symbol} @ {market} "
                                       f"sl={o.sl} tp={o.tp or '-'} (was waiting at {o.price_open})"))
        return out

    # ---- cancel / close
    def cancel_pending(self, tag: str | None = None) -> list[str]:
        out = []
        for o in self._mine(self.mt5.orders_get(), tag):
            req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
            out.append(self._send(req, f"cancel pending order on {o.symbol} #{o.ticket}"))
        return out or ["no pending orders to cancel"]

    def close_positions(self, tag: str | None = None) -> list[str]:
        return [self._close(p) for p in self._mine(self.mt5.positions_get(), tag)] or ["no open positions to close"]

    def status(self) -> list[str]:
        """A few lines on the terminal, the account and the bot's trades (for /status)."""
        mt5, cfg = self.mt5, self.cfg
        term, acc = mt5.terminal_info(), mt5.account_info()
        if term is None or acc is None:
            return ["MT5: NOT reachable - is the terminal open and logged in?"]
        lines = ["MT5: " + ("connected to the broker" if term.connected else "NO connection to the broker")
                 + ("" if term.trade_allowed else " - Algo Trading is OFF")]
        own = acc.equity - getattr(acc, "credit", 0.0)
        lines.append(f"Account {acc.login}: balance {acc.balance:.2f}, equity {own:.2f} {acc.currency}")
        positions, waiting = self._mine(mt5.positions_get(), None), self._mine(mt5.orders_get(), None)
        floating = sum(getattr(p, "profit", 0.0) for p in positions)
        lines.append(f"Bot trades: {len(positions)} open ({floating:+.2f} {acc.currency}), "
                     f"{len(waiting)} limit order(s) waiting")
        if cfg.max_daily_loss_percent > 0:
            today = round(-self.daily_loss_percent(), 1) + 0.0      # + 0.0 turns -0.0 into 0.0
            lines.append(f"Today: {today:+.1f}%  (new trades stop at "
                         f"-{cfg.max_daily_loss_percent:g}%)")
        return lines

    def trades(self) -> list[str]:
        """One line per open position and waiting order of the bot (for /trades)."""
        mt5 = self.mt5
        acc = mt5.account_info()
        currency = acc.currency if acc is not None else ""
        names = {mt5.ORDER_TYPE_BUY_LIMIT: "BUY LIMIT", mt5.ORDER_TYPE_SELL_LIMIT: "SELL LIMIT",
                 mt5.ORDER_TYPE_BUY_STOP: "BUY STOP", mt5.ORDER_TYPE_SELL_STOP: "SELL STOP"}
        label = lambda item: f"TP{self._tp_index(item.comment)}" if self._tp_index(item.comment) else ""
        out = []
        for p in self._mine(mt5.positions_get(), None):
            out.append(f"{'BUY' if self._is_buy(p) else 'SELL'} {p.volume} {p.symbol} @ {p.price_open}  "
                       f"sl {p.sl or '-'}  tp {p.tp or '-'}  {getattr(p, 'profit', 0.0):+.2f} {currency}  {label(p)}".rstrip())
        for o in self._mine(mt5.orders_get(), None):
            out.append(f"waiting: {names.get(o.type, 'ORDER')} {o.volume_current} {o.symbol} @ {o.price_open}  "
                       f"sl {o.sl or '-'}  tp {o.tp or '-'}  {label(o)}".rstrip())
        return out or ["No open positions or waiting orders from the bot."]

    def housekeeping(self) -> list[str]:
        """Called every few seconds: queued breakevens and expired pending orders."""
        out = self.retry_breakeven()
        for line in out:
            log.info(line)
        return out + self.expire_pending()

    def expire_pending(self) -> list[str]:
        """Cancel the bot's pending orders older than pending_expiry_minutes."""
        limit = self.cfg.pending_expiry_minutes * 60
        if limit <= 0:
            return []
        out = []
        for o in self._mine(self.mt5.orders_get(), None):
            tick = self.mt5.symbol_info_tick(o.symbol)      # server time, same clock as time_setup
            if tick is not None and tick.time - o.time_setup > limit:
                req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
                out.append(self._send(req, f"cancel expired pending order on {o.symbol} #{o.ticket}"))
        for line in out:
            log.info(line)
        return out

    def update_levels(self, tag: str, old: Signal, new: Signal) -> list[str]:
        """The channel edited a signal we traded: move its SL / TPs to the new values."""
        self._ensure_connected()
        mt5 = self.mt5
        same_count = len(old.tps) == len(new.tps)
        out = []

        def levels(item, keep_sl: bool):
            digits = mt5.symbol_info(item.symbol).digits
            sl = item.sl if keep_sl else round(new.sl, digits)
            tp = item.tp
            if same_count:
                for before, after in zip(old.tps, new.tps):
                    if round(before, digits) == round(item.tp, digits):
                        tp = round(after, digits)
                        break
            return sl, tp

        for p in self._mine(mt5.positions_get(), tag):
            # Never loosen a stop that is already at breakeven or better.
            is_buy = p.type == mt5.POSITION_TYPE_BUY
            secured = bool(p.sl) and ((p.sl >= p.price_open) if is_buy else (p.sl <= p.price_open))
            sl, tp = levels(p, keep_sl=secured)
            if (sl, tp) != (p.sl, p.tp):
                req = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                       "sl": sl, "tp": tp}
                out.append(self._send(req, f"update {p.symbol} #{p.ticket} to sl={sl} tp={tp or '-'}"))
        for o in self._mine(mt5.orders_get(), tag):
            sl, tp = levels(o, keep_sl=False)
            if (sl, tp) != (o.sl, o.tp):
                req = {"action": mt5.TRADE_ACTION_MODIFY, "order": o.ticket, "price": o.price_open,
                       "sl": sl, "tp": tp, "type_time": o.type_time, "expiration": o.time_expiration}
                out.append(self._send(req, f"update pending {o.symbol} #{o.ticket} to sl={sl} tp={tp or '-'}"))
        if out and not same_count:
            out.append("the number of take profits changed - only the stop loss was updated")
        for line in out:
            log.info(line)
        return out

    def manage(self, command: Command | str, tag: str | None = None) -> list[str]:
        self._ensure_connected()
        c = Command(command) if isinstance(command, str) else command
        if c.name == "breakeven":
            report = self.breakeven(tag)
            # Orders of ours that have not filled yet are left waiting by default: this
            # channel says "set breakeven" seconds after a signal, long before price has
            # come back to the entry zone. With breakeven_cancels_pending they are removed
            # instead (only when a single signal is active, to avoid guessing).
            pending = self._mine(self.mt5.orders_get(), tag)
            active = {self._signal_of(i.comment) for i in pending + self._mine(self.mt5.positions_get(), tag)}
            if pending and self.cfg.breakeven_cancels_pending and len(active) == 1:
                report += self.cancel_pending(tag)
            elif pending:
                if report == ["no open positions to move to breakeven"]:
                    report = []
                report.append(f"{len(pending)} order(s) not filled yet - left waiting at the entry zone")
        elif c.name == "move_sl":
            report = self.move_sl(c.price, tag)
        elif c.name == "take_tp":
            report = self.take_tp(c.n, c.price, tag)
        elif c.name == "activate":
            report = self.activate(c.side, tag)
        elif c.name == "close":
            report = self.cancel_pending(tag) + self.close_positions(tag)
        else:  # cancel
            report = self.cancel_pending(tag)
            if self.cfg.cancel_closes_positions:
                report += self.close_positions(tag)
        for line in report:
            log.info(line)
        return report
