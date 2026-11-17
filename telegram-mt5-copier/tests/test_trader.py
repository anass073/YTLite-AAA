from types import SimpleNamespace as NS

import pytest

from copier.config import Mt5Cfg, TradingCfg
from copier.parser import Signal
from copier.trader import Skip, Trader


class FakeMT5:
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 2, 3, 4, 5
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING = 1, 5
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC, ORDER_TIME_SPECIFIED = 0, 2
    TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE = 10008, 10009

    def __init__(self, bid=2650.0, ask=2650.2, balance=10_000.0):
        self.bid, self.ask, self.balance = bid, ask, balance
        self.sent, self.checked, self.open = [], [], []

    def symbol_info(self, name):
        if name != "XAUUSD":
            return None
        return NS(visible=True, digits=2, volume_min=0.01, volume_max=50.0,
                  volume_step=0.01, filling_mode=2)

    def symbol_info_tick(self, name): return NS(bid=self.bid, ask=self.ask, time=1_700_000_000)
    equity, deals = None, ()
    def account_info(self):
        return NS(balance=self.balance, equity=self.balance if self.equity is None else self.equity,
                  login=1, server="x", currency="USD")
    def history_deals_get(self, start, end): return self.deals
    def positions_get(self): return self.open
    def orders_get(self): return []
    def last_error(self): return (0, "")
    def terminal_info(self): return NS(connected=True, trade_allowed=True)
    def order_calc_profit(self, otype, name, lots, price, sl):   # gold: 100 oz per lot
        return (sl - price if otype == self.ORDER_TYPE_BUY else price - sl) * 100 * lots
    def order_check(self, req): self.checked.append(req); return NS(retcode=0, comment="Done")
    def order_send(self, req): self.sent.append(req); return NS(retcode=10009, order=len(self.sent), comment="")


def make(mt5=None, **cfg):
    mt5 = mt5 or FakeMT5()
    return Trader(TradingCfg(**{"dry_run": False, **cfg}), Mt5Cfg(), mt5), mt5


def test_risk_percent_sizing_split_across_tps():
    t, m = make(risk_percent=1.0)
    t.execute(Signal("buy", "XAUUSD", 2640.2, [2655.0, 2660.0], 2650.0))
    # risk $100, SL distance $10 x 100oz = $1000/lot -> 0.10 lots -> 2 x 0.05
    assert [(r["volume"], r["tp"]) for r in m.sent] == [(0.05, 2655.0), (0.05, 2660.0)]
    assert all(r["price"] == 2650.2 and r["sl"] == 2640.2 and r["type"] == 0 for r in m.sent)
    assert m.sent[0]["type_filling"] == m.ORDER_FILLING_IOC


def test_small_size_collapses_to_first_tp():
    t, m = make(risk_mode="fixed", fixed_lot=0.01)
    t.execute(Signal("sell", "XAUUSD", 2660.0, [2645.0, 2640.0], 2650.0))
    assert [(r["volume"], r["tp"], r["type"]) for r in m.sent] == [(0.01, 2645.0, 1)]


def test_dry_run_never_sends():
    t, m = make(dry_run=True)
    report = t.execute(Signal("buy", "XAUUSD", 2640.0, [2660.0]))
    assert m.sent == [] and len(m.checked) == 1 and "DRY RUN" in report[0]


def test_pending_order_type_follows_price():
    t, m = make()
    t.execute(Signal("buy", "XAUUSD", 2630.0, [2660.0], 2640.0, pending=True))
    t.execute(Signal("buy", "XAUUSD", 2650.0, [2670.0], 2660.0, pending=True))
    assert [(r["action"], r["type"], r["price"]) for r in m.sent] == [(5, 2, 2640.0), (5, 4, 2660.0)]


def test_max_lot_caps_size():
    t, m = make(risk_percent=50.0, max_lot=0.2, max_tps=1)
    t.execute(Signal("buy", "XAUUSD", 2649.0, [2660.0]))
    assert m.sent[0]["volume"] == 0.2


