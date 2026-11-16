"""Interactive first-time setup: asks a few questions and writes config.yaml."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import yaml

from .config import Config, Mt5Cfg, TelegramCfg, TradingCfg


def ask(prompt: str, default=None, cast=str):
    hint = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{hint}: ").strip()
        if not raw and default is not None:
            return default
        try:
            if raw:
                return cast(raw)
        except ValueError:
            pass
        print("  Please enter a valid value.")


def detect_suffix(names: list[str]) -> str:
    """Work out the broker's symbol suffix (e.g. 'm' in EURUSDm) from its symbol list."""
    for base in ("EURUSD", "XAUUSD", "GBPUSD"):
        if base in names:
            return ""
        matches = sorted((n for n in names if n.startswith(base)), key=len)
        if matches:
            return matches[0][len(base):]
    return ""


def check_mt5() -> str:
    """Connect to the running terminal, report the account, return the symbol suffix."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("  ! MetaTrader5 library is not installed (it only works on Windows).")
        return ""
    if not mt5.initialize():
        print(f"  ! Could not connect to MT5: {mt5.last_error()}")
        print("    Open the MT5 terminal, log into your account, then run setup again.")
        return ""
    try:
        acc, term = mt5.account_info(), mt5.terminal_info()
        if acc is None:
            print("  ! MT5 is open but not logged into an account.")
            return ""
        print(f"  Connected: account {acc.login} on {acc.server}, balance {acc.balance:.2f} {acc.currency}")
        if term is not None and not term.trade_allowed:
            print("  ! Algo Trading is OFF - click the 'Algo Trading' button in the MT5 toolbar.")
        suffix = detect_suffix([s.name for s in mt5.symbols_get() or ()])
        if suffix:
            print(f"  Your broker adds '{suffix}' to symbol names - configured automatically.")
        return suffix
    finally:
        mt5.shutdown()


async def wizard(path: str) -> None:
    from telethon import TelegramClient

    target = Path(path)
    if target.exists() and ask(f"{path} already exists. Overwrite? (y/n)", "n").lower() != "y":
        return

    print("\n== 1/3 Telegram ==")
    print("Open https://my.telegram.org, log in, click 'API development tools',")
    print("create an app (any name) and copy the two values shown.\n")
    tg = TelegramCfg(api_id=ask("api_id", cast=int), api_hash=ask("api_hash"))

    print("\nLogging into Telegram - enter your phone number, then the code Telegram sends you.")
    client = TelegramClient(tg.session, tg.api_id, tg.api_hash)
    await client.start()
    groups = [d async for d in client.iter_dialogs(limit=200) if d.is_group or d.is_channel]
    await client.disconnect()
    if not groups:
        raise SystemExit("No groups or channels found on this Telegram account.")
    print("\nYour groups and channels:")
    for i, d in enumerate(groups, 1):
        print(f"  {i:>3}. {d.name}")
    while True:
        try:
            picks = [int(p) for p in ask("\nNumber of the signal group (several: 3,7)").split(",")]
            tg.chats = [groups[p - 1].id for p in picks if p >= 1]
            if len(tg.chats) == len(picks):
                break
        except (ValueError, IndexError):
            pass
        print("  Please enter numbers from the list.")

    print("\n== 2/3 MetaTrader 5 ==")
    print("The bot trades on whichever account is logged into your MT5 terminal.")
    input("Open MT5, log into the account you want to use (demo recommended), then press Enter...")
    trading = TradingCfg(symbol_suffix=check_mt5())

    print("\n== 3/3 Risk ==")
    trading.risk_percent = ask("Percent of balance to risk per signal", 1.0, float)
    trading.max_open_trades = ask("Maximum trades open at once", 5, int)

    cfg = Config(tg, Mt5Cfg(), trading)
    target.write_text(yaml.safe_dump(asdict(cfg), sort_keys=False))
    print(f"\nSaved {path}. The bot starts in DRY RUN mode: it shows what it would trade")
    print("but sends nothing. When you are happy, open config.yaml and set dry_run: false.")
    print("Start the bot with run.bat (or: python -m copier).")
