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


# --- the real channel format -------------------------------------------------
def test_channel_format():
    s = ok("1. Sell XAUUSD 3855-3858⚡\n\nSL : 3863\n\nTP1 : 3850\nTP2 : 3845\nTP3 : 3810")
    assert (s.side, s.symbol, s.entry, s.sl, s.tps, s.pending) == (
        "sell", "XAUUSD", 3856.5, 3863.0, [3850.0, 3845.0, 3810.0], False)
    assert (s.entry_low, s.entry_high) == (3855.0, 3858.0)
    single = ok("buy gold 2650 sl 2640 tp 2660")
    assert (single.entry_low, single.entry_high) == (2650.0, 2650.0)


def cmds(text):
    from copier.parser import parse_commands
    assert parse_signal(text)[0] is None
    return [str(c) for c in parse_commands(text)]


@pytest.mark.parametrize("text,expected", [
    # breakeven, including the channel's typos
    ("Set breakeven", ["breakeven"]), ("Set breakeven now", ["breakeven"]), ("Move SL to BE", ["breakeven"]),
    ("Srt breakenven now", ["breakeven"]), ("Ok set breakeven now if you are in", ["breakeven"]),
    ("Srt breakenven and take Tp1 at 3901", ["breakeven", "take TP1 at 3901.0"]),
    ("Set Breakeven and Take TP1 at 3870", ["breakeven", "take TP1 at 3870.0"]),
    ("Sey be now", ["breakeven"]), ("Set be now", ["breakeven"]), ("Srt be", ["breakeven"]),
    # targets and trailing stops
    ("Take tp1 now", ["take TP1 now"]), ("Take tp1 nox", ["take TP1 now"]), ("Takt tp1 now", ["take TP1 now"]),
    ("Mov SL at 3890", ["move SL to 3890.0"]),
    ("Take TP1 at 3868", ["take TP1 at 3868.0"]), ("Take tp1 a 3885", ["take TP1 at 3885.0"]),
    ("Take TP2 at 3947", ["take TP2 at 3947.0"]), ("Take TP3 at 4902", ["take TP3 at 4902.0"]),
    ("120 pips running, Move SL at 3950 (+50 pips)", ["move SL to 3950.0"]),
    ("Move sl at 3894", ["move SL to 3894.0"]),
    ("110 pips running, Move SL at 3886 for last entry", ["move SL to 3886.0"]),
    ("More 160 pips , Move SL at 3890", ["move SL to 3890.0"]),
    # enter now
    ("Sell Gold now", ["enter now (sell)"]), ("Buy gold now", ["enter now (buy)"]),
    ("Buy Now", ["enter now (buy)"]), ("Activ buy now", ["enter now (buy)"]),
    ("Cancel", ["cancel"]), ("Cancel it", ["cancel"]), ("Close it now", ["close"]), ("Close all trades", ["close"]),
    # reports and chatter must do nothing
    ("BE hit", []), ("Breakeven hit", []), ("Set", []), ("be careful now", []), ("Sell to be safe", []),
    ("See you tomorrow, be ready", []), ("TP1 HIT + 50 PIPS ✅", []), ("SL HIT 😢", []), ("in", []), ("In", []),
    ("Sorry , not TP1 , BE hit before 😢", []), ("I Didn’t  move it myself", []),
    ("Be hit , not the time to annonce to take tp1 at 3887", []),
    ("Buu gold 3890", []), ("3890.3", []), ("I m not in for 1 pips !!!", []), ("Are you in ? yes 👍", []),
    ("Close entry 69 and keep 71 and 72", []), ("only keep entry 87/88", []),
    ("Ready for this new week ?", []), ("Market closed for today", []), ("Hello Traders, Have a good day", []),
    ("I hope you’ve understood how I operate and that you’ve taken it on board ::))", []),
])
def test_commands(text, expected):
    assert cmds(text) == expected


def test_recaps_are_not_signals():
    recap = ("DAILY RECAP : +320 PIPS 🚀\n\n• 1. Sell $XAUUSD + 40 PIPS ✅\n• 1. Buy $XAUUSD +90 PIPS ✅\n"
             "• 3. Sell $XAUUSD - 80 PIPS \n\n1 SL, 1 breakeven hits. 🔥 Let’s keep it going tomorrow. 🚀")
    assert cmds(recap) == []


def test_buy_zone_written_high_to_low():
    s = ok("1. Buy XAUUSD 3890-3887⚡️\n\nSL : 3882\n\nTP1 : 3894\nTP2 : 3898\nTP3 : 3930")
    assert (s.side, s.entry_low, s.entry_high, s.sl, s.tps) == ("buy", 3887.0, 3890.0, 3882.0, [3894.0, 3898.0, 3930.0])
