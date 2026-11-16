"""Executes parsed signals on MetaTrader 5."""
from __future__ import annotations

import logging
import math

from .config import Mt5Cfg, TradingCfg
from .parser import Signal

log = logging.getLogger("copier.trader")


class Skip(Exception):
    """The signal was understood but deliberately not traded."""


class Trader:
    def __init__(self, cfg: TradingCfg, mt5_cfg: Mt5Cfg, mt5=None):
        if mt5 is None:
            import MetaTrader5 as mt5  # Windows only; imported lazily so the parser works anywhere
        self.mt5, self.cfg, self.mt5_cfg = mt5, cfg, mt5_cfg

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

        is_buy = sig.side == "buy"
        market = tick.ask if is_buy else tick.bid
        pending = sig.pending
        if not pending and sig.entry is not None:
            drift, allowed = abs(market - sig.entry), cfg.max_entry_drift * abs(sig.entry - sig.sl)
            if drift > allowed:
                # Price is on the profit side of the entry zone: wait for it to come
                # back with a limit order. On the stop-loss side: too late, skip.
                waiting_for_zone = (market < sig.entry) != is_buy
                if not (waiting_for_zone and cfg.limit_if_away_from_entry):
                    raise Skip(f"price {market} moved too far from signal entry {sig.entry}")
                pending = True
        price = sig.entry if pending else market

        if (sig.sl < price) != is_buy:
            raise Skip(f"price {price} is already past the stop loss {sig.sl}")
        tps = [tp for tp in sig.tps if (tp > price) == is_buy]
        if sig.tps and not tps:
            raise Skip("price is already beyond every take profit")

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
                "magic": cfg.magic, "comment": tag,
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
        return [i for i in items or () if i.magic == self.cfg.magic and (tag is None or i.comment == tag)]

    def _send(self, req: dict, desc: str) -> str:
        if self.cfg.dry_run:
            return f"[DRY RUN] would {desc}"
        res = self._submit(req)
        if res is not None and res.retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED):
            return f"OK {desc}"
        detail = f"{res.retcode} {res.comment}" if res is not None else str(self.mt5.last_error())
        return f"FAILED {desc}: {detail}"

    def breakeven(self, tag: str | None = None) -> list[str]:
        """Move the stop loss to the entry price on the bot's open positions."""
        out = []
        for p in self._mine(self.mt5.positions_get(), tag):
            if p.sl == p.price_open:
                continue
            req = {"action": self.mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                   "sl": p.price_open, "tp": p.tp}
            out.append(self._send(req, f"move SL to breakeven {p.price_open} on {p.symbol} #{p.ticket}"))
        return out or ["no open positions to move to breakeven"]

    def cancel_pending(self, tag: str | None = None) -> list[str]:
        out = []
        for o in self._mine(self.mt5.orders_get(), tag):
            req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket}
            out.append(self._send(req, f"cancel pending order on {o.symbol} #{o.ticket}"))
        return out or ["no pending orders to cancel"]

    def close_positions(self, tag: str | None = None) -> list[str]:
        mt5, out = self.mt5, []
        for p in self._mine(mt5.positions_get(), tag):
            was_buy = p.type == mt5.POSITION_TYPE_BUY
            tick, info = mt5.symbol_info_tick(p.symbol), mt5.symbol_info(p.symbol)
            req = {"action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol,
                   "volume": p.volume, "type": mt5.ORDER_TYPE_SELL if was_buy else mt5.ORDER_TYPE_BUY,
                   "price": tick.bid if was_buy else tick.ask, "deviation": self.cfg.deviation_points,
                   "magic": self.cfg.magic, "comment": "tg-close", "type_filling": self._filling(info)}
            out.append(self._send(req, f"close {p.volume} {p.symbol} #{p.ticket}"))
        return out or ["no open positions to close"]

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

    def manage(self, command: str, tag: str | None = None) -> list[str]:
        self._ensure_connected()
        if command == "breakeven":
            report = self.breakeven(tag)
            # The channel is already in profit on this trade, so orders of ours that never
            # filled would now be a late entry with the full stop: remove them. Without a
            # reply we only do this when a single signal is active, to avoid guessing.
            pending = self._mine(self.mt5.orders_get(), tag)
            active = {i.comment for i in pending + self._mine(self.mt5.positions_get(), tag)}
            if pending and len(active) == 1:
                report += self.cancel_pending(tag)
        elif command == "close":
            report = self.cancel_pending(tag) + self.close_positions(tag)
        else:  # cancel
            report = self.cancel_pending(tag)
            if self.cfg.cancel_closes_positions:
                report += self.close_positions(tag)
        for line in report:
            log.info(line)
        return report
