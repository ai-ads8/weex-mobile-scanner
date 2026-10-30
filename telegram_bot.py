"""WEEX Moon v6.2 — Telegram webhook + scanner alerts.

No trading orders. Signals are paper setups.

Changes vs v6.1:
- Subscribers are seeded from ALERT_CHAT_IDS / TELEGRAM_CHAT_ID on every start, so
  alerts survive Render restarts even though /tmp SQLite is wiped.
- Alerts are filtered on quality (ALERT_MIN_QUALITY, default A) and on live
  freshness per mode; a 10-minute-old 1m scalp is no longer pushed.
- /scan shows actionable setups sorted by quality, not by recency.
- Duplicate webhook deliveries (same update_id) are ignored; the webhook always
  answers 200 so Telegram does not retry a failing command forever.
- BOT_SENDS_ALERTS=0 lets the scanner service push alerts instead (lower latency).
"""
import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager

import requests
from flask import Flask, abort, jsonify, request

import notifier
import signal_engine as eng

VERSION = "v6.2"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SCANNER = os.getenv("SCANNER_URL", "https://weex-moon-scanner-live.onrender.com/api/status")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://weex-moon-bot-live.onrender.com").rstrip("/")
DB = os.getenv("DB_PATH", "/tmp/weex_moon.sqlite3")
SEED_CHATS = notifier.chat_ids(os.getenv("ALERT_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", ""))
SENDS_ALERTS = os.getenv("BOT_SENDS_ALERTS", "1") != "0"
MIN_QUALITY = os.getenv("ALERT_MIN_QUALITY", "A").upper()
COOLDOWN = {"SCALP": 10 * 60, "INTRADAY": 60 * 60}
POLL_SECONDS = max(10, int(os.getenv("BOT_POLL_SECONDS", "20")))

SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or (
    hashlib.sha256(("weex-secret:" + TOKEN).encode()).hexdigest() if TOKEN else "")
PATH = ("/telegram/" + hashlib.sha256(("weex-path:" + TOKEN).encode()).hexdigest()[:24]
        if TOKEN else "/telegram/unconfigured")

LOCK = threading.RLock()
STATE = {"lastScan": 0, "lastFetch": 0, "error": "Wachten op scanner", "symbols": {}, "setups": [],
         "telegram": "initialiseren", "alertsSent": 0}
RECENT_UPDATES = deque(maxlen=500)
STARTED = False


@contextmanager
def db_open():
    c = sqlite3.connect(DB, timeout=10)
    try:
        c.execute("CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY, alerts INTEGER NOT NULL DEFAULT 1)")
        c.execute("CREATE TABLE IF NOT EXISTS delivered (signal_id TEXT, chat_id INTEGER, sent_at REAL, "
                  "PRIMARY KEY(signal_id, chat_id))")
        c.execute("CREATE TABLE IF NOT EXISTS sent_keys (key TEXT, chat_id INTEGER, sent_at REAL, PRIMARY KEY(key, chat_id))")
        yield c
        c.commit()
    finally:
        c.close()


def seed_subscribers():
    """Env-configured chats are always subscribed (unless they sent /alerts_off
    during this process lifetime)."""
    with db_open() as c:
        for chat in SEED_CHATS:
            c.execute("INSERT OR IGNORE INTO subscribers(chat_id,alerts) VALUES (?,1)", (chat,))


def subscribers():
    with db_open() as c:
        return [r[0] for r in c.execute("SELECT chat_id FROM subscribers WHERE alerts=1")]


def send(chat, text):
    return notifier.send(TOKEN, chat, text)


def scanner_ok(state):
    age = int(time.time() - state["lastScan"]) if state["lastScan"] else None
    return age, bool(age is not None and 0 <= age <= 180 and not state["error"])


def ranked_actionable(setups, now):
    out = []
    for s in setups:
        if not isinstance(s, dict) or not s.get("id") or "entry" not in s or "stop" not in s:
            continue
        f = eng.freshness(s, now, (s.get("quote") or {}).get("lastPrice"))
        out.append((s, f))
    act = [(s, f) for s, f in out if f["actionable"]]
    act.sort(key=lambda sf: (-eng.QUALITY_RANK.get(sf[0].get("quality"), 0), -(sf[0].get("score") or 0)))
    return act, len(out) - len(act)


