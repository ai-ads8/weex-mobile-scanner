
"""WEEX Moon v7.3 — Telegram webhook.

Reads the latest scheduled GitHub Actions scan state.
Does not execute trades or initiate real-time scans.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
from flask import Flask, abort, jsonify, request

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

VERSION = "v7.3"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

PUBLIC_URL = os.getenv(
    "PUBLIC_URL",
    "https://weex-moon-bot-live.onrender.com"
).rstrip("/")

REPO = os.getenv(
    "MOON_GITHUB_REPO",
    "ai-ads8/weex-mobile-scanner"
).strip()

BRANCH = os.getenv(
    "MOON_GITHUB_BRANCH",
    "main"
).strip()

MAX_AGE = int(
    os.getenv("MOON_STATE_MAX_AGE_SECONDS", "900")
)

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

STARTED_AT = int(time.time())


def tg(method, payload):
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN ontbreekt in Render"
        )

    response = requests.post(
        API + "/" + method,
        json=payload,
        timeout=(5, 20)
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            "Telegram: " +
            str(data.get("description", "unknown"))
        )

    return data.get("result")


def send(chat, message):
    for start in range(0, len(message), 3500):
        tg(
            "sendMessage",
            {
                "chat_id": chat,
                "text": message[start:start + 3500]
            }
        )


def state_url():
    if not REPO or "/" not in REPO:
        raise RuntimeError(
            "MOON_GITHUB_REPO ongeldig"
        )

    return (
        f"https://raw.githubusercontent.com/"
        f"{REPO}/{BRANCH}/moon_state.json"
    )


def load_state():
    # Unique query parameter helps bypass cached copies.
    cache_buster = str(time.time_ns())

    response = requests.get(
        state_url(),
        params={"t": cache_buster},
        headers={
            "User-Agent": "WEEX-Moon-Telegram-v7.3",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        },
        timeout=(5, 15)
    )

    if response.status_code == 404:
        raise RuntimeError(
            "moon_state.json niet gevonden op GitHub"
        )

    if response.status_code in (401, 403, 429):
        raise RuntimeError(
            f"GitHub-toegang mislukt: HTTP "
            f"{response.status_code}"
        )

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            "GitHub gaf geen geldige JSON terug"
        )

    if not isinstance(data, dict):
        raise RuntimeError(
            "Ongeldige moon_state.json"
        )

    stamp = int(data.get("lastRun") or 0)

    if not stamp:
        raise RuntimeError(
            "Geen lastRun-tijdstip gevonden"
        )

    age = int(time.time()) - stamp

    if age < -60:
        raise RuntimeError(
            "Scantijd ligt in de toekomst"
        )

    if age > MAX_AGE:
        raise RuntimeError(
            f"Scannerdata niet actueel "
            f"(leeftijd: {age} sec)"
        )

    return data, age


def fmt(value):
    try:
        return f"{float(value):.8g}"
    except (TypeError, ValueError):
        return "—"


def setup_text(setup):
    return (
        f"{setup.get('symbol', '?')} "
        f"{setup.get('side', '?')} | PAPER SETUP\n"
        f"Score: {setup.get('score', '?')}/100 "
        f"(geen winstkans)\n"
        f"Candle entry: {fmt(setup.get('entry'))}\n"
        f"Stop: {fmt(setup.get('stop'))}\n"
        f"TP1: {fmt(setup.get('tp1'))}\n"
        f"Volume: {fmt(setup.get('volume'))}x\n"
        f"Trend: {setup.get('aligned', '?')}/3\n"
        "Geen live entry. Controleer actuele koers, "
        "spread, margin en liquidatierisico."
    )


def process_message(message):
    chat = (message.get("chat") or {}).get("id")

    if chat is None:
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
            chat,
            f"WEEX Moon {VERSION} OK. "
            "Telegram-webhook reageert."
        )

    elif command == "/whoami":
        send(chat, f"Chat-ID: {chat}")

    elif command in ("/start", "/help"):
        send(
            chat,
            f"WEEX Moon {VERSION}\n\n"
            "/scan - laatste GitHub-scan\n"
            "/status - scannerstatus\n"
            "/test - webhook testen\n"
            "/whoami - chat-ID\n"
            "/alerts_on - meldingeninformatie\n"
            "/alerts_off - meldingeninformatie\n\n"
            "Automatische alerts worden rechtstreeks "
            "door GitHub Actions verstuurd."
        )

    elif command in ("/alerts_on", "/alerts_off"):
        send(
            chat,
            "Automatische v7-meldingen worden beheerd "
            "door GitHub Actions.\n"
            "Dit commando wijzigt de meldingen niet."
        )

    elif command in ("/scan", "/status"):
        try:
            state, age = load_state()

        except Exception as exc:
            logging.warning(
                "GitHub state unavailable: %s",
                exc
            )

            send(
                chat,
                f"WEEX Moon {VERSION} | "
                "GEEN ACTUELE SCANNERDATA\n"
                f"{str(exc)[:240]}\n"
                "Gebruik geen oude setups als "
                "actuele entries."
            )
            return

        markets = int(
            state.get("marketsOK") or 0
        )

        setups = (
            state.get("setups")
            if isinstance(state.get("setups"), list)
            else []
        )

        errors = (
            state.get("errors")
            if isinstance(state.get("errors"), list)
            else []
        )

        stamp = datetime.fromtimestamp(
            int(state["lastRun"]),
            tz=timezone.utc
        ).strftime("%d-%m %H:%M UTC")

        if command == "/status":
            send(
                chat,
                f"WEEX Moon {VERSION} | ACTUEEL\n"
                f"Laatste scan: {stamp}\n"
                f"Leeftijd: {age} sec\n"
                f"Markten OK: {markets}/8\n"
                f"Paper-setups: {len(setups)}\n"
                f"Fouten: {len(errors)}\n"
                "Bron: GitHub Actions / moon_state.json\n"
                "Scans zijn gepland, niet realtime."
            )

        else:
            valid = [
                setup
                for setup in setups
                if isinstance(setup, dict)
                and setup.get("id")
            ]

            valid.sort(
                key=lambda s: float(
                    s.get("score") or 0
                ),
                reverse=True
            )

            if not valid:
                send(
                    chat,
                    f"WEEX Moon {VERSION}\n"
                    f"Laatste scan: {age} sec geleden\n"
                    f"Markten: {markets}/8\n"
                    "Geen bevestigde paper-setups.\n"
                    "Dit is geen nieuwe realtime scan."
                )

            else:
                message_text = (
                    f"WEEX Moon {VERSION}\n"
                    f"Laatste scan: {age} sec geleden\n"
                    "Historische paper-setups, "
                    "GEEN live entries:\n\n"
                )

                message_text += "\n\n".join(
                    setup_text(s)
                    for s in valid[:3]
                )

                send(chat, message_text)

    elif command.startswith("/"):
        send(
            chat,
            "Onbekend commando.\n"
            "Gebruik /start /scan /status /test /whoami"
        )


@app.post(PATH)
def webhook():
    supplied = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not SECRET or not hmac.compare_digest(
        supplied, SECRET
    ):
        abort(403)

    update = request.get_json(silent=True) or {}

    if update.get("message"):
        try:
            process_message(update["message"])

        except Exception:
            logging.exception(
                "Telegram update failed"
            )
            return jsonify(ok=False), 500

    return jsonify(ok=True)


@app.get("/")
def home():
    return (
        f"WEEX Moon {VERSION} "
        "Telegram webhook running"
    )


@app.get("/api/health")
def health():
    return jsonify(
        version=VERSION,
        uptimeSeconds=int(time.time()) - STARTED_AT,
        webhookConfigured=bool(TOKEN),
        scanner="GitHub Actions state"
    )


def configure_webhook():
    if not TOKEN:
        logging.error(
            "TELEGRAM_BOT_TOKEN ontbreekt"
        )
        return

    import threading

    def work():
        for attempt in range(5):
            try:
                tg(
                    "setWebhook",
                    {
                        "url": PUBLIC_URL + PATH,
                        "secret_token": SECRET,
                        "allowed_updates": ["message"],
                        "drop_pending_updates": False
                    }
                )

                logging.info(
                    "Telegram webhook configured"
                )
                return

            except Exception:
                logging.exception(
                    "Webhook registration attempt %s failed",
                    attempt + 1
                )

                time.sleep(
                    10 * (attempt + 1)
                )

    threading.Thread(
        target=work,
        daemon=True
    ).start()


configure_webhook()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        threaded=True
    )
