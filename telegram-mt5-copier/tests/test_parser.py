import pytest

from copier.parser import parse_signal


def ok(text):
    sig, why = parse_signal(text)
    assert sig is not None, why
    return sig


def test_basic_market_buy():
    s = ok("BUY XAUUSD @ 2650.5\nSL 2645\nTP1 2655\nTP2 2660")
    assert (s.side, s.symbol, s.entry, s.sl, s.tps, s.pending) == (
        "buy", "XAUUSD", 2650.5, 2645.0, [2655.0, 2660.0], False)


def test_gold_alias_sell_zone():
    s = ok("🔥 SELL GOLD NOW 2650-2653\n\nStop Loss: 2658\nTP 2645\nTP 2640\nTP: open")
    assert (s.side, s.symbol, s.entry, s.sl) == ("sell", "XAUUSD", 2651.5, 2658.0)
    assert s.tps == [2645.0, 2640.0]


def test_pending_limit_with_slash_pair():
    s = ok("EUR/USD Buy Limit\nEntry: 1.0850\nSL: 1.0820\nTP: 1.0900")
    assert (s.symbol, s.pending, s.entry, s.sl, s.tps) == ("EURUSD", True, 1.085, 1.082, [1.09])


def test_numbered_levels_and_separators():
    s = ok("Sell GBPJPY 191.20\nSL : 191.80\nTP 1 : 190.80\nTP2 - 190.40\nTP3 = 189.90")
    assert s.tps == [190.8, 190.4, 189.9] and s.entry == 191.2


def test_index_symbol_digits_are_not_entry():
    s = ok("BUY US30\nSL 42100\nTP 42600")
    assert (s.symbol, s.entry, s.sl, s.tps) == ("US30", None, 42100.0, [42600.0])


def test_noise_numbers_are_not_entry():
    s = ok("Signal #12 - risk 1%\nBUY NAS100 at 20150\nSL 20050\nTP1 20250")
    assert s.entry == 20150.0


def test_unicode_bold_and_comma_formats():
    s = ok("𝗕𝗨𝗬 𝗫𝗔𝗨𝗨𝗦𝗗 2,650.50\nSL 2,645\nTP 2,660")
    assert (s.side, s.entry, s.sl, s.tps) == ("buy", 2650.5, 2645.0, [2660.0])
    assert ok("buy eurusd 1,0850 sl 1,0820 tp 1,0900").entry == 1.085


def test_sell_stop_is_not_stop_loss():
    s = ok("SELL STOP BTCUSD 64000\nSL 65000\nTP 62000")
    assert (s.pending, s.entry, s.sl) == (True, 64000.0, 65000.0)


def test_tps_sorted_nearest_first():
    assert ok("buy gold 2650 sl 2640 tp 2670 tp 2660").tps == [2660.0, 2670.0]


@pytest.mark.parametrize("text,reason", [
    ("TP1 hit ✅ +50 pips", "no buy/sell"),
    ("Good morning traders, gold looks bullish today", "no buy/sell"),
    ("BUY GOLD NOW", "no stop loss"),
    ("Sell gold closed at 2650, great trade", "no stop loss"),
    ("BUY GOLD 2650 SL 30 pips TP 60 pips", "no stop loss"),
    ("Move SL to 2650 on the buy", "no recognisable symbol"),
    ("BUY XAUUSD 2650 SL 2660 TP 2670", "wrong side"),
    ("SELL XAUUSD 2650 SL 2660 TP 2670", "wrong side"),
    ("BUY LIMIT EURUSD SL 1.0800", "pending order without"),
])
def test_rejected(text, reason):
    sig, why = parse_signal(text)
    assert sig is None and reason in why, (sig, why)