HELP = ("WEEX Moon commando's:\n/scan – actuele setups (beste eerst)\n/status – scannerstatus\n"
        "/alerts_on /alerts_off – automatische meldingen\n/whoami – je chat-ID\n/test – bot-check")


def process_message(msg):
    chat = (msg.get("chat") or {}).get("id")
    if not chat:
        return
    cmd = (msg.get("text") or "").strip().split(" ")[0].split("@")[0].lower()
    if cmd == "/test":
        send(chat, f"WEEX Moon {VERSION} OK. Webhook reageert. Alerts via bot: {'aan' if SENDS_ALERTS else 'uit (scanner stuurt)'}.")
    elif cmd == "/whoami":
        send(chat, f"Chat-ID: {chat}\nZet dit als ALERT_CHAT_IDS in Render zodat meldingen een herstart overleven.")
    elif cmd in ("/start", "/alerts_on", "/alerts_off"):
        enabled = int(cmd != "/alerts_off")
        with db_open() as c:
            c.execute("INSERT INTO subscribers(chat_id,alerts) VALUES (?,?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET alerts=excluded.alerts", (chat, enabled))
        persist = "" if chat in SEED_CHATS else (
            "\n⚠️ Je chat-ID staat niet in ALERT_CHAT_IDS: na een herstart van de gratis server ben je weer uitgeschreven.")
        send(chat, f"Meldingen {'AAN' if enabled else 'UIT'} (minimaal kwaliteit {MIN_QUALITY}).{persist}\n\n{HELP}")
    elif cmd == "/help":
        send(chat, HELP)
    elif cmd in ("/status", "/scan"):
        with LOCK:
            state = dict(STATE)
        age, ok = scanner_ok(state)
        if cmd == "/status":
            healthy = sum(1 for v in state["symbols"].values() if isinstance(v, dict) and v.get("status") == "OK")
            send(chat, f"Scanner: {'ACTUEEL' if ok else 'NIET ACTUEEL'}\nLaatste scan: {age if age is not None else '?'} sec\n"
                       f"Markten OK: {healthy}\nSetups in lijst: {len(state['setups'])}\n"
                       f"Alerts verstuurd (deze sessie): {state['alertsSent']}\nFout: {state['error'] or 'geen'}")
        elif not ok:
            send(chat, "Geen actuele scannerdata. Gebruik geen signalen.\n" + str(state["error"]))
        else:
            act, stale = ranked_actionable(state["setups"], time.time())
            if not act:
                send(chat, f"Scanner actief. Geen actuele setups." + (f" ({stale} verlopen/te ver gelopen)" if stale else ""))
            else:
                body = "\n\n".join(notifier.signal_text(s, f) for s, f in act[:3])
                send(chat, f"Actuele setups (beste eerst){f', {stale} verlopen niet getoond' if stale else ''}:\n\n{body}")
    else:
        send(chat, HELP)


def notify(payload, now=None):
    now = now or time.time()
    act, _ = ranked_actionable(payload.get("setups", []), now)
    chats = subscribers()
    for s, fresh in act:
        if not eng.passes_alert(s, MIN_QUALITY):
            continue
        key = f"{s.get('symbol')}:{s.get('mode')}:{s.get('side')}"
        for chat in chats:
            with db_open() as c:
                dup = c.execute("SELECT 1 FROM delivered WHERE signal_id=? AND chat_id=?", (s["id"], chat)).fetchone()
                cool = c.execute("SELECT 1 FROM sent_keys WHERE key=? AND chat_id=? AND sent_at>?",
                                 (key, chat, now - COOLDOWN.get(s.get("mode"), 600))).fetchone()
            if dup or cool:
                continue
            if send(chat, notifier.signal_text(s, fresh)):
                with db_open() as c:
                    c.execute("INSERT OR IGNORE INTO delivered VALUES (?,?,?)", (s["id"], chat, now))
                    c.execute("INSERT OR REPLACE INTO sent_keys VALUES (?,?,?)", (key, chat, now))
                with LOCK:
                    STATE["alertsSent"] += 1
    with db_open() as c:
        c.execute("DELETE FROM delivered WHERE sent_at < ?", (now - 14 * 86400,))


