# Telegram → MetaTrader 5 signal copier

Listens to a Telegram signal group through your own account and places the trade in
MetaTrader 5 the moment a signal is posted (typically well under a second).

## Requirements

- **Windows** (PC or VPS) with the MetaTrader 5 terminal installed and logged in. The
  official MT5 Python library only exists for Windows. It must stay running for trades
  to be copied, so a cheap Windows VPS is the usual choice.
- Python 3.10+
- In MT5: enable the **Algo Trading** button in the toolbar.

## Setup

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
updates such as "TP1 hit" or "move SL to BE", are ignored.

## What it does with a signal

- **Sizing** – `risk_percent` of balance for the *whole* signal (or `fixed_lot`),
  capped by `max_lot`, split into one position per take profit (up to `max_tps`).
  If the size is too small to split, it opens one position at TP1.
- **Market signals** execute at the current price. They are skipped if price has
  already moved more than `max_entry_drift` × the stop distance from the posted entry,
  or is past the SL or every TP.
- **Limit/stop signals** become pending orders at the posted entry.
- **Safety** – `max_open_trades`, `allowed_symbols`, a stop loss is always required,
  each message is traded once, and old messages are ignored.

It does **not** manage trades afterwards (break-even, partial closes, "close now"
messages) — SL and TP are set on the order and MT5 handles the rest.

## Tests

```bash
python -m pytest
```

## Risk

This places real orders with real money, automatically, based on what strangers post.
Signal groups frequently lose money. Run on demo first, keep the risk settings small,
and never share `config.yaml` or the `.session` file — the session file is a full login
to your Telegram account.
