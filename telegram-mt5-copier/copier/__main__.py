"""Telegram signal -> MetaTrader 5 copier.

    python -m copier --setup          guided first-time setup, writes config.yaml
    python -m copier --list-chats    find the id of your signal group
    python -m copier --parse          paste a message, see how it would be read
    python -m copier                  run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from . import config as config_mod
from .parser import parse_command, parse_signal

log = logging.getLogger("copier")


async def resolve_chats(client, wanted: list) -> set[int]:
    wanted_norm = {str(w).strip().lstrip("@").lower() for w in wanted}
    found, ids = set(), set()
    async for d in client.iter_dialogs():
        keys = {str(d.id), (d.name or "").lower(), (getattr(d.entity, "username", None) or "").lower()}
        hit = (keys & wanted_norm) - {""}
        if hit:
            ids.add(d.id)
            found |= hit
            log.info("Watching: %s (%s)", d.name, d.id)
    if missing := wanted_norm - found:
        raise SystemExit(f"Chats not found in your account: {sorted(missing)} - run --list-chats")
    return ids


async def run(cfg, args) -> None:
    from telethon import TelegramClient, events

    client = TelegramClient(cfg.telegram.session, cfg.telegram.api_id, cfg.telegram.api_hash)
    await client.start()  # asks for phone + login code on the first run only

    if args.list_chats:
        async for d in client.iter_dialogs():
            print(f"{d.id:>16}  {d.name}")
        return

    if not cfg.telegram.chats:
        raise SystemExit("Set telegram.chats in config.yaml (run --list-chats to find the id)")
    chat_ids = await resolve_chats(client, cfg.telegram.chats)

    from .trader import Skip, Trader
    trader = Trader(cfg.trading, cfg.mt5)
    log.info("MT5 connected: %s", trader.connect())
    log.info("Mode: %s", "DRY RUN - no orders will be sent" if cfg.trading.dry_run else "LIVE TRADING")

    done: set[tuple[int, int]] = set()
    traded: dict[tuple[int, int], object] = {}      # signals we acted on, to follow later edits
    lock = asyncio.Lock()

    async def notify(text: str) -> None:
        if cfg.telegram.notify:
            try:
                await client.send_message("me", text)
            except Exception:
                log.exception("Could not send notification")

    async def run_job(title: str, job, quiet_if_empty: bool = False) -> None:
        log.info(title)
        async with lock:
            try:
                report = await asyncio.to_thread(job)
            except Skip as e:
                report = [f"SKIPPED: {e}"]
                log.warning(report[0])
            except Exception as e:  # keep listening whatever happens
                report = [f"ERROR: {e!r}"]
                log.exception("Execution failed")
        if report or not quiet_if_empty:
            await notify(f"{title}\n" + "\n".join(report))

    async def handle(event):
        if event.chat_id not in chat_ids:
            return
        key = (event.chat_id, event.id)
        if key in done:
            # An edit of a message we already acted on: follow changed SL / TP levels.
            old = traded.get(key)
            new = parse_signal(event.raw_text)[0] if old else None
            if new is None or (new.sl, new.tps) == (old.sl, old.tps):
                return
            if (new.side, new.symbol) != (old.side, old.symbol):
                log.warning("Signal %s was edited into a different trade - not followed", event.id)
                return
            traded[key] = new
            await run_job(f"Signal edited: sl {old.sl} -> {new.sl}, tps {old.tps} -> {new.tps}",
                          lambda: trader.update_levels(f"tg-{event.id}", old, new), quiet_if_empty=True)
            return
        age = (datetime.now(timezone.utc) - event.date).total_seconds()
        if age > cfg.telegram.max_signal_age_seconds:
            # e.g. an old signal edited to add "TP1 hit". If this shows for brand new
            # messages, the PC clock is wrong.
            log.info("Ignored message %s: %.0f seconds old", event.id, age)
            return
        sig, why = parse_signal(event.raw_text)
        command = None if sig else parse_command(event.raw_text)
        if sig is None and command is None:
            log.info("Ignored message %s: %s", event.id, why)
            return
        done.add(key)
        if sig:
            traded[key] = sig
            await run_job(str(sig), lambda: trader.execute(sig, tag=f"tg-{event.id}"))
        else:
            # A reply to a signal only affects that signal's trades; otherwise all of the bot's trades.
            replied = getattr(event.message, "reply_to_msg_id", None)
            await run_job(f"Instruction: {command}" + (" (for the replied-to signal)" if replied else ""),
                          lambda: trader.manage(command, f"tg-{replied}" if replied else None))

    async def sweep():
        """Remove pending orders that outlived pending_expiry_minutes."""
        while True:
            await asyncio.sleep(30)
            async with lock:
                try:
                    report = await asyncio.to_thread(trader.expire_pending)
                except Exception:
                    log.exception("Expiry check failed")
                    continue
            if report:
                await notify("\n".join(report))

    sweeper = asyncio.create_task(sweep()) if cfg.trading.pending_expiry_minutes > 0 else None  # noqa: F841

    client.add_event_handler(handle, events.NewMessage())
    client.add_event_handler(handle, events.MessageEdited())  # groups that add SL/TP by editing
    log.info("Listening for signals... Ctrl+C to stop")
    await client.run_until_disconnected()


def main() -> None:
    ap = argparse.ArgumentParser(prog="copier", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--setup", action="store_true", help="interactive first-time setup")
    ap.add_argument("--list-chats", action="store_true", help="print your chats and their ids")
    ap.add_argument("--parse", action="store_true", help="parse a message from stdin and exit")
    args = ap.parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if not (args.parse or args.setup or args.list_chats):
        handlers.append(logging.FileHandler("copier.log", encoding="utf-8"))   # history survives the window
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)
    logging.getLogger("telethon").setLevel(logging.WARNING)

    if args.parse:
        sig, why = parse_signal(sys.stdin.read())
        print(sig if sig else f"Not a signal: {why}")
        return
    try:
        if args.setup:
            from .setup import wizard
            asyncio.run(wizard(args.config))
            return
        asyncio.run(run(config_mod.load(args.config), args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
