"""Results report: what the bot's trades actually made, read from the MT5 account history."""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

REASONS = [                     # (text found in the log, how the report words it)
    ("wrong side", "typo in the signal"),
    ("implausibly far", "typo in the signal"),
    ("moved too far", "price already too far towards the stop"),
    ("not at the entry zone", "price not at the entry zone"),
    ("beyond every take profit", "price already past the targets"),
    ("past the stop loss", "price already past the stop"),
    ("max_open_trades", "too many trades open"),
    ("daily loss limit", "daily loss limit reached"),
    ("below the broker minimum", "size below broker minimum"),
]
LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+ (?:SKIPPED: |Ignored message \d+: )(.*)$")


def _net(d) -> float:
    return d.profit + d.swap + d.commission + getattr(d, "fee", 0.0)


def _signal(comment: str) -> str:
    return "-".join(comment.split("-")[:2])            # "tg-1234-2" -> "tg-1234"


def skipped_from_log(log_text: str, since: datetime) -> dict[str, int]:
    """Count the signals the bot saw but did not trade, by reason."""
    counts: dict[str, int] = {}
    for line in log_text.splitlines():
        m = LOG_LINE.match(line)
        if not m or datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") < since:
            continue
        for needle, label in REASONS:
            if needle in m.group(2):
                counts[label] = counts.get(label, 0) + 1
                break
    return counts


def build_report(deals, orders, positions, balance: float, currency: str, magic: int,
                 skipped: dict[str, int] | None = None, days: int = 30) -> str:
    deals = sorted(deals or (), key=lambda d: d.time)
    trades = [d for d in deals if d.type in (0, 1)]                 # buys and sells, not deposits
    # The deal that opens a position carries the bot's tag; later deals share its position id.
    owner = {d.position_id: _signal(d.comment) for d in trades
             if d.entry == 0 and d.magic == magic and d.comment.startswith("tg-")}

    signals: dict[str, dict] = {}
    for d in trades:
        tag = owner.get(d.position_id)
        if tag is None:
            continue
        s = signals.setdefault(tag, {"time": d.time, "side": "BUY" if d.type == 0 else "SELL",
                                     "symbol": d.symbol, "positions": set(), "net": 0.0, "open": 0})
        s["positions"].add(d.position_id)
        s["net"] += _net(d)

    floating, open_count = 0.0, 0
    for p in positions or ():
        tag = owner.get(getattr(p, "identifier", p.ticket))
        if tag is not None:
            signals[tag]["open"] += 1
            open_count += 1
            floating += p.profit + getattr(p, "swap", 0.0)

    unfilled = {_signal(o.comment) for o in orders or ()
                if o.magic == magic and o.comment.startswith("tg-") and o.state in (2, 6)} - set(signals)

    eps = max(0.0025 * balance, 0.01)                               # "breakeven" = within this of zero
    money = lambda x: f"{x:+.2f} {currency}"
    out = [f"Results for the last {days} days  (trades opened by the bot, broker time)", ""]
    if not signals:
        out.append("No bot trades in this period.")
    else:
        out.append(f"{'Signal':<26}{'Positions':>9}   Result")
        for s in signals.values():
            when = datetime.fromtimestamp(s["time"], timezone.utc).strftime("%d %b %H:%M")
            note = f"   ({s['open']} still open)" if s["open"] else (
                "   (breakeven)" if abs(s["net"]) < eps else "")
            out.append(f"{when + ' ' + s['side'] + ' ' + s['symbol']:<26}{len(s['positions']):>9}   "
                       f"{money(s['net'])}{note}")
        closed = [s for s in signals.values() if not s["open"]]
        won = sum(s["net"] >= eps for s in closed)
        lost = sum(s["net"] <= -eps for s in closed)
        out += ["",
                f"Signals traded:      {len(signals)}",
                f"Finished signals:    {len(closed)}   (won {won}, lost {lost}, breakeven {len(closed) - won - lost};"
                f" breakeven = within {eps:.2f} {currency} of zero)",
                f"Result so far:       {money(sum(s['net'] for s in signals.values()))}"
                "   (closed trades; spread, commission and swap included)"]
        if open_count:
            out.append(f"Still open:          {open_count} position(s), currently {money(floating)}")
    out.append("")
    if unfilled:
        out.append(f"Limit orders that never filled:  {len(unfilled)} signal(s)")
    if skipped:
        detail = ", ".join(f"{n} {label}" for label, n in sorted(skipped.items(), key=lambda kv: -kv[1]))
        out.append(f"Signals not traded:  {sum(skipped.values())}   ({detail})")

    other = sum(_net(d) for d in trades if d.position_id not in owner)
    credit = sum(_net(d) for d in deals if d.type == 3)             # broker credit/bonus: not part of the balance
    moved = sum(_net(d) for d in deals if d.type not in (0, 1, 3))
    if abs(other) >= 0.005:
        out.append(f"Trades not opened by the bot:  {money(other)}")
    if abs(moved) >= 0.005:
        out.append(f"Deposits / withdrawals:        {money(moved)}")
    if abs(credit) >= 0.005:
        out.append(f"Broker credit / bonus:         {money(credit)}   (not counted in the balance)")
    start = balance - sum(_net(d) for d in deals if d.type != 3)
    if start < -0.005:      # the history does not add up (e.g. entries the broker books differently)
        out.append(f"Balance now:         {balance:.2f} {currency}")
    else:
        out.append(f"Balance:             {start:.2f} -> {balance:.2f} {currency}")
    return "\n".join(out)


def run(cfg, days: int) -> str:
    from .trader import Trader

    trader = Trader(replace(cfg.trading, dry_run=True), cfg.mt5)   # read-only: no trading rights needed
    trader.connect()
    mt5 = trader.mt5
    try:
        since = datetime.now() - timedelta(days=days)
        until = datetime.now() + timedelta(days=1)
        acc = mt5.account_info()
        log = Path("copier.log")
        skipped = skipped_from_log(log.read_text(encoding="utf-8", errors="replace"), since) if log.exists() else {}
        return build_report(mt5.history_deals_get(since, until), mt5.history_orders_get(since, until),
                            mt5.positions_get(), acc.balance, acc.currency, cfg.trading.magic, skipped, days)
    finally:
        mt5.shutdown()