@pytest.mark.parametrize("sig,cfg,reason", [
    (Signal("buy", "XAUUSD", 2640.0, [2680.0], 2665.0), {}, "moved too far"),
    (Signal("buy", "XAUUSD", 2655.0, [2660.0]), {}, "past the stop loss"),
    (Signal("buy", "XAUUSD", 2640.0, [2650.1]), {}, "beyond every take profit"),
    (Signal("buy", "EURUSD", 1.08, [1.09]), {}, "not found at broker"),
    (Signal("buy", "XAUUSD", 2640.0, [2660.0]), {"allowed_symbols": ["EURUSD"]}, "allowed_symbols"),
    (Signal("buy", "XAUUSD", 2640.0, []), {"require_tp": True}, "no take profit"),
    (Signal("buy", "XAUUSD", 2640.0, [2660.0]), {"risk_percent": 0.001}, "below the broker minimum"),
])
def test_skips(sig, cfg, reason):
    t, m = make(**cfg)
    with pytest.raises(Skip, match=reason):
        t.execute(sig)
    assert m.sent == []


def test_max_open_trades():
    m = FakeMT5()
    m.open = [NS(magic=770077)] * 5
    t, _ = make(m)
    with pytest.raises(Skip, match="max_open_trades"):
        t.execute(Signal("buy", "XAUUSD", 2640.0, [2660.0]))


def test_detect_suffix():
    from copier.setup import detect_suffix
    assert detect_suffix(["EURUSD", "EURUSDm"]) == ""
    assert detect_suffix(["EURUSD.pro", "XAUUSD.pro"]) == ".pro"
    assert detect_suffix(["XAUUSDm", "US30m"]) == "m"
    assert detect_suffix([]) == ""


# --- entry zones and follow-up instructions ----------------------------------
FakeMT5.TRADE_ACTION_SLTP, FakeMT5.TRADE_ACTION_REMOVE, FakeMT5.POSITION_TYPE_BUY = 6, 8, 0
ZONE = dict(side="sell", symbol="XAUUSD", sl=3863.0, tps=[3850.0, 3845.0, 3810.0], entry=3856.5,
            entry_low=3855.0, entry_high=3858.0)


def test_zone_market_when_price_is_in_zone():
    t, m = make(FakeMT5(bid=3856.0, ask=3856.2), risk_percent=3.0)
    t.execute(Signal(**ZONE), tag="tg-42")
    assert [(r["action"], r["type"], r["price"], r["tp"]) for r in m.sent] == [
        (1, 1, 3856.0, 3850.0), (1, 1, 3856.0, 3845.0), (1, 1, 3856.0, 3810.0)]
    assert [r["comment"] for r in m.sent] == ["tg-42-1", "tg-42-2", "tg-42-3"]


def test_zone_limit_when_price_has_not_reached_it():
    t, m = make(FakeMT5(bid=3851.0, ask=3851.2), risk_percent=3.0)
    t.execute(Signal(**ZONE))
    # sell limit at the edge of the zone price reaches first
    assert [(r["action"], r["type"], r["price"]) for r in m.sent] == [(5, 3, 3855.0)] * 3


def test_zone_skipped_when_price_ran_towards_stop():
    t, m = make(FakeMT5(bid=3861.0, ask=3861.2))
    with pytest.raises(Skip, match="moved too far"):
        t.execute(Signal(**ZONE))


def pos(ticket, comment, **kw):
    return NS(ticket=ticket, magic=770077, comment=comment, symbol="XAUUSD", sl=3863.0, tp=3810.0,
              price_open=3856.0, volume=0.05, type=1, **kw)


def test_breakeven_targets_replied_signal_only():
    m = FakeMT5(); m.open = [pos(1, "tg-42"), pos(2, "tg-43"), NS(ticket=3, magic=1, comment="manual")]
    t, _ = make(m)
    t.manage("breakeven", "tg-42")
    assert m.sent == [{"action": 6, "position": 1, "symbol": "XAUUSD", "sl": 3856.0, "tp": 3810.0}]
    m.sent.clear()
    t.manage("breakeven")                       # not a reply -> every bot position, never manual ones
    assert [r["position"] for r in m.sent] == [1, 2]


def test_cancel_removes_pending_only_and_close_closes():
    m = FakeMT5(); m.open = [pos(1, "tg-42")]
    m.orders_get = lambda: [NS(ticket=9, magic=770077, comment="tg-42", symbol="XAUUSD")]
    t, _ = make(m)
    t.manage("cancel")
    assert m.sent == [{"action": 8, "order": 9}]
    m.sent.clear()
    t.manage("close")
    assert [(r["action"], r.get("position"), r.get("type")) for r in m.sent] == [(8, None, None), (1, 1, 0)]
    assert m.sent[1]["price"] == m.ask and m.sent[1]["volume"] == 0.05


