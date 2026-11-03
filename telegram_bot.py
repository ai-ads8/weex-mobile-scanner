
"""WEEX Moon v7.4 — Telegram webhook + GitHub wake."""

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

VERSION = "v7.4"

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

GITHUB_WAKE_TOKEN = os.getenv(
    "GITHUB_WAKE_TOKEN", ""
).strip()

ALLOWED_CHAT_ID = os.getenv(
    "TELEGRAM_ALLOWED_CHAT_ID", ""
).strip()

MAX_AGE = int(
    os.getenv("MOON_STATE_MAX_AGE_SECONDS", "900")
)

TOTAL_MARKETS = 15

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
        raise RuntimeError("Telegram-token ontbreekt")

    response = requests.post(
        API + "/" + method,
        json=payload,
        timeout=(5, 20)
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            str(data.get("description", "Telegram error"))
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
    return (
        f"https://raw.githubusercontent.com/"
        f"{REPO}/{BRANCH}/moon_state.json"
    )


def load_state():
    response = requests.get(
        state_url(),
        params={"t": str(time.time_ns())},
        headers={
            "User-Agent": "WEEX-Moon-v7.4",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        },
        timeout=(5, 15)
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        raise RuntimeError("Ongeldige scannerdata")

    stamp = int(data.get("lastRun") or 0)

    if not stamp:
        raise RuntimeError(
            "Geen scantijd beschikbaar"
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
        "(geen winstkans)\n"
        f"Entry: {fmt(setup.get('entry'))}\n"
        f"Stop: {fmt(setup.get('stop'))}\n"
        f"TP1: {fmt(setup.get('tp1'))}\n"
        f"Volume: {fmt(setup.get('volume'))}x\n"
        f"Trend: {setup.get('aligned', '?')}/3\n"
        "Controleer actuele koers, margin "
        "en liquidatierisico."
    )


def wake_scan(chat):
    if not ALLOWED_CHAT_ID:
        send(
            chat,
            "TELEGRAM_ALLOWED_CHAT_ID ontbreekt in Render."
        )
        return

    if str(chat) != ALLOWED_CHAT_ID:
        send(chat, "Geen toestemming voor /wake.")
        return

    if not GITHUB_WAKE_TOKEN:
        send(
            chat,
            "GITHUB_WAKE_TOKEN ontbreekt in Render."
        )
        return

    url = (
        f"https://api.github.com/repos/{REPO}/"
        "actions/workflows/"
        "weex-moon-v7.yml/dispatches"
    )

    try:
        response = requests.post(
            url,
            headers={
                "Authorization":
                    f"Bearer {GITHUB_WAKE_TOKEN}",
                "Accept":
                    "application/vnd.github+json",
                "X-GitHub-Api-Version":
                    "2022-11-28"
            },
            json={
                "ref": BRANCH,
                "inputs": {
                    "test_message": "true"
                }
            },
            timeout=(5, 15)
        )

        if response.status_code == 204:
            send(
                chat,
                "WEEX Moon v7.4\n"
                "Nieuwe scan aangevraagd!\n\n"
                "GitHub Actions controleert "
                "de 15 coins.\n"
                "Wacht op het testbericht.\n"
                "Gebruik daarna /status."
            )

        else:
            logging.error(
                "GitHub dispatch HTTP %s",
                response.status_code
            )

            send(
                chat,
                "Scan starten mislukt.\n"
                f"GitHub HTTP {response.status_code}."
            )

    except requests.RequestException:
        logging.exception("Wake request failed")
        send(
            chat,
            "GitHub tijdelijk niet bereikbaar."
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

    elif command == "/wake":
        wake_scan(chat)

    elif command in ("/start", "/help"):
        send(
            chat,
            f"WEEX Moon {VERSION}\n\n"
            "/wake - nieuwe scan aanvragen\n"
            "/scan - laatste scanresultaten\n"
            "/status - scannerstatus\n"
            "/test - Telegram testen\n"
            "/whoami - chat-ID\n"
            "/alerts_on - meldingeninformatie\n"
            "/alerts_off - meldingeninformatie\n\n"
            "15 coins via GitHub Actions.\n"
            "Geen automatische trade-executie."
        )

    elif command in ("/alerts_on", "/alerts_off"):
        send(
            chat,
            "Automatische alerts worden "
            "beheerd door GitHub Actions.\n"
            "Dit commando verandert de "
            "meldingen momenteel niet."
        )

    elif command in ("/scan", "/status"):
        try:
            state, age = load_state()

        except Exception as exc:
            logging.warning(
                "Scannerdata unavailable: %s",
                exc
            )

            send(
                chat,
                f"WEEX Moon {VERSION} | "
                "GEEN ACTUELE SCANNERDATA\n"
                f"{str(exc)[:240]}\n"
                "Gebruik geen oude setups "
                "als actuele entries."
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
                f"Markten OK: {markets}/{TOTAL_MARKETS}\n"
                f"Paper-setups: {len(setups)}\n"
                f"Fouten: {len(errors)}\n"
                "Bron: GitHub Actions\n"
                "Scans zijn niet realtime."
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
                    f"Markten: {markets}/{TOTAL_MARKETS}\n"
                    "Geen bevestigde paper-setups.\n"
                    "Dit is geen realtime scan."
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
            "Gebruik /start voor alle commando's."
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
        scanner="GitHub Actions"
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
