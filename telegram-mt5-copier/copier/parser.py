"""Turn a free-text Telegram signal into a structured Signal.

Handles the common layouts, e.g.

    BUY XAUUSD @ 2650.5          SELL GOLD NOW 2650-2653       EUR/USD Buy Limit
    SL 2645                      Stop Loss: 2658               Entry: 1.0850
    TP1 2655  TP2 2660           TP 2645 / TP 2640             SL: 1.0820  TP: 1.0900
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

NUM = r"\d+(?:\.\d+)?"
SEP = r"[:=@\-–)\]]"
NOT_A_PRICE = r"(?![.\d])(?!\s*(?:pips?|points?|pts|%))"

CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "SGD", "HKD", "MXN",
    "ZAR", "TRY", "SEK", "NOK", "PLN", "CNH", "XAU", "XAG", "BTC", "ETH", "LTC",
    "XRP", "SOL",
}
OTHER_SYMBOLS = (
    "US30|NAS100|US100|USTEC|SPX500|US500|GER40|GER30|DE40|UK100|JP225|"
    "USOIL|UKOIL|XTIUSD|XBRUSD"
)
ALIASES = {
    "GOLD": "XAUUSD", "SILVER": "XAGUSD", "NASDAQ": "NAS100", "DOW": "US30",
    "DJ30": "US30", "DAX": "GER40", "BITCOIN": "BTCUSD", "BTC": "BTCUSD",
    "ETHEREUM": "ETHUSD", "ETH": "ETHUSD", "OIL": "USOIL", "WTI": "USOIL",
}

SIDE_RE = re.compile(r"\b(buy|sell|long|short)\b(?:\s+(limit|stop)\b(?!\s*loss))?", re.I)
PAIR_RE = re.compile(r"(?<![A-Z])(?=([A-Z]{3})[/ ]?([A-Z]{3}))")
OTHER_RE = re.compile(rf"\b({OTHER_SYMBOLS})\b")
ALIAS_RE = re.compile(rf"\b({'|'.join(ALIASES)})\b")
CANDIDATE_RE = re.compile(
    rf"(?<![\w.])({NUM})(?![\w%.])(?:\s*(?:-|–|/|to)\s*({NUM})(?![\w%.]))?", re.I
)


def _level_re(names: str) -> re.Pattern:
    return re.compile(
        rf"\b(?:{names})(?:\s*\d{{1,2}}\s*{SEP}+\s*|\s*\d\s+|\s*{SEP}+\s*|\s+)({NUM})\b{NOT_A_PRICE}",
        re.I,
    )


SL_RE = _level_re(r"sl|s/l|stop\s*loss")
TP_RE = _level_re(r"tp|t/p|take\s*profit|target|tgt")


@dataclass
class Signal:
    side: str                      # "buy" | "sell"
    symbol: str                    # normalised, e.g. "XAUUSD"
    sl: float
    tps: list[float] = field(default_factory=list)
    entry: float | None = None     # None = "at market, no reference price given"
    pending: bool = False          # True when the signal says limit / stop
    entry_low: float | None = None   # edges of the entry zone ("3855-3858");
    entry_high: float | None = None  # both equal `entry` for a single price

    def __str__(self) -> str:
        kind = "PENDING" if self.pending else "MARKET"
        return (f"{self.side.upper()} {self.symbol} {kind} entry={self.entry} "
                f"sl={self.sl} tps={self.tps}")


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)          # fancy bold/italic letters -> ASCII
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)      # 2,650.5 -> 2650.5
    return re.sub(r"(?<=\d),(?=\d)", ".", text)          # 1,0850  -> 1.0850


def _find_symbol(upper: str) -> str | None:
    for m in PAIR_RE.finditer(upper):
        if m.group(1) in CURRENCIES and m.group(2) in CURRENCIES and m.group(1) != m.group(2):
            return m.group(1) + m.group(2)
    if m := OTHER_RE.search(upper):
        return m.group(1)
    if m := ALIAS_RE.search(upper):
        return ALIASES[m.group(1)]
    return None


def _blank(text: str, *patterns: re.Pattern) -> str:
    for p in patterns:
        text = p.sub(lambda m: " " * len(m.group(0)), text)
    return text


def parse_signal(text: str) -> tuple[Signal | None, str]:
    """Return (signal, "") or (None, reason it was not treated as a signal)."""
    text = _normalise(text or "")
    side_m = SIDE_RE.search(text)
    if not side_m:
        return None, "no buy/sell keyword"
    side = "buy" if side_m.group(1).lower() in ("buy", "long") else "sell"
    is_buy = side == "buy"

    symbol = _find_symbol(text.upper())
    if not symbol:
        return None, "no recognisable symbol"

    sl_m = SL_RE.search(text)
    if not sl_m:
        return None, "no stop loss"
    sl = float(sl_m.group(1))
    if sl <= 0:
        return None, "invalid stop loss"

    tps = list(dict.fromkeys(float(m.group(1)) for m in TP_RE.finditer(text)))

    # Entry = first plausible price that is not an SL/TP level or part of a symbol name.
    rest = _blank(text, SL_RE, TP_RE, re.compile(OTHER_RE.pattern, re.I))
    plausible = lambda p: abs(p - sl) / sl < 0.2
    entry = low = high = None
    for m in CANDIDATE_RE.finditer(rest):
        a = float(m.group(1))
        if not plausible(a):
            continue
        b = float(m.group(2)) if m.group(2) else None
        if b is None or not plausible(b):
            b = a
        low, high, entry = min(a, b), max(a, b), (a + b) / 2
        break

    pending = bool(side_m.group(2))
    if pending and entry is None:
        return None, "pending order without an entry price"

    if entry is not None and (sl < entry) != is_buy:
        return None, f"stop loss {sl} is on the wrong side of entry {entry} for a {side}"
    ref = entry if entry is not None else sl
    if any((tp > ref) != is_buy for tp in tps):
        return None, f"take profit on the wrong side for a {side}"
    tps.sort(reverse=not is_buy)

    return Signal(side, symbol, sl, tps, entry, pending, low, high), ""


BREAKEVEN_RE = re.compile(
    r"\b(?:set|move|put|secure)\b.{0,25}\b(?:be|b/e|break\s*-?\s*even)\b"
    r"|\bbreak\s*-?\s*even\s+now\b|\bsl\s+(?:to\s+)?(?:be|entry|break\s*-?\s*even)\b", re.I)
CANCEL_RE = re.compile(r"^\W*cancel\b|\bcancel\s+(?:it|this|the|that|order|trade|signal)\b", re.I)
CLOSE_RE = re.compile(
    r"\bclose\s+(?:it|all|now|this|everything|early|manually|(?:the\s+|your\s+)?(?:trade|position|order)s?)\b", re.I)


def parse_command(text: str) -> str | None:
    """Recognise short follow-up instructions: "breakeven", "cancel" or "close"."""
    text = _normalise(text or "").strip()
    if not text or len(text) > 80:      # long messages are commentary, not instructions
        return None
    if BREAKEVEN_RE.search(text):
        return "breakeven"
    if CANCEL_RE.search(text):
        return "cancel"
    if CLOSE_RE.search(text):
        return "close"
    return None