def test_management_respects_dry_run():
    m = FakeMT5(); m.open = [pos(1, "tg-42")]
    t, _ = make(m, dry_run=True)
    assert "DRY RUN" in t.manage("breakeven")[0] and m.sent == []


# --- robustness ----------------------------------------------------------------
def order(ticket, comment, age_s):
    return NS(ticket=ticket, magic=770077, comment=comment, symbol="XAUUSD", time_setup=1_700_000_000 - age_s)


def test_expired_pending_orders_are_cancelled():
    m = FakeMT5()
    m.orders_get = lambda: [order(1, "tg-1", 121 * 60), order(2, "tg-2", 5 * 60)]
    t, _ = make(m, pending_expiry_minutes=120)
    t.expire_pending()
    assert m.sent == [{"action": 8, "order": 1}]
    t2, m2 = make(pending_expiry_minutes=0)
    assert t2.expire_pending() == []


def test_breakeven_cancels_unfilled_orders_of_the_same_signal():
    m = FakeMT5(); m.open = [pos(1, "tg-42")]
    m.orders_get = lambda: [order(9, "tg-42", 60)]
    t, _ = make(m, breakeven_cancels_pending=True)
    t.manage("breakeven")
    assert [r["action"] for r in m.sent] == [6, 8]


def test_breakeven_leaves_unfilled_orders_waiting_by_default():
    m = FakeMT5()
    m.orders_get = lambda: [order(1, "tg-42-1", 16), order(2, "tg-42-2", 16), order(3, "tg-42-3", 16)]
    t, _ = make(m)
    assert t.manage("breakeven") == ["3 order(s) not filled yet - left waiting at the entry zone"]
    assert m.sent == []


def test_breakeven_without_reply_leaves_other_signals_orders_alone():
    m = FakeMT5(); m.open = [pos(1, "tg-42")]
    m.orders_get = lambda: [order(9, "tg-50", 60)]
    t, _ = make(m)
    t.manage("breakeven")
    assert [r["action"] for r in m.sent] == [6]


def test_retries_other_filling_mode_when_rejected():
    m = FakeMT5()
    def send(req):
        m.sent.append(req)
        return NS(retcode=10030 if req["type_filling"] == m.ORDER_FILLING_IOC else 10009, order=1, comment="")
    m.order_send = send
    t, _ = make(m, risk_mode="fixed")
    assert t.execute(Signal("buy", "XAUUSD", 2640.0, [2660.0]))[0].startswith("OK")
    assert [r["type_filling"] for r in m.sent] == [m.ORDER_FILLING_IOC, m.ORDER_FILLING_FOK]


def test_reconnects_when_terminal_was_restarted():
    m = FakeMT5(); calls = []
    states = iter([None])
    m.terminal_info = lambda: next(states, NS(connected=True, trade_allowed=True))
    m.initialize = lambda **kw: calls.append(kw) or True
    t, _ = make(m)
    t.execute(Signal("buy", "XAUUSD", 2640.0, [2660.0]))
    assert calls and m.sent


def test_no_broker_connection_is_skipped():
    m = FakeMT5(); m.terminal_info = lambda: NS(connected=False, trade_allowed=True)
    t, _ = make(m)
    with pytest.raises(Skip, match="no connection"):
        t.execute(Signal("buy", "XAUUSD", 2640.0, [2660.0]))


# --- zone edges, daily loss limit, edited signals ----------------------------------
FakeMT5.TRADE_ACTION_MODIFY = 7


def test_just_below_zone_still_enters_at_market():
    t, m = make(FakeMT5(bid=3854.5, ask=3854.7), risk_percent=3.0)     # 0.5 under the zone, tolerance ~1
    t.execute(Signal(**ZONE))
    assert {r["action"] for r in m.sent} == {1}


def test_buy_zone_limit_sits_at_upper_edge():
    t, m = make(FakeMT5(bid=3870.0, ask=3870.2), risk_percent=3.0)
    t.execute(Signal("buy", "XAUUSD", 3850.0, [3862.0, 3867.0], 3856.5, False, 3855.0, 3858.0))
    assert [(r["action"], r["type"], r["price"]) for r in m.sent] == [(5, 2, 3858.0)] * 2


