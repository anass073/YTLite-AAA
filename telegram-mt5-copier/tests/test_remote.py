from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from copier.config import Config, Mt5Cfg, TelegramCfg, TradingCfg
from copier.remote import Remote
from copier.trader import Trader
from test_trader import FakeMT5, buys, limit


def make(tmp_path, mt5=None, now=datetime(2026, 11, 17, 9, 0), **trading):
    mt5 = mt5 or FakeMT5(bid=3893.0, ask=3893.2, balance=200.0)
    mt5.history_orders_get = lambda a, b: ()
    cfg = Config(TelegramCfg(), Mt5Cfg(), TradingCfg(**{"dry_run": False, "risk_mode": "fixed", "fixed_lot": 0.03, **trading}))
    r = Remote(cfg, Trader(cfg.trading, cfg.mt5, mt5), tmp_path / "state.json", tmp_path / "copier.log", now=now)
    return r, mt5, cfg


def test_not_a_command(tmp_path):
    r, _, _ = make(tmp_path)
    assert r.handle("note to self: buy milk") is None and r.handle("") is None
    assert "Unknown command /foo" in r.handle("/foo")
    assert "/closeall" in r.handle("/help")


def test_status_and_trades(tmp_path):
    m = buys(FakeMT5(bid=3893.0, ask=3893.2, balance=200.0))
    for p in m.open: p.profit = 1.5
    m.orders_get = lambda: [limit(9, "tg-80-2", tp=3898.0)]
    r, _, _ = make(tmp_path, m)
    status = r.handle("/Status")
    assert "Bot is running - LIVE" in status and "Size per signal: 0.03 lots" in status
    assert "Bot trades: 3 open (+4.50 USD), 1 limit order(s) waiting" in status
    trades = r.handle("/trades")
    assert "BUY 0.01 XAUUSD @ 3890.0  sl 3882.0  tp 3894.0  +1.50 USD  TP1" in trades
    assert "waiting: BUY LIMIT 0.01 XAUUSD @ 3890.0  sl 3882.0  tp 3898.0  TP2" in trades
    assert "No open positions" in make(tmp_path)[0].handle("/trades")


def test_pause_survives_restart(tmp_path):
    r, m, _ = make(tmp_path)
    m.orders_get = lambda: [limit(9, "tg-80-1")]
    reply = r.handle("/pause")
    assert r.paused and "1 limit order(s) are still waiting" in reply
    r2, _, _ = make(tmp_path)                      # restart
    assert r2.paused and "PAUSED" in r2.startup_notes()[0] and "PAUSED" in r2.handle("/status")
    r2.handle("/resume")
    assert not make(tmp_path)[0].paused


def test_trade_commands(tmp_path):
    m = buys(FakeMT5(bid=3893.0, ask=3893.2))
    m.orders_get = lambda: [limit(9, "tg-80-1")]
    r, _, _ = make(tmp_path, m)
    r.handle("/breakeven")
    assert [(q["action"], q["sl"]) for q in m.sent] == [(6, 3890.0)] * 3      # waiting order left alone
    m.sent.clear()
    assert "OK cancel pending order" in r.handle("/cancel")
    assert m.sent == [{"action": 8, "order": 9}]
    m.sent.clear()
    reply = r.handle("/closeall")
    assert [q["action"] for q in m.sent] == [8, 1, 1, 1] and reply.count("OK close") == 3


def test_risk_needs_confirmation_to_raise_and_survives_restart(tmp_path):
    r, _, cfg = make(tmp_path)
    assert "0.03 lots per signal" in r.handle("/risk")
    assert "now 0.02" in r.handle("/risk 0,02") and cfg.trading.fixed_lot == 0.02       # lowering: immediate
    reply = r.handle("/risk 0.3")
    assert "RAISES" in reply and "15.0x" in reply and cfg.trading.fixed_lot == 0.02      # nothing changed yet
    assert "Refused" in r.handle("/risk 3") and "Refused" in r.handle("/risk 0")         # above max_lot 1.0
    assert "Use /risk" in r.handle("/risk lots")
    r.handle("/risk 0.06 confirm")
    assert cfg.trading.fixed_lot == 0.06
    r2, _, cfg2 = make(tmp_path)                                                         # restart keeps it
    assert cfg2.trading.fixed_lot == 0.06 and "config.yaml says 0.03" in r2.startup_notes()[0]
    assert "back to 0.03" in r2.handle("/risk reset") and make(tmp_path)[2].trading.fixed_lot == 0.03


