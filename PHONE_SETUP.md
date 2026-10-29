# WEEX mobile scanner

Pairs: BTC, ETH, SOL, XRP, DOGE, INJ, FET, UNI (USDT futures).

## Phone-only usage

The app must run on a remote server to scan when the iPhone is locked. An iPhone browser cannot reliably run a Python server or maintain background scanning. This ZIP is deployment source, not a live website.

1. From your phone, upload the extracted project to a private GitHub repository.
2. In Render, create a Blueprint from the repository (render.yaml). Choose an always-on paid instance (free instances sleep and cannot guarantee alerts).
3. Optionally set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as secret environment variables on Render; never paste secrets into the public website.
4. Visit the Render URL from Safari, use Share > Add to Home Screen.
5. Confirm `/api/signals` contains fresh timestamps and no ERROR statuses before trusting the feed.

No exchange account keys or trading permissions are needed. Market data availability for each pair depends on WEEX API symbol support. This code has not been end-to-end tested against the live API. No trade execution. Strategy is unvalidated.
