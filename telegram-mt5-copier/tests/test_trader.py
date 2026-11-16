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
    def account_info(self): return NS(balance=self.balance, login=1, server="x", currency="USD")
    def positions_get(self): return self.open
    def orders_get(self): return []
    def last_error(self): return (0, "")
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
    (Signal("buy", "XAUUSD", 2640.0, [2660.0], 2630.0), {}, "moved too far"),
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
