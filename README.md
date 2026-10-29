# WEEX Entry Scanner — experimental v1

A read-only WEEX futures scanner. Monitors BTC, ETH, SOL, XRP and DOGE USDT perpetual markets. Uses the official public WEEX REST API, no account connection needed.

## Run

```bash
cd weex-entry-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:8080

## Optional Telegram notifications

Create a Telegram bot with BotFather, start a conversation with it, get your chat ID and set:

```bash
export TELEGRAM_BOT_TOKEN='your_bot_token'
export TELEGRAM_CHAT_ID='your_chat_id'
python app.py
```

Keep the Python process running to receive alerts. For 24/7 operation, deploy to an always-on server or VPS. The browser can be closed while the server runs. Never publish your Telegram bot token.

## Strategy (UNVALIDATED)

On every 30-second scan, pull closed 5m, 15m and 1h candles. Confirm 15m and 1h EMA20 vs EMA50 direction, then check 5m close above previous 20-bar high (LONG) or below previous 20-bar low (SHORT), with volume >= 1.2 times the previous 20-bar average. SL is beyond prior 20-bar opposite extreme or 1.5 ATR, whichever is farther. TP1=2R and TP2=3R. Signal deduplicated per closed 5m candle per pair per process. WAIT means no confirmed signal.

## Important limitations

- This is an experimental pattern detector, NOT a profitable or validated strategy. Backtest and paper trade first.
- Signals can arrive after breakout, and stop-loss can be extremely wide; do not assume every alert is tradable.
- No automatic orders, balance access, live liquidation calculation, or wallet sizing.
- WEEX API may be blocked by geography/network, or change format/rate limits.
- A restart resets alert deduplication; production should persist state, add logging, uptime monitoring, retries, historical evaluation, and Telegram delivery diagnostics.
- Cross margin with 50x/200x leverage can liquidate before a stop-loss executes and can put more than the initial margin at risk.