def test_daily_loss_limit_blocks_new_trades():
    m = FakeMT5(balance=170.0)
    m.deals = [NS(profit=-30.0, swap=0.0, commission=0.0, type=1), NS(profit=5000.0, swap=0, commission=0, type=2)]
    m.equity = 160.0                              # started at 200, now 160 with open trades -> down 20%
    t, _ = make(m, max_daily_loss_percent=20.0, risk_mode="fixed")
    assert round(t.daily_loss_percent(), 6) == 20.0
    with pytest.raises(Skip, match="daily loss limit"):
        t.execute(Signal("buy", "XAUUSD", 2640.0, [2660.0]))
    t2, m2 = make(m, max_daily_loss_percent=25.0, risk_mode="fixed")
    t2.execute(Signal("buy", "XAUUSD", 2640.0, [2660.0]))
    assert m.sent


def test_edited_signal_updates_levels_but_keeps_breakeven():
    old, new = Signal(**ZONE), Signal(**{**ZONE, "sl": 3865.0, "tps": [3851.0, 3845.0, 3810.0]})
    m = FakeMT5()
    m.open = [pos(1, "tg-42"), pos(2, "tg-42"), pos(3, "tg-42"), pos(4, "tg-99")]
    m.open[0].tp = 3850.0                          # the TP1 position
    m.open[1].sl = 3856.0                          # already at breakeven
    m.orders_get = lambda: [NS(ticket=9, magic=770077, comment="tg-42", symbol="XAUUSD", sl=3863.0,
                               tp=3845.0, price_open=3855.0, type_time=0, time_expiration=0)]
    t, _ = make(m)
    t.update_levels("tg-42", old, new)
    assert m.sent == [
        {"action": 6, "position": 1, "symbol": "XAUUSD", "sl": 3865.0, "tp": 3851.0},
        {"action": 6, "position": 3, "symbol": "XAUUSD", "sl": 3865.0, "tp": 3810.0},
        {"action": 7, "order": 9, "price": 3855.0, "sl": 3865.0, "tp": 3845.0, "type_time": 0, "expiration": 0},
    ]
    assert t.update_levels("tg-nothing", old, new) == []


# --- the channel's follow-up instructions -------------------------------------------
from copier.parser import Command


def buys(m, sl=3882.0):
    """Three open buy positions from signal 77, entry 3890, TP1-3."""
    m.open = [NS(ticket=i, magic=770077, comment=f"tg-77-{i}", symbol="XAUUSD", sl=sl, tp=tp,
                 price_open=3890.0, volume=0.01, type=0) for i, tp in ((1, 3894.0), (2, 3898.0), (3, 3930.0))]
    return m


def test_take_tp_now_closes_only_that_position():
    m = buys(FakeMT5(bid=3893.0, ask=3893.2)); t, _ = make(m)
    t.manage(Command("take_tp", n=1))
    assert [(r["action"], r["position"], r["type"], r["price"]) for r in m.sent] == [(1, 1, 1, 3893.0)]


def test_take_tp_at_price_moves_the_target():
    m = buys(FakeMT5(bid=3891.0, ask=3891.2)); t, _ = make(m)
    t.manage(Command("take_tp", price=3893.0, n=1))
    assert m.sent == [{"action": 6, "position": 1, "symbol": "XAUUSD", "sl": 3882.0, "tp": 3893.0}]
    m.sent.clear()
    t.manage(Command("take_tp", price=3890.5, n=2))          # price is already above it: close now
    assert [(r["action"], r["position"]) for r in m.sent] == [(1, 2)]


def test_take_tp_typo_is_ignored():
    m = buys(FakeMT5(bid=3891.0, ask=3891.2)); t, _ = make(m)
    assert "not a plausible" in t.manage(Command("take_tp", price=4902.0, n=3))[0] and m.sent == []


def test_move_sl_only_tightens():
    m = buys(FakeMT5(bid=3899.0, ask=3899.2)); t, _ = make(m)
    t.manage(Command("move_sl", price=3894.0))
    assert [(r["position"], r["sl"]) for r in m.sent] == [(1, 3894.0), (2, 3894.0), (3, 3894.0)]
    m.sent.clear()
    for p in m.open: p.sl = 3894.0
    t.manage(Command("move_sl", price=3886.0))                # would loosen: nothing sent
    t.manage(Command("breakeven"))                            # stop already beyond entry: nothing sent
    t.manage(Command("move_sl", price=389.0))                 # typo
    assert m.sent == []


