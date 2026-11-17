from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class TelegramCfg:
    api_id: int = 0
    api_hash: str = ""
    session: str = "copier"
    chats: list = field(default_factory=list)   # chat ids, @usernames or exact titles
    notify: bool = True                         # send results to your Saved Messages
    daily_summary_time: str = "22:00"           # send the day's result at this time, Mon-Fri; "" = off
    max_signal_age_seconds: int = 120
    edit_grace_seconds: int = 900               # a message we could not use may still be corrected by an edit this long after posting


@dataclass
class Mt5Cfg:
    login: int = 0
    password: str = ""
    server: str = ""
    path: str = ""                              # terminal64.exe, only if auto-detect fails


@dataclass
class TradingCfg:
    dry_run: bool = True                        # validate with the broker but never send
    risk_mode: str = "percent"                  # "percent" | "fixed"
    risk_percent: float = 1.0                   # % of balance lost if SL is hit (whole signal)
    fixed_lot: float = 0.01                     # total lots per signal when risk_mode=fixed
    max_lot: float = 1.0                        # hard cap on total lots per signal
    max_tps: int = 3                            # split into at most this many positions
    max_open_trades: int = 5                    # positions + pending orders opened by this bot
    max_entry_drift: float = 0.5                # skip if price ran towards the SL by > this fraction of the SL distance
    market_entry_tolerance: float = 0.15        # enter at market if price is within this fraction of the SL distance of the zone
    activate_max_distance: float = 0.25         # "buy now": turn a waiting limit into a market order if price is this close (fraction of the SL distance)
    max_daily_loss_percent: float = 20.0        # no new trades once the account is down this much today; 0 = off
    deviation_points: int = 30                  # max slippage on market orders
    entry_mode: str = "edge"                    # where in the entry zone to enter: edge | layered | best
    limit_if_away_from_entry: bool = True       # price not at the entry zone yet -> place a limit order there
    pending_expiry_minutes: int = 120           # cancel unfilled limit/stop orders after this; 0 = never
    breakeven_offset: float = 0.0               # put the breakeven stop this fraction of the stop distance in profit (covers slippage)
    auto_breakeven_at: float = 0.0              # move to breakeven by itself once price is this fraction of the stop distance in profit; 0 = off
    trailing_stop_percent: float = 0.0          # keep the stop this % of the price behind the market once that locks in profit; 0 = off
    breakeven_cancels_pending: bool = False     # "set breakeven" also removes this signal's orders that have not filled
    cancel_closes_positions: bool = False       # "cancel it" also closes already-open trades
    require_tp: bool = False
    magic: int = 770077
    symbol_suffix: str = ""                     # e.g. ".m" or "m" if your broker adds one
    symbol_map: dict = field(default_factory=dict)        # e.g. {XAUUSD: GOLD}
    allowed_symbols: list = field(default_factory=list)   # empty = all


@dataclass
class Config:
    telegram: TelegramCfg
    mt5: Mt5Cfg
    trading: TradingCfg


def _build(cls, data: dict | None):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys in {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cfg = Config(
        _build(TelegramCfg, raw.get("telegram")),
        _build(Mt5Cfg, raw.get("mt5")),
        _build(TradingCfg, raw.get("trading")),
    )
    if cfg.trading.risk_mode not in ("percent", "fixed"):
        raise ValueError("trading.risk_mode must be 'percent' or 'fixed'")
    if cfg.trading.entry_mode not in ("edge", "layered", "best"):
        raise ValueError("trading.entry_mode must be 'edge', 'layered' or 'best'")
    return cfg
