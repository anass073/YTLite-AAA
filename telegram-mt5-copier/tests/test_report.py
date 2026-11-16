from types import SimpleNamespace as NS

from copier.report import build_report, skipped_from_log
from datetime import datetime

T = 1_794_844_663          # 16 Nov 2026 15:57:43 UTC


def deal(pos, type, entry, profit=0.0, comment="", magic=770077, time=T, commission=0.0):
    return NS(position_id=pos, type=type, entry=entry, profit=profit, swap=0.0, commission=commission,
              fee=0.0, comment=comment, magic=magic, time=time, symbol="XAUUSD")


def test_report_groups_by_signal_and_counts_money():
    deals = [
        NS(position_id=0, type=2, entry=0, profit=200.0, swap=0.0, commission=0.0, fee=0.0, comment="Deposit",
           magic=0, time=T - 90000, symbol=""),
        # signal 1243: TP1 and TP2 hit, third closed at breakeven  -> win
        deal(1, 0, 0, comment="tg-1243-1"), deal(1, 1, 1, 3.40, "[tp 3884.00]", magic=0, time=T + 60),
        deal(2, 0, 0, comment="tg-1243-2"), deal(2, 1, 1, 6.90, "[tp 3888.00]", magic=0, time=T + 300),
        deal(3, 0, 0, comment="tg-1243-3"), deal(3, 1, 1, 0.0, "[sl 3880.00]", magic=0, time=T + 900),
        # signal 1250: stopped out on all three -> loss
        *[d for i in (4, 5, 6) for d in (deal(i, 1, 0, comment=f"tg-1250-{i - 3}", time=T + 4000),
                                         deal(i, 0, 1, -5.60, "[sl 3881.50]", magic=0, time=T + 5000))],
        # signal 1260: all three at breakeven
        *[d for i in (7, 8, 9) for d in (deal(i, 0, 0, comment=f"tg-1260-{i - 6}", time=T + 8000),
                                         deal(i, 1, 1, -0.05, "[sl 3890.00]", magic=0, time=T + 8600))],
        # signal 1270: one closed, two still open
        deal(10, 0, 0, comment="tg-1270-1", time=T + 9000), deal(10, 1, 1, 3.0, "tg-close", time=T + 9100),
        deal(11, 0, 0, comment="tg-1270-2", time=T + 9000), deal(12, 0, 0, comment="tg-1270-3", time=T + 9000),
        # a manual trade
        deal(50, 0, 0, comment="", magic=0), deal(50, 1, 1, -2.0, comment="", magic=0, time=T + 100),
    ]
    orders = [NS(magic=770077, comment="tg-1280-1", state=2), NS(magic=770077, comment="tg-1280-2", state=6),
              NS(magic=770077, comment="tg-1243-1", state=4)]
    positions = [NS(ticket=11, identifier=11, profit=1.5, swap=0.0), NS(ticket=12, identifier=12, profit=0.5, swap=0.0),
                 NS(ticket=99, identifier=99, profit=-9.0, swap=0.0)]
    balance = 200.0 + 10.30 - 16.80 - 0.15 + 3.0 - 2.0
    text = build_report(deals, orders, positions, balance, "EUR", 770077, {"typo in the signal": 2}, 30)
    print(text)
    assert "16 Nov 15:57 BUY XAUUSD           3   +10.30 EUR" in text
    assert "-16.80 EUR" in text and "-0.15 EUR   (breakeven)" in text and "+3.00 EUR   (2 still open)" in text
    assert "Signals traded:      4" in text
    assert "Finished signals:    3   (won 1, lost 1, breakeven 1;" in text
    assert "Result so far:       -3.65 EUR" in text
    assert "Still open:          2 position(s), currently +2.00 EUR" in text
    assert "Limit orders that never filled:  1 signal(s)" in text
    assert "Signals not traded:  2   (2 typo in the signal)" in text
    assert "Trades not opened by the bot:  -2.00 EUR" in text
    assert "Deposits / withdrawals:        +200.00 EUR" in text
    assert f"Balance:             0.00 -> {balance:.2f} EUR" in text


def test_empty_report():
    text = build_report([], [], [], 200.0, "EUR", 770077)
    assert "No bot trades in this period." in text and "Balance:             200.00 -> 200.00 EUR" in text


def test_skipped_reasons_from_log():
    log = """2026-11-16 13:27:05,779 INFO Ignored message 1235: stop loss 3851.5 is on the wrong side of entry 3875.0 for a sell
2026-11-16 13:33:31,149 INFO Ignored message 1238: no buy/sell keyword
2026-11-17 09:00:00,000 WARNING SKIPPED: price 3861.0 moved too far from signal entry 3856.5 towards the stop
2026-11-17 10:00:00,000 WARNING SKIPPED: max_open_trades (4) reached
2026-10-01 10:00:00,000 WARNING SKIPPED: max_open_trades (4) reached
2026-11-16 15:57:59,433 INFO no open positions to move to breakeven
"""
    assert skipped_from_log(log, datetime(2026, 11, 1)) == {
        "typo in the signal": 1, "price already too far towards the stop": 1, "too many trades open": 1}