def test_breakeven_is_queued_until_price_passes_entry():
    m = buys(FakeMT5(bid=3889.5, ask=3889.7)); t, _ = make(m)
    assert all("queued" in line for line in t.manage(Command("breakeven"))) and m.sent == []
    assert t.housekeeping() == [] and m.sent == []
    m.bid, m.ask = 3890.4, 3890.6
    assert len(t.housekeeping()) == 3
    assert [(r["action"], r["sl"]) for r in m.sent] == [(6, 3890.0)] * 3
    m.sent.clear()
    assert t.housekeeping() == [] and m.sent == []            # done, not repeated


def limit(ticket, comment, type=2, price=3890.0, sl=3882.0, tp=3894.0):
    return NS(ticket=ticket, magic=770077, comment=comment, symbol="XAUUSD", type=type, price_open=price,
              sl=sl, tp=tp, volume_current=0.01, time_setup=1_700_000_000, type_time=0, time_expiration=0)


def test_buy_now_turns_waiting_limits_into_market_orders():
    m = FakeMT5(bid=3890.1, ask=3890.3)                       # the channel's "Activ buy now 3890.3"
    m.orders_get = lambda: [limit(1, "tg-77-1"), limit(2, "tg-77-2", tp=3898.0), limit(3, "tg-60-1", type=3)]
    t, _ = make(m)
    t.manage(Command("activate", side="buy"))
    assert [(r["action"], r.get("order"), r.get("price"), r.get("tp"), r.get("comment")) for r in m.sent] == [
        (8, 1, None, None, None), (1, None, 3890.3, 3894.0, "tg-77-1"),
        (8, 2, None, None, None), (1, None, 3890.3, 3898.0, "tg-77-2")]


def test_buy_now_leaves_orders_alone_when_price_is_far_or_nothing_waits():
    m = FakeMT5(bid=3892.9, ask=3893.1)                       # 3.1 above a limit with an 8 point stop
    m.orders_get = lambda: [limit(1, "tg-77-1")]
    t, _ = make(m)
    assert "left pending" in t.manage(Command("activate", side="buy"))[0] and m.sent == []
    t2, m2 = make()
    assert t2.manage(Command("activate", side="sell")) == []  # a pre-alert with nothing waiting: silent


def test_implausible_signal_levels_are_refused():
    t, m = make(FakeMT5(bid=3890.0, ask=3890.2), risk_mode="fixed")
    with pytest.raises(Skip, match="implausibly far"):
        t.execute(Signal("buy", "XAUUSD", 2.0, [3894.0]))     # e.g. a recap misread as "SL 2"
    with pytest.raises(Skip, match="implausibly far"):
        t.execute(Signal("buy", "XAUUSD", 3882.0, [3894.0, 4902.0]))
    assert m.sent == []


def test_daily_loss_ignores_broker_credit():
    m = FakeMT5(balance=200.0)
    m.account_info = lambda: NS(balance=200.0, equity=207.90, credit=47.90, login=1, server="x", currency="EUR")
    t, _ = make(m)                                 # equity 207.90 includes 47.90 credit -> really down 40 of 200
    assert round(t.daily_loss_percent(), 6) == 20.0


def test_status_lines():
    m = buys(FakeMT5(bid=3893.0, ask=3893.2, balance=200.0))
    for p in m.open: p.profit = 1.5
    m.orders_get = lambda: [limit(9, "tg-80-1")]
    m.account_info = lambda: NS(balance=200.0, equity=252.40, credit=47.90, login=345, server="x", currency="EUR")
    t, _ = make(m, max_daily_loss_percent=50.0)
    assert t.status() == ["MT5: connected to the broker",
                          "Account 345: balance 200.00, equity 204.50 EUR",
                          "Bot trades: 3 open (+4.50 EUR), 1 limit order(s) waiting",
                          "Today: +2.2%  (new trades stop at -50%)"]
    m.terminal_info = lambda: None
    assert "NOT reachable" in t.status()[0]