def test_risk_in_percent_mode_is_capped(tmp_path):
    r, _, cfg = make(tmp_path, risk_mode="percent", risk_percent=1.0)
    assert "Refused" in r.handle("/risk 6") and "RAISES" in r.handle("/risk 2")
    r.handle("/risk 0.5")
    assert cfg.trading.risk_percent == 0.5 and cfg.trading.fixed_lot == 0.03


def test_last_lines_of_log(tmp_path):
    r, _, _ = make(tmp_path)
    assert "No log file yet." in r.handle("/last")
    (tmp_path / "copier.log").write_text("\n".join(
        f"2026-11-16 15:57:{i:02d},304 INFO line {i}" for i in range(50)))
    reply = r.handle("/last 3")
    assert reply == "```\n15:57:47 line 47\n15:57:48 line 48\n15:57:49 line 49\n```"
    assert r.handle("/last").count("line") == 15


def test_command_errors_still_get_an_answer(tmp_path):
    r, m, _ = make(tmp_path)
    m.terminal_info = lambda: NS(connected=False, trade_allowed=True)
    assert "running, but /closeall failed" in r.handle("/closeall")


def test_daily_summary_schedule(tmp_path):
    r, m, _ = make(tmp_path, now=datetime(2026, 11, 17, 9, 0))           # Tuesday morning
    assert not r.summary_due(datetime(2026, 11, 17, 21, 59))
    assert r.summary_due(datetime(2026, 11, 17, 22, 0))
    assert not r.summary_due(datetime(2026, 11, 17, 22, 5))              # once a day
    assert r.summary_due(datetime(2026, 11, 18, 22, 0, 3))
    assert not r.summary_due(datetime(2026, 11, 21, 22, 0))              # Saturday
    late, _, _ = make(tmp_path, now=datetime(2026, 11, 17, 23, 0))       # started after the time
    assert not late.summary_due(datetime(2026, 11, 17, 23, 1)) and late.summary_due(datetime(2026, 11, 18, 22, 0))
    text = r.daily_summary(datetime(2026, 11, 17, 22, 0))
    assert text.startswith("Daily summary - bot is running, LIVE") and "Today, 17 Nov" in text
    assert "No bot trades in this period." in text and "Balance:" in text


def test_daily_summary_can_be_turned_off(tmp_path):
    r, _, cfg = make(tmp_path)
    cfg.telegram.daily_summary_time = ""
    r2 = Remote(cfg, r.trader, tmp_path / "s.json", tmp_path / "l.log")
    assert not r2.summary_due(datetime(2026, 11, 17, 23, 0))


def test_breakeven_and_trades_include_your_own_trades(tmp_path):
    m = buys(FakeMT5(bid=3893.0, ask=3893.2))
    own = NS(ticket=50, magic=0, comment="", symbol="XAUUSD", sl=3880.0, tp=3900.0, price_open=3891.0,
             volume=0.05, type=0, profit=1.0)
    m.open.append(own)
    for p in m.open: p.profit = getattr(p, "profit", 0.0)
    r, _, _ = make(tmp_path, m)
    reply = r.handle("/breakeven")
    assert [(q["position"], q["sl"]) for q in m.sent] == [(1, 3890.0), (2, 3890.0), (3, 3890.0), (50, 3891.0)]
    assert reply.count("OK move SL to breakeven") == 4
    assert "BUY 0.05 XAUUSD @ 3891.0  sl 3880.0  tp 3900.0  +1.00 USD  (yours)" in r.handle("/trades")
    assert "Your own trades: 1 open (+1.00 USD)" in r.handle("/status")
    m.sent.clear()
    r.handle("/closeall")                                   # never closes your own trade
    assert 50 not in [q.get("position") for q in m.sent]


def test_own_trade_breakeven_is_queued_until_price_allows(tmp_path):
    m = FakeMT5(bid=3890.5, ask=3890.7)
    m.open = [NS(ticket=50, magic=0, comment="", symbol="XAUUSD", sl=3880.0, tp=3900.0, price_open=3891.0,
                 volume=0.05, type=0, profit=-0.25)]
    r, _, _ = make(tmp_path, m)
    assert "queued" in r.handle("/breakeven") and m.sent == []
    m.bid = 3891.5
    assert "was queued" in r.trader.housekeeping()[0] and m.sent[0]["position"] == 50
