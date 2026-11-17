# Telegram → MetaTrader 5 signal copier

Listens to a Telegram signal group through your own account and places the trade in
MetaTrader 5 the moment a signal is posted (typically well under a second).

## Requirements

- **Windows** (PC or VPS) with the MetaTrader 5 terminal installed and logged in. The
  official MT5 Python library only exists for Windows. It must stay running for trades
  to be copied, so a cheap Windows VPS is the usual choice.
- Python 3.10+
- In MT5: enable the **Algo Trading** button in the toolbar.

## Quick setup (Windows)

1. Install [Python](https://www.python.org/downloads/) (tick *Add python.exe to PATH*).
2. Open MT5 and log into the account you want to trade (demo recommended).
3. Double-click **`setup.bat`**. It installs everything, then asks for your Telegram
   API keys, logs you into Telegram, lets you pick the signal group from a list,
   checks the MT5 connection and writes `config.yaml`.
4. Double-click **`run.bat`** to start the bot.

## Manual setup

```bash
pip install -r requirements.txt
```

```bash
copy config.example.yaml config.yaml
```

1. Get `api_id` / `api_hash` at <https://my.telegram.org> → *API development tools*,
   and put them in `config.yaml` with your MT5 login.
2. Find your signal group's id (first run asks for your phone number and login code):

   ```bash
   python -m copier --list-chats
   ```

3. Put that id in `telegram.chats`, then start it:

   ```bash
   python -m copier
   ```

It starts in **dry-run mode**: every signal is parsed, sized and validated by your
broker, but nothing is sent. Results are logged and sent to your Telegram *Saved
Messages*. When the dry-run output matches what you would have done by hand, set
`trading.dry_run: false` — ideally on a demo account first.

## Check how your group's messages are read

Paste a real signal, then press Ctrl+Z and Enter:

```bash
python -m copier --parse
```

Understood out of the box: `BUY/SELL/LONG/SHORT`, `limit`/`stop` pending orders, forex
pairs (`EURUSD`, `EUR/USD`), metals, crypto, major indices, aliases like `GOLD`,
entry prices or zones (`2650-2653`), `SL` / `Stop Loss`, and any number of
`TP` / `TP1` / `Take Profit` / `Target` levels. Messages without a stop loss, and
status updates such as "TP1 hit", are ignored.

## What it does with a signal

- **Sizing** – `risk_percent` of balance for the *whole* signal (or `fixed_lot`),
  capped by `max_lot`, split into one position per take profit (up to `max_tps`).
  If the size is too small to split, it opens one position at TP1.
- **Entry price or zone** (`Sell XAUUSD 3855-3858`): if price is in or right next to
  the zone, it enters at market. If price has not come back to the zone yet, it places
  a limit order at the edge of the zone price will touch first (3855 for that sell),
  so it fills whenever the channel's own entry does. If price has already run towards
  the stop loss by more than `max_entry_drift` × the stop distance, it skips.
- **Limit/stop signals** become pending orders at the posted entry.
- **Follow-up messages** are acted on (spelling mistakes like "Srt breakenven" included):

  | Message | What the bot does |
  |---|---|
  | `Set breakeven now` / `Move SL to BE` | Stop loss moved to the entry price. If price has not passed the entry yet, it is queued and applied the moment it has. Orders that have not filled yet stay waiting (`breakeven_cancels_pending: true` removes them instead). |
  | `Move SL at 3950` | Stop moved to that price on the open positions. Only ever tightens a stop. |
  | `Take TP1 now` | Closes the TP1 position at market. |
  | `Take TP2 at 3947` | Moves that position's target; closes it if price is already there. |
  | `Buy now` / `Sell gold now` / `Activ buy now` | If the bot is still waiting with limit orders, and price is within `activate_max_distance` of them, they become market orders. Otherwise nothing happens. |
  | `Cancel` / `Cancel it` | Pending orders removed (open trades too if `cancel_closes_positions`). |
  | `Close it` / `Close all` | Pending orders removed and open trades closed. |

  If the message is a reply to a signal, only that signal's trades are touched;
  otherwise all trades opened by the bot. Trades you open by hand are never touched.
  A price that is more than 5% away from the market ("Take TP3 at 4902") is treated as
  a typo and ignored. Reports such as "TP1 hit", "BE hit" or "in" do nothing, and
  entry-specific instructions ("Close entry 69 and keep 71") are not understood.
- **Edited signals** – if the channel edits the SL or TPs of a signal the bot traded,
  its positions and pending orders are moved to the new levels (a stop already at
  breakeven is left alone). Only works for signals traded since the bot last started.
- **Daily loss limit** – once the account is down `max_daily_loss_percent` since
  midnight (closed and open trades, including ones you placed by hand), new signals
  are skipped until the next day. Open trades are left alone.
- **Unfilled orders expire** after `pending_expiry_minutes` (default 120), so a limit
  order the market never reached cannot fill hours later.
- **Keeps itself running** – reconnects to MT5 if the terminal restarts, `run.bat`
  restarts the bot if it stops, and everything is written to `copier.log`.
- **Safety** – `max_open_trades`, `allowed_symbols`, a stop loss is always required,
  each message is traded once, and old messages are ignored.

## Check on it from your phone

Send one of these to your own **Saved Messages** in Telegram and the bot answers there:

- `/status` – whether it is running, live or dry run, the MT5 connection, your balance,
  open trades and waiting orders, today's result, and when the channel last posted
- `/report` – the results report below for the last 30 days (`/report 7` for 7 days)
- `/help` – the list of commands

No answer within a few seconds means the bot is not running.

## Results report

Double-click **`report.bat`** (or run `python -m copier --report --days 30`) to see what
the bot's trades actually made, read from your MT5 account history: result per signal,
won / lost / breakeven counts, the total after spread and commission, limit orders that
never filled, and signals that were skipped and why. MT5 must be open.

## Tests

```bash
python -m pytest
```

## Risk

This places real orders with real money, automatically, based on what strangers post.
Signal groups frequently lose money. Run on demo first, keep the risk settings small,
and never share `config.yaml` or the `.session` file — the session file is a full login
to your Telegram account.
