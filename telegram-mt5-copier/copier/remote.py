"""Commands you send to your own Telegram Saved Messages, and the daily summary."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time
from pathlib import Path

from .report import collect

log = logging.getLogger("copier.remote")

HELP = """Commands (send them to your Saved Messages):
/status - is the bot running, MT5 connection, balance, open trades
/trades - every open position and waiting order (yours marked)
/report - results of the last 30 days (/report 7 for 7 days)
/last - the last lines of the log (/last 30 for more)
/pause - stop taking new signals (open trades stay managed)
/resume - take signals again
/breakeven - move all open positions to breakeven now, your own trades included
/cancel - remove the bot's waiting limit orders
/closeall - close all bot positions and remove waiting orders
/risk - show the size per signal (/risk 0.02 to change, /risk reset)
/trail 0.1 - trailing stop on ALL open trades, 0.1% of the price behind it (/trail off, /trail to show)"""
MAX_TRAIL_PERCENT = 2.0
MAX_RISK_PERCENT = 5.0
LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+ \w+ ")


def _code(text: str) -> str:
    return "```\n" + text[:3800] + "\n```"


class Remote:
    def __init__(self, cfg, trader, state_path: str = "copier.state.json", log_path: str = "copier.log",
                 now: datetime | None = None):
        self.cfg, self.trader = cfg, trader
        self.state_path, self.log_path = Path(state_path), Path(log_path)
        now = now or datetime.now()
        self.started = now
        self.last_message: datetime | None = None
        self.last_signal: tuple[datetime, str] | None = None

        t = cfg.trading
        self._fixed = t.risk_mode == "fixed"
        self._config_size = t.fixed_lot if self._fixed else t.risk_percent
        state = {}
        if self.state_path.exists():
            try:
                state = json.loads(self.state_path.read_text())
            except ValueError:
                log.warning("Ignoring unreadable %s", self.state_path)
        self.paused = bool(state.get("paused"))
        size = state.get("size")
        if isinstance(size, (int, float)) and 0 < size <= self._cap:
            self._set_size(float(size))

        self._config_trail = t.trailing_stop_percent
        trail = state.get("trail")
        if isinstance(trail, (int, float)) and 0 <= trail <= MAX_TRAIL_PERCENT:
            self._set_trail(float(trail))

        self._summary_at = self._parse_time(cfg.telegram.daily_summary_time)
        # Started after today's summary time: the next one is tomorrow.
        self._summary_day = now.date() if self._summary_at and now.time() >= self._summary_at else None

    # ------------------------------------------------------------------ state
    @property
    def _cap(self) -> float:
        return self.cfg.trading.max_lot if self._fixed else MAX_RISK_PERCENT

    @property
    def size(self) -> float:
        return self.cfg.trading.fixed_lot if self._fixed else self.cfg.trading.risk_percent

    def _set_size(self, value: float) -> None:
        if self._fixed:
            self.cfg.trading.fixed_lot = value
        else:
            self.cfg.trading.risk_percent = value

    def _set_trail(self, percent: float | None) -> None:
        """None = back to config.yaml; 0 = off; otherwise trail every open trade at this %."""
        self.cfg.trading.trailing_stop_percent = self._config_trail if percent is None else percent
        self.trader.trail_all = percent is not None and percent > 0
        self._trail = percent

    def _save(self) -> None:
        state = {"paused": self.paused, "size": self.size if self.size != self._config_size else None,
                 "trail": getattr(self, "_trail", None)}
        self.state_path.write_text(json.dumps(state))

    def startup_notes(self) -> list[str]:
        """Things carried over from before a restart, worth a line in the log."""
        notes = []
        if self.paused:
            notes.append("PAUSED by /pause - new signals are not traded until you send /resume")
        if self.size != self._config_size:
            notes.append(f"Size per signal is {self.size:g} (set by /risk; config.yaml says {self._config_size:g})")
        if getattr(self, "_trail", None) is not None:
            notes.append("Trailing stop: " + self._trail_text() + " (set by /trail)")
        return notes

    # ------------------------------------------------------------------ commands
    def handle(self, text: str) -> str | None:
        """Return the reply for a command, or None if the text is not a command."""
        words = (text or "").strip().lower().split()
        if not words or not words[0].startswith("/") or len(text) > 60:
            return None
        cmd, args = words[0], words[1:]
        log.info("Command from Saved Messages: %s", " ".join(words))
        try:
            if cmd == "/help":
                return HELP
            if cmd == "/status":
                return "\n".join(self.status())
            if cmd == "/report":
                days = int(args[0]) if args and args[0].isdigit() and int(args[0]) > 0 else 30
                return _code(collect(self.trader.mt5, self.cfg, days))
            if cmd == "/trades":
                return _code("\n".join(self.trader.trades()))
            if cmd == "/last":
                return _code(self._last(int(args[0]) if args and args[0].isdigit() else 15))
            if cmd == "/pause":
                self.paused = True
                self._save()
                waiting = len(self.trader._mine(self.trader.mt5.orders_get(), None))
                return ("Paused. New signals will not be traded; open trades are still managed."
                        + (f"\n{waiting} limit order(s) are still waiting and can fill - /cancel removes them."
                           if waiting else "") + "\nSend /resume to continue.")
            if cmd == "/resume":
                self.paused = False
                self._save()
                return "Resumed. New signals will be traded again."
            if cmd == "/breakeven":                  # bot trades and your own trades
                self.trader._ensure_connected()
                return "\n".join(self.trader.breakeven(None, include_manual=True))
            if cmd == "/cancel":
                self.trader._ensure_connected()
                return "\n".join(self.trader.cancel_pending(None))
            if cmd == "/closeall":
                return "\n".join(self.trader.manage("close"))
            if cmd == "/trail":
                return self._trail_cmd(args)
            if cmd == "/risk":
                return self._risk(args)
        except Exception as e:                      # always answer, so silence means "not running"
            log.exception("Command failed")
            return f"The bot is running, but {cmd} failed: {e}"
        return f"Unknown command {cmd}. Send /help for the list."

    def status(self) -> list[str]:
        t = self.cfg.trading
        mode = "DRY RUN (no real orders)" if t.dry_run else "LIVE"
        state = "PAUSED - not taking new signals" if self.paused else "running"
        unit = "lots" if self._fixed else "% of balance"
        size = f"Size per signal: {self.size:g} {unit}"
        if self.size != self._config_size:
            size += f"  (set by /risk; config.yaml says {self._config_size:g})"
        return [f"Bot is {state} - {mode}, started {self.started:%d %b %H:%M} ({self._ago(self.started)})",
                "Last channel message: " + (f"{self.last_message:%H:%M} ({self._ago(self.last_message)})"
                                            if self.last_message else "none since start"),
                "Last signal: " + (f"{self.last_signal[0]:%d %b %H:%M} {self.last_signal[1]}"
                                   if self.last_signal else "none since start"),
                size, "Trailing stop: " + self._trail_text(), *self.trader.status()]

    def _trail_text(self) -> str:
        pct = self.cfg.trading.trailing_stop_percent
        if pct <= 0:
            return "off"
        scope = "all open trades" if self.trader.trail_all else (
            "bot trades and yours" if self.cfg.trading.manage_manual_trades else "bot trades")
        return f"{pct:g}% behind the price, on {scope}"

    def _example_distance(self, pct: float) -> str:
        positions = self.trader.mt5.positions_get() or ()
        for p in positions:
            tick = self.trader.mt5.symbol_info_tick(p.symbol)
            if tick is not None and tick.bid:
                return f" (about {tick.bid * pct / 100:.2f} on {p.symbol} at {tick.bid:g})"
        return ""

    def _trail_cmd(self, args: list[str]) -> str:
        if not args:
            return ("Trailing stop: " + self._trail_text() +
                    "\n/trail 0.1 puts it on every open trade, /trail off stops it, "
                    "/trail reset goes back to config.yaml.")
        if args[0] in ("off", "0", "stop"):
            self._set_trail(0.0)
            self._save()
            return "Trailing stop is OFF. Stops already moved stay where they are."
        if args[0] == "reset":
            self._set_trail(None)
            self._save()
            return "Trailing stop is back to config.yaml: " + self._trail_text()
        try:
            pct = float(args[0].replace(",", ".").rstrip("%"))
        except ValueError:
            return "Use /trail <percent>, for example /trail 0.1, or /trail off."
        if not 0 < pct <= MAX_TRAIL_PERCENT:
            return f"Refused: use a value above 0 and at most {MAX_TRAIL_PERCENT:g} (percent of the price)."
        self._set_trail(pct)
        self._save()
        self.trader._ensure_connected()
        open_now = len(self.trader.mt5.positions_get() or ())
        started = self.trader.trail_stops()
        lines = [f"Trailing stop ON: {pct:g}% behind the price{self._example_distance(pct)}, "
                 f"for all {open_now} open trade(s), yours included, and new ones.",
                 "It starts on a trade once that distance locks in a profit and only ever tightens the stop."]
        return "\n".join(lines + started)

    def _risk(self, args: list[str]) -> str:
        unit = "lots per signal" if self._fixed else "% of balance per signal"
        current = self.size
        if not args:
            note = f" (config.yaml says {self._config_size:g})" if current != self._config_size else ""
            return (f"Size: {current:g} {unit}{note}\nChange it with /risk <number>, "
                    "go back to config.yaml with /risk reset.")
        if args[0] == "reset":
            self._set_size(self._config_size)
            self._save()
            return f"Size is back to {self._config_size:g} {unit} from config.yaml."
        try:
            value = float(args[0].replace(",", "."))
        except ValueError:
            return "Use /risk <number>, for example /risk 0.02, or /risk reset."
        if not 0 < value <= self._cap:
            limit = "max_lot in config.yaml" if self._fixed else "the bot's hard limit"
            return f"Refused: the size must be above 0 and at most {self._cap:g} ({limit})."
        if value > current and args[1:] != ["confirm"]:
            return (f"This RAISES the size from {current:g} to {value:g} {unit} - "
                    f"{value / current:.1f}x the money at risk on every signal.\n"
                    f"Send  /risk {value:g} confirm  to apply it.")
        self._set_size(value)
        self._save()
        return f"Size is now {value:g} {unit} for new signals. Open trades are unchanged."

    def _last(self, n: int) -> str:
        if not self.log_path.exists():
            return "No log file yet."
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(n, 40)):]
        return "\n".join(LOG_PREFIX.sub(r"\1 ", line) for line in lines) or "The log is empty."

    def _ago(self, t: datetime) -> str:
        minutes = int((datetime.now() - t).total_seconds() // 60)
        return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"

    # ------------------------------------------------------------------ daily summary
    @staticmethod
    def _parse_time(text: str) -> time | None:
        try:
            hour, minute = str(text).strip().split(":")
            return time(int(hour), int(minute))
        except ValueError:
            return None                              # "" or anything unreadable turns the summary off

    def summary_due(self, now: datetime) -> bool:
        """True once per weekday, at or after daily_summary_time."""
        if not self._summary_at or now.weekday() > 4 or now.time() < self._summary_at:
            return False
        if self._summary_day == now.date():
            return False
        self._summary_day = now.date()
        return True

    def daily_summary(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        state = "PAUSED" if self.paused else ("running, DRY RUN" if self.cfg.trading.dry_run else "running, LIVE")
        body = collect(self.trader.mt5, self.cfg, 1, since=midnight,
                       title=f"Today, {now:%d %b}  (trades opened by the bot)")
        return f"Daily summary - bot is {state}\n" + _code(body)