def scanner_loop():
    was_healthy, last_alert = None, 0
    while True:
        try:
            r = requests.get(SCANNER, timeout=(5, 20), headers={"Cache-Control": "no-cache"})
            r.raise_for_status()
            payload = r.json()
            stamp = float(payload.get("lastScan") or 0)
            symbols, setups = payload.get("symbols"), payload.get("setups")
            if not stamp or not 0 <= time.time() - stamp <= 180:
                raise RuntimeError("Scannerdata verouderd")
            if not isinstance(symbols, dict) or not isinstance(setups, list):
                raise RuntimeError("Ongeldig scannerformaat")
            if not any(isinstance(v, dict) and v.get("status") == "OK" for v in symbols.values()):
                raise RuntimeError("Geen gezonde markten")
            with LOCK:
                STATE.update(lastScan=stamp, lastFetch=time.time(), error=None, symbols=symbols, setups=setups)
            if was_healthy is False:
                for chat in subscribers():
                    send(chat, "WEEX Moon: scanner weer ACTUEEL.")
            was_healthy = True
            if SENDS_ALERTS:
                notify(payload)
        except Exception as exc:
            with LOCK:
                STATE["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            log.warning("Scanner unavailable: %s", STATE["error"])
            if was_healthy is not False and time.time() - last_alert > 900:
                last_alert = time.time()
                for chat in subscribers():
                    send(chat, "WEEX Moon: scanner NIET ACTUEEL. Gebruik geen oude signalen.")
            was_healthy = False
        time.sleep(POLL_SECONDS)


def configure_webhook():
    while True:
        try:
            notifier.telegram(TOKEN, "setWebhook", {"url": PUBLIC_URL + PATH, "secret_token": SECRET,
                                                     "allowed_updates": ["message"], "drop_pending_updates": False},
                              timeout=(5, 20))
            with LOCK:
                STATE["telegram"] = "webhook ingesteld"
            log.info("Webhook configured")
            return
        except Exception as exc:
            log.warning("Webhook setup failed: %s", type(exc).__name__)
            with LOCK:
                STATE["telegram"] = "webhook instellen mislukt"
            time.sleep(30)


@app.post(PATH)
def webhook():
    if not SECRET or not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), SECRET):
        abort(403)
    update = request.get_json(silent=True) or {}
    uid = update.get("update_id")
    with LOCK:
        if uid is not None and uid in RECENT_UPDATES:
            return jsonify(ok=True, duplicate=True)
        if uid is not None:
            RECENT_UPDATES.append(uid)
    msg = update.get("message")
    if msg:
        try:
            process_message(msg)
        except Exception:
            log.exception("Telegram command error")   # answer 200 anyway: no retry storm
    return jsonify(ok=True)


@app.get("/")
def home():
    return f"WEEX Moon {VERSION} webhook server running"


@app.get("/api/status")
def status():
    with LOCK:
        data = dict(STATE)
    data["scanner_current"] = scanner_ok(data)[1]
    data["version"] = VERSION
    data["botSendsAlerts"] = SENDS_ALERTS
    data["seededChats"] = len(SEED_CHATS)
    data.pop("symbols", None)
    data["setups"] = len(data["setups"])
    return jsonify(data)


def start():
    global STARTED
    if STARTED:
        return
    STARTED = True
    seed_subscribers()
    if os.getenv("BOT_DISABLE_THREADS") == "1":
        return
    threading.Thread(target=scanner_loop, daemon=True, name="scanner").start()
    if TOKEN:
        threading.Thread(target=configure_webhook, daemon=True, name="webhook-setup").start()
    else:
        log.error("TELEGRAM_BOT_TOKEN ontbreekt")


start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
