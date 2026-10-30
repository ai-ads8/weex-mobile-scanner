
"""WEEX Moon v6.1 — Telegram webhook + scanner alerts.

No trading orders. Signals are paper setups.
"""

import os
import time
import hmac
import hashlib
import logging
import sqlite3
import threading

import requests
from flask import Flask, request, jsonify, abort

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API = "https://api.telegram.org/bot" + TOKEN

SCANNER = os.getenv(
    "SCANNER_URL",
    "https://weex-moon-scanner-live.onrender.com/api/status",
)

PUBLIC_URL = os.getenv(
    "PUBLIC_URL",
    "https://weex-moon-bot-live.onrender.com",
).rstrip("/")

DB = os.getenv("DB_PATH", "/tmp/weex_moon.sqlite3")

SECRET = (
    hashlib.sha256(
        ("weex-secret:" + TOKEN).encode()
    ).hexdigest()
    if TOKEN else ""
)

PATH = (
    "/telegram/" +
    hashlib.sha256(
        ("weex-path:" + TOKEN).encode()
    ).hexdigest()[:24]
    if TOKEN else "/telegram/unconfigured"
)

LOCK = threading.RLock()
HTTP = requests.Session()

STATE = {
    "lastScan": 0,
    "lastFetch": 0,
    "error": "Wachten op scanner",
    "symbols": {},
    "setups": [],
    "telegram": "initialiseren",
}

STARTED = False


