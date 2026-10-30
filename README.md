# WEEX Moon v6.0

Read-only WEEX futures scanner with Telegram alerts, paper tracking and a trade analyzer.
**No orders are ever placed.** Scores and quality grades are heuristics, not win probabilities.

## Files

| File | Role |
|---|---|
| `app.py` | Scanner + dashboard (`/`), Trade Analyzer (`/trade`), API, optional direct Telegram alerts |
| `signal_engine.py` | Pure signal logic (shared by live scanner, backtest and tests) |
| `notifier.py` | Telegram sending and alert text |
| `telegram_bot.py` | Telegram webhook commands (`/scan`, `/status`, ...) and optional alerts |
| `trade_analyzer.py` | Rule-based position check |
| `backtest.py` | Replays the live engine on historical candles (walk-forward, fees, latency) |
| `tests/` | `python -m unittest discover -s tests` |
| `deploy/` | systemd unit + env template for an always-on VM |

## What changed in v6.0 (short)

- Scans start ~3 s after each candle close instead of at arbitrary moments (scalp latency).
- Higher-timeframe candles are cached until a new one closes (≈30 instead of ≈170 WEEX calls/min).
- Every setup gets a quality grade A/B/C from entry checks: chasing, rejection wick, climax candle,
  BTC counter-trend, spread, late/failed price drift. The v5.1 score is kept unchanged for comparison.
- Alerts only for fresh, quality-A setups (configurable), with cooldown and dedup.
- Telegram subscribers are seeded from `ALERT_CHAT_IDS`, so restarts no longer silently unsubscribe you.
- Paper tracking now uses 1m candles: realistic fill after latency, fees, slippage, MFE/MAE, time stop.
- `/api/stats`: results by mode, quality, score bracket, side, pair.

## Environment variables

Scanner: `TELEGRAM_BOT_TOKEN`, `ALERT_CHAT_IDS`, `ALERTS_FROM_SCANNER` (1/0), `ALERT_MIN_QUALITY` (A/B/C),
`APP_ACCESS_TOKEN` (enables `/api/positions`), `SCANNER_DB_PATH`, `SCAN_SYMBOLS`, `SCAN_INTERVAL_SECONDS`,
`SCAN_OFFSET_SECONDS`, `PAPER_FEE_PCT_ROUNDTRIP`, `PAPER_SLIPPAGE_PCT`, `PAPER_LATENCY_SECONDS`.

Bot: `TELEGRAM_BOT_TOKEN`, `ALERT_CHAT_IDS`, `BOT_SENDS_ALERTS` (set `0` when the scanner sends),
`SCANNER_URL`, `PUBLIC_URL`, `WEBHOOK_SECRET` (optional), `DB_PATH`.

Let exactly one service send alerts, otherwise you get each alert twice.

## Limits

- Render free services sleep after 15 min without inbound traffic and lose `/tmp` data. Alerts then stop.
- WEEX REST quotes are snapshots; entry levels are references, not executable prices.
- Nothing here is validated as profitable. Use `/api/stats` and `backtest.py` before trusting any filter.
