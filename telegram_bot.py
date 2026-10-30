
"""WEEX Moon Telegram Bot v5.2.

Telegram commands, automatic paper alerts, scanner monitoring,
and recovery of stopped polling threads.

This bot never places trading orders.
"""

import os
import time
import json
import logging
import sqlite3
import threading
import fcntl

import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

SCANNER = os.getenv(
    "SCANNER_URL",
    "https://weex-moon-scanner-live.onrender.com/api/status",
)

DB = os.getenv("DB_PATH", "/tmp/weex_moon.sqlite3")

SCAN_INTERVAL = 30
LOCK = threading.RLock()

STATE = {
    "lastScan": 0,
    "lastFetch": 0,
    "error": "Wachten op scanner",
    "symbols": {},
    "setups": [],
    "telegram": "initialiseren",
}

POLL_HEARTBEAT = 0
POLL_WORKER = None
LAST_HEALTH_STATE = None
LAST_HEALTH_ALERT = 0

SCANNER_SESSION = requests.Session()
POLL_SESSION = requests.Session()
SEND_SESSION = requests.Session()


def connect():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            alerts INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivered (
            signal_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            sent_at REAL NOT NULL,
            PRIMARY KEY(signal_id, chat_id)
        )
    """)

    conn.commit()
    return conn


def send(chat_id, message):
    if not TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN ontbreekt")
        return False

    message = str(message)

    # Telegram allows up to 4096 characters per message.
    chunks = []

    while message:
        if len(message) <= 3500:
            chunks.append(message)
            break

        cut = message.rfind("\n\n", 0, 3500)

        if cut < 500:
            cut = 3500

        chunks.append(message[:cut])
        message = message[cut:].lstrip()

    for chunk in chunks:
        try:
            response = SEND_SESSION.post(
                API + "/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                },
                timeout=(5, 12),
            )

            response.raise_for_status()

            if not response.json().get("ok"):
                return False

        except Exception as exc:
            logging.warning(
                "Telegram send failed: %s",
                type(exc).__name__,
            )
            return False

    return True


def fmt(value):
    try:
        return f"{float(value):.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "—"


def format_signal(signal):
    return (
        "WEEX MOON | PAPER SETUP\n"
        f"{signal.get('symbol', '?')} "
        f"{signal.get('side', '?')} | "
        f"{signal.get('mode', '?')}\n"
        f"Score: {signal.get('score', '—')}/100 | "
        f"{signal.get('tier', 'SETUP')} "
        "(geen winstkans)\n"
        f"Candle entry: {fmt(signal.get('entry'))}\n"
        f"Stop: {fmt(signal.get('stop'))}\n"
        f"TP1: {fmt(signal.get('tp1'))}\n"
        f"Volume: {fmt(signal.get('volumeRatio'))}x\n"
        f"Stopafstand: {fmt(signal.get('riskPct'))}% | "
        f"Trendbevestiging: "
        f"{signal.get('alignment', '—')}/3\n"
        "LET OP: Hoge leverage kan liquidatie "
        "veroorzaken voor de technische stop. "
        "Paper signaal, geen order. "
        "Controleer mark price, margin en risico."
    )


def subscriber_ids():
    with connect() as db:
        return [
            row["chat_id"]
            for row in db.execute(
                "SELECT chat_id FROM subscribers WHERE alerts=1"
            )
        ]


def scanner_read():
    response = SCANNER_SESSION.get(
        SCANNER,
        timeout=(5, 15),
        headers={"Cache-Control": "no-cache"},
    )

    response.raise_for_status()
    payload = response.json()

    timestamp = float(payload.get("lastScan") or 0)

    if not timestamp or not 0 <= time.time() - timestamp <= 180:
        raise RuntimeError("Scannerdata niet actueel")

    symbols = payload.get("symbols")
    setups = payload.get("setups")

    if not isinstance(symbols, dict):
        raise RuntimeError("Ongeldige symbolen")

    if not isinstance(setups, list):
        raise RuntimeError("Ongeldige setups")

    healthy = sum(
        1
        for item in symbols.values()
        if isinstance(item, dict)
        and item.get("status") == "OK"
        and float(item.get("closed1mPrice") or 0) > 0
    )

    if healthy == 0:
        raise RuntimeError("Geen gezonde markten")

    with LOCK:
        STATE.update(
            lastScan=timestamp,
            lastFetch=time.time(),
            error=None,
            symbols=symbols,
            setups=setups,
        )

    return payload


def notify_new_signals(payload):
    now = time.time()

    candidates = [
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

    for signal in candidates:
        key = (
            signal.get("symbol"),
            signal.get("side"),
        )

        groups.setdefault(key, []).append(signal)

    for group in groups.values():
        group.sort(
            key=lambda s: float(
                s.get("createdAt") or 0
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
            for chat_id in subscriber_ids():

                ids = [
                    str(signal["id"])
                    for signal in batch
                ]

                with connect() as db:
                    placeholders = ",".join(
                        "?" for _ in ids
                    )

                    query = (
                        "SELECT signal_id FROM delivered "
                        "WHERE chat_id=? AND signal_id IN ("
                        + placeholders + ")"
                    )

                    existing = {
                        row["signal_id"]
                        for row in db.execute(
                            query,
                            (chat_id, *ids),
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
                    key=lambda s: float(
                        s.get("score") or 0
                    ),
                )

                message = format_signal(best)

                if len(fresh) > 1:
                    message += (
                        f"\nBevestiging: {len(fresh)} "
                        "setups; zelfde marktbeweging."
                    )

                if send(chat_id, message):
                    with connect() as db:
                        db.executemany(
                            "INSERT OR IGNORE INTO delivered "
                            "VALUES (?,?,?)",
                            [
                                (
                                    str(signal["id"]),
                                    chat_id,
                                    now,
                                )
                                for signal in fresh
                            ],
                        )

    with connect() as db:
        db.execute(
            "DELETE FROM delivered WHERE sent_at < ?",
            (now - 14 * 86400,),
        )


def scanner_loop():
    global LAST_HEALTH_STATE
    global LAST_HEALTH_ALERT

    while True:
        try:
            payload = scanner_read()

            if LAST_HEALTH_STATE is False:
                for chat_id in subscriber_ids():
                    send(
                        chat_id,
                        "WEEX Moon: scanner weer ACTUEEL.",
                    )

            LAST_HEALTH_STATE = True

            notify_new_signals(payload)

        except Exception as exc:
            with LOCK:
                STATE["error"] = (
                    f"{type(exc).__name__}: "
                    f"{str(exc)[:130]}"
                )

            logging.warning(
                "Scanner issue: %s",
                type(exc).__name__,
            )

            if (
                LAST_HEALTH_STATE is not False
                and time.time() - LAST_HEALTH_ALERT > 900
            ):
                LAST_HEALTH_ALERT = time.time()

                for chat_id in subscriber_ids():
                    send(
                        chat_id,
                        "WEEX Moon waarschuwing: "
                        "scanner NIET ACTUEEL. "
                        "Vertrouw geen nieuwe setups.",
                    )

            LAST_HEALTH_STATE = False

        time.sleep(SCAN_INTERVAL)


def process_message(message):
    chat_id = message.get(
        "chat", {}
    ).get("id")

    if not chat_id:
        return

    command = (
        (message.get("text") or "")
        .strip()
        .split(" ")[0]
        .split("@")[0]
        .lower()
    )

    if command == "/test":
        send(
            chat_id,
            "WEEX Moon test OK. "
            "Telegram reageert; dit zegt niets "
            "over scannerdata.",
        )

    elif command == "/whoami":
        send(chat_id, f"Chat-ID: {chat_id}")

    elif command in (
        "/start",
        "/alerts_on",
        "/alerts_off",
    ):
        enabled = int(command != "/alerts_off")

        with connect() as db:
            db.execute(
                "INSERT INTO subscribers(chat_id,alerts) "
                "VALUES (?,?) "
                "ON CONFLICT(chat_id) DO UPDATE "
                "SET alerts=excluded.alerts",
                (chat_id, enabled),
            )

        send(
            chat_id,
            "WEEX Moon verbonden. Meldingen "
            + ("AAN" if enabled else "UIT")
            + ".\n/status /scan /test "
            "/alerts_on /alerts_off /whoami",
        )

    elif command == "/status":
        with LOCK:
            state = dict(STATE)

        age = (
            int(time.time() - state["lastScan"])
            if state["lastScan"]
            else None
        )

        healthy = sum(
            1
            for item in state["symbols"].values()
            if isinstance(item, dict)
            and item.get("status") == "OK"
        )

        ok = (
            age is not None
            and 0 <= age <= 180
            and not state["error"]
        )

        send(
            chat_id,
            f"Scanner: "
            f"{'ACTUEEL' if ok else 'NIET ACTUEEL'}\n"
            f"Laatste scan: "
            f"{age if age is not None else '?'} "
            f"seconden geleden\n"
            f"Markten OK: {healthy}\n"
            f"Setups in feed: "
            f"{len(state['setups'])}\n"
            f"Fout: {state['error'] or 'geen'}",
        )

    elif command == "/scan":
        with LOCK:
            state = dict(STATE)

        if (
            not state["lastScan"]
            or time.time() - state["lastScan"] > 180
            or state["error"]
        ):
            send(
                chat_id,
                "Geen actuele scannerdata. "
                "Gebruik deze signalen niet.\n"
                + str(state["error"]),
            )

        elif not state["setups"]:
            send(
                chat_id,
                "Scanner actief. Geen bevestigde "
                "setups op dit moment.",
            )

        else:
            recent = sorted(
                state["setups"],
                key=lambda s: float(
                    s.get("createdAt") or 0
                ),
                reverse=True,
            )[:3]

            send(
                chat_id,
                "Recente paper-setups "
                "(niet per se nieuwe entries):\n\n"
                + "\n\n".join(
                    format_signal(s)
                    for s in recent
                ),
            )


def telegram_loop():
    global POLL_HEARTBEAT

    lock_file = open(
        "/tmp/weex_telegram_poll.lock",
        "w",
    )

    try:
        fcntl.flock(
            lock_file,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        logging.error(
            "Telegram polling already active "
            "in this instance"
        )
        return

    if not TOKEN:
        logging.error(
            "TELEGRAM_BOT_TOKEN ontbreekt"
        )
        return

    offset = None
    conflicts = 0

    while True:
        POLL_HEARTBEAT = time.time()

        try:
            args = {
                "timeout": 15,
                "allowed_updates": json.dumps(
                    ["message"]
                ),
            }

            if offset is not None:
                args["offset"] = offset

            response = POLL_SESSION.get(
                API + "/getUpdates",
                params=args,
                timeout=(5, 23),
            )

            response.raise_for_status()

            payload = response.json()

            if not payload.get("ok"):
                raise RuntimeError(
                    "Telegram returned not ok"
                )

            POLL_HEARTBEAT = time.time()

            for update in payload.get(
                "result", []
            ):
                offset = update["update_id"] + 1

                if "message" in update:
                    try:
                        process_message(
                            update["message"]
                        )
                    except Exception as exc:
                        logging.warning(
                            "Command handler error: %s",
                            type(exc).__name__,
                        )

            conflicts = 0

            with LOCK:
                STATE["telegram"] = (
                    "polling actief"
                )

        except Exception as exc:
            status = getattr(
                getattr(
                    exc, "response", None
                ),
                "status_code",
                None,
            )

            if status == 409:
                conflicts += 1
                delay = min(
                    60, 10 * conflicts
                )
            else:
                conflicts = 0
                delay = 5

            with LOCK:
                STATE["telegram"] = (
                    f"polling error "
                    f"{type(exc).__name__} "
                    f"HTTP {status or '?'}"
                )

            logging.warning(
                "Telegram poll error: %s "
                "HTTP %s",
                type(exc).__name__,
                status or "?",
            )

            time.sleep(delay)


def watchdog_loop():
    global POLL_WORKER

    while True:
        time.sleep(15)

        if (
            POLL_WORKER
            and not POLL_WORKER.is_alive()
        ):
            logging.error(
                "Telegram worker stopped; restarting"
            )

            POLL_WORKER = threading.Thread(
                target=telegram_loop,
                daemon=True,
                name="telegram-poll",
            )

            POLL_WORKER.start()

        elif (
            POLL_HEARTBEAT
            and time.time() - POLL_HEARTBEAT > 100
        ):
            logging.error(
                "Telegram polling heartbeat stale"
            )


@app.get("/")
def index():
    return (
        "WEEX Moon Telegram bridge v5.2 running"
    )


@app.get("/api/status")
def status():
    with LOCK:
        data = dict(STATE)

    data["scanner_current"] = bool(
        data["lastScan"]
        and 0 <= time.time() - data["lastScan"] <= 180
        and not data["error"]
    )

    data["telegram_poll_age_seconds"] = (
        round(
            time.time() - POLL_HEARTBEAT,
            1,
        )
        if POLL_HEARTBEAT
        else None
    )

    data["telegram_worker_alive"] = bool(
        POLL_WORKER
        and POLL_WORKER.is_alive()
    )

    return jsonify(data)


def start_workers():
    global POLL_WORKER

    connect().close()

    threading.Thread(
        target=scanner_loop,
        daemon=True,
        name="scanner",
    ).start()

    POLL_WORKER = threading.Thread(
        target=telegram_loop,
        daemon=True,
        name="telegram-poll",
    )

    POLL_WORKER.start()

    threading.Thread(
        target=watchdog_loop,
        daemon=True,
        name="watchdog",
    ).start()


start_workers()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        threaded=True,
    )