def db_open():
    db = sqlite3.connect(DB, timeout=10)

    db.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            alerts INTEGER NOT NULL DEFAULT 1
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS delivered (
            signal_id TEXT,
            chat_id INTEGER,
            sent_at REAL,
            PRIMARY KEY(signal_id, chat_id)
        )
    """)

    db.commit()
    return db


def telegram(method, data, timeout=(5, 12)):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ontbreekt")

    response = HTTP.post(
        API + "/" + method,
        json=data,
        timeout=timeout,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram API response not OK")

    return result


def send(chat, message):
    text = str(message)

    while text:
        if len(text) <= 3500:
            part, text = text, ""
        else:
            cut = text.rfind("\n\n", 0, 3500)

            if cut < 500:
                cut = 3500

            part = text[:cut]
            text = text[cut:].lstrip()

        try:
            telegram(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": part,
                },
            )

        except Exception as exc:
            logging.warning(
                "Send failed: %s",
                type(exc).__name__,
            )
            return False

    return True


def fmt(value):
    try:
        return (
            f"{float(value):.8f}"
            .rstrip("0")
            .rstrip(".")
        )
    except (ValueError, TypeError):
        return "—"


def signal_text(s):
    return (
        "WEEX MOON | PAPER SETUP\n"
        f"{s.get('symbol', '?')} "
        f"{s.get('side', '?')} | "
        f"{s.get('mode', '?')}\n"
        f"Score: {s.get('score', '—')}/100 | "
        f"{s.get('tier', 'SETUP')} "
        "(geen winstkans)\n"
        f"Candle entry: {fmt(s.get('entry'))}\n"
        f"Stop: {fmt(s.get('stop'))}\n"
        f"TP1: {fmt(s.get('tp1'))}\n"
        f"Volume: {fmt(s.get('volumeRatio'))}x\n"
        f"Stopafstand: {fmt(s.get('riskPct'))}% | "
        f"Trendbevestiging: "
        f"{s.get('alignment', '—')}/3\n"
        "Paper signaal, geen order. "
        "Controleer mark price, margin en risico."
    )


def subscribers():
    with db_open() as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT chat_id FROM subscribers "
                "WHERE alerts=1"
            )
        ]


def process_message(msg):
    chat = msg.get("chat", {}).get("id")

    if not chat:
        return

    cmd = (
        (msg.get("text") or "")
        .strip()
        .split(" ")[0]
        .split("@")[0]
        .lower()
    )

    if cmd == "/test":
        send(
            chat,
            "WEEX Moon v6.1 OK. "
            "Telegram-webhook reageert.",
        )

    elif cmd == "/whoami":
        send(chat, f"Chat-ID: {chat}")

    elif cmd in (
        "/start",
        "/alerts_on",
        "/alerts_off",
    ):
        enabled = int(cmd != "/alerts_off")

        with db_open() as db:
            db.execute(
                "INSERT INTO subscribers(chat_id,alerts) "
                "VALUES (?,?) "
                "ON CONFLICT(chat_id) DO UPDATE "
                "SET alerts=excluded.alerts",
                (chat, enabled),
            )

        send(
            chat,
            "WEEX Moon verbonden. Meldingen "
            + ("AAN" if enabled else "UIT")
            + ".\n/status /scan /test "
            "/alerts_on /alerts_off /whoami",
        )

    elif cmd in ("/status", "/scan"):
        with LOCK:
            state = dict(STATE)

        age = (
            int(time.time() - state["lastScan"])
            if state["lastScan"]
            else None
        )

        ok = (
            age is not None
            and 0 <= age <= 180
            and not state["error"]
        )

        if cmd == "/status":
            healthy = sum(
                1
                for item in state["symbols"].values()
                if isinstance(item, dict)
                and item.get("status") == "OK"
            )

            send(
                chat,
                f"Scanner: "
                f"{'ACTUEEL' if ok else 'NIET ACTUEEL'}\n"
                f"Laatste scan: "
                f"{age if age is not None else '?'} sec\n"
                f"Markten OK: {healthy}\n"
                f"Setups: {len(state['setups'])}\n"
                f"Fout: {state['error'] or 'geen'}",
            )

        elif not ok:
            send(
                chat,
                "Geen actuele scannerdata. "
                "Gebruik deze signalen niet.\n"
                + str(state["error"]),
            )

        elif not state["setups"]:
            send(
                chat,
                "Scanner actief. "
                "Geen bevestigde setups.",
            )

        else:
            recent = sorted(
                state["setups"],
                key=lambda x: float(
                    x.get("createdAt") or 0
                ),
                reverse=True,
            )[:3]

            send(
                chat,
                "Recente paper-setups "
                "(geen live entries):\n\n"
                + "\n\n".join(
                    signal_text(item)
                    for item in recent
                ),
            )


def notify(payload):
    now = time.time()

    signals = [
        signal
        for signal in payload.get("setups", [])
        if isinstance(signal, dict)
        and signal.get("id")
        and signal.get("status") in (
            "PAPER_OPEN",
            "OPEN",
            None,
        )
        and 0 <= now - float(
            signal.get("createdAt") or 0
        ) <= 600
    ]

    groups = {}

    for signal in signals:
        key = (
            signal.get("symbol"),
            signal.get("side"),
        )

        groups.setdefault(key, []).append(signal)

    for group in groups.values():
        group.sort(
            key=lambda x: float(
                x.get("createdAt") or 0
            )
        )

        batches = []

        for signal in group:
            created = float(
                signal.get("createdAt") or 0
            )

            if (
                not batches
                or created - float(
                    batches[-1][0].get("createdAt") or 0
                ) > 300
            ):
                batches.append([signal])
            else:
                batches[-1].append(signal)

        for batch in batches:
            for chat in subscribers():
                ids = [
                    str(signal["id"])
                    for signal in batch
                ]

                with db_open() as db:
                    placeholders = ",".join(
                        "?" for _ in ids
                    )

                    query = (
                        "SELECT signal_id FROM delivered "
                        "WHERE chat_id=? "
                        "AND signal_id IN ("
                        + placeholders + ")"
                    )

                    existing = {
                        row[0]
                        for row in db.execute(
                            query,
                            (chat, *ids),
                        )
                    }

                fresh = [
                    signal
                    for signal in batch
                    if str(signal["id"]) not in existing
                ]

                if not fresh:
                    continue

                best = max(
                    fresh,
                    key=lambda x: float(
                        x.get("score") or 0
                    ),
                )

                message = signal_text(best)

                if len(fresh) > 1:
                    message += (
                        f"\nBevestiging: {len(fresh)} "
                        "setups in dezelfde marktbeweging."
                    )

                if send(chat, message):
                    with db_open() as db:
                        db.executemany(
                            "INSERT OR IGNORE INTO delivered "
                            "VALUES (?,?,?)",
                            [
                                (
                                    str(signal["id"]),
                                    chat,
                                    now,
                                )
                                for signal in fresh
                            ],
                        )

    with db_open() as db:
        db.execute(
            "DELETE FROM delivered "
            "WHERE sent_at < ?",
            (now - 14 * 86400,),
        )


def scanner_loop():
    was_healthy = None
    last_alert = 0

    while True:
        try:
            response = requests.get(
                SCANNER,
                timeout=(5, 15),
                headers={
                    "Cache-Control": "no-cache"
                },
            )

            response.raise_for_status()
            payload = response.json()

            stamp = float(
                payload.get("lastScan") or 0
            )

            symbols = payload.get("symbols")
            setups = payload.get("setups")

            if (
                not stamp
                or not 0 <= time.time() - stamp <= 180
            ):
                raise RuntimeError(
                    "Scannerdata verouderd"
                )

            if (
                not isinstance(symbols, dict)
                or not isinstance(setups, list)
            ):
                raise RuntimeError(
                    "Ongeldig scannerformaat"
                )

            healthy = sum(
                1
                for item in symbols.values()
                if isinstance(item, dict)
                and item.get("status") == "OK"
                and float(
                    item.get("closed1mPrice") or 0
                ) > 0
            )

            if healthy == 0:
                raise RuntimeError(
                    "Geen gezonde markten"
                )

            with LOCK:
                STATE.update(
                    lastScan=stamp,
                    lastFetch=time.time(),
                    error=None,
                    symbols=symbols,
                    setups=setups,
                )

            if was_healthy is False:
                for chat in subscribers():
                    send(
                        chat,
                        "WEEX Moon: scanner weer ACTUEEL.",
                    )

            was_healthy = True
            notify(payload)

        except Exception as exc:
            with LOCK:
                STATE["error"] = (
                    f"{type(exc).__name__}: "
                    f"{str(exc)[:120]}"
                )

            logging.warning(
                "Scanner unavailable: %s",
                type(exc).__name__,
            )

            if (
                was_healthy is not False
                and time.time() - last_alert > 900
            ):
                last_alert = time.time()

                for chat in subscribers():
                    send(
                        chat,
                        "WEEX Moon: scanner NIET ACTUEEL. "
                        "Gebruik geen oude signalen.",
                    )

            was_healthy = False

        time.sleep(30)


def configure_webhook():
    while True:
        try:
            telegram(
                "setWebhook",
                {
                    "url": PUBLIC_URL + PATH,
                    "secret_token": SECRET,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": False,
                },
                timeout=(5, 20),
            )

            with LOCK:
                STATE["telegram"] = (
                    "webhook ingesteld"
                )

            logging.info(
                "Webhook configured"
            )
            return

        except Exception as exc:
            logging.warning(
                "Webhook setup failed: %s",
                type(exc).__name__,
            )

            with LOCK:
                STATE["telegram"] = (
                    "webhook instellen mislukt"
                )

            time.sleep(30)


@app.post(PATH)
def webhook():
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        "",
    )

    if (
        not SECRET
        or not hmac.compare_digest(
            received_secret,
            SECRET,
        )
    ):
        abort(403)

    update = request.get_json(
        silent=True
    ) or {}

    msg = update.get("message")

    if msg:
        try:
            process_message(msg)

        except Exception as exc:
            logging.exception(
                "Telegram command error: %s",
                type(exc).__name__,
            )

            return jsonify(ok=False), 500

    return jsonify(ok=True)


@app.get("/")
def home():
    return (
        "WEEX Moon v6.1 webhook server running"
    )


@app.get("/api/status")
def status():
    with LOCK:
        data = dict(STATE)

    data["scanner_current"] = bool(
        data["lastScan"]
        and 0 <= (
            time.time() - data["lastScan"]
        ) <= 180
        and not data["error"]
    )

    return jsonify(data)


def start():
    global STARTED

    if STARTED:
        return

    STARTED = True

    db_open().close()

    threading.Thread(
        target=scanner_loop,
        daemon=True,
        name="scanner",
    ).start()

    if TOKEN:
        threading.Thread(
            target=configure_webhook,
            daemon=True,
            name="webhook-setup",
        ).start()
    else:
        logging.error(
            "TELEGRAM_BOT_TOKEN ontbreekt"
        )


start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        threaded=True,
    )
