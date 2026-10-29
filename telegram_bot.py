
import os
import time
import threading
import requests

from flask import Flask, jsonify

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"

SCANNER_URL = (
    "https://weex-mobile-scanner-v5.onrender.com/api/status"
)

subscribers = set()
alerts_enabled = set()
last_update_id = 0
seen_signals = set()
last_scan = None
last_error = None


def send_message(chat_id, message):
    if not TOKEN:
        return False

    response = requests.post(
        f"{API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("ok", False)


def get_scanner():
    response = requests.get(
        SCANNER_URL,
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()

    if not isinstance(data.get("rows"), list):
        raise ValueError("Scannerdata ontbreekt")

    return data


def format_scan(data):
    rows = [
        row for row in data.get("rows", [])
        if row.get("score") is not None
    ]

    rows.sort(
        key=lambda row: row.get("score", 0),
        reverse=True,
    )

    if not rows:
        return "Geen bruikbare scannerdata beschikbaar."

    lines = ["WEEX Moon - beste setups"]

    for row in rows[:5]:
        lines.append(
            f"{row.get('symbol', '?')} "
            f"{row.get('side', '?')} "
            f"{row.get('mode', '?')} | "
            f"{row.get('score', 0)}/100 | "
            f"{row.get('status', 'WACHT')}"
        )

    return "\n".join(lines)


def handle_message(message):
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    if not chat_id:
        return

    command = (
        message.get("text", "")
        .split(" ")[0]
        .split("@")[0]
        .lower()
    )

    if command == "/start":
        subscribers.add(chat_id)
        alerts_enabled.add(chat_id)

        send_message(
            chat_id,
            "WEEX Moon is verbonden!\n\n"
            "Je ontvangt entry-alerts zodra "
            "de scanner een setup bevestigt.\n\n"
            "/status - verbinding controleren\n"
            "/scan - beste setups bekijken\n"
            "/alerts_on - meldingen aan\n"
            "/alerts_off - meldingen uit\n\n"
            "Let op: signalen zijn experimenteel. "
            "Controleer altijd zelf je risico."
        )

    elif command == "/status":
        try:
            data = get_scanner()
            age = time.time() - float(
                data.get("updated") or 0
            )

            if age > 180:
                status = "Scannerdata is verouderd"
            else:
                status = "Scanner bereikbaar"

            send_message(
                chat_id,
                f"WEEX Moon status\n"
                f"{status}\n"
                f"Laatste update: {int(age)} sec geleden\n"
                f"Alerts: "
                f"{'AAN' if chat_id in alerts_enabled else 'UIT'}"
            )

        except Exception as exc:
            send_message(
                chat_id,
                f"Scanner niet bereikbaar: {str(exc)[:150]}"
            )

    elif command == "/scan":
        try:
            send_message(
                chat_id,
                format_scan(get_scanner()),
            )
        except Exception as exc:
            send_message(
                chat_id,
                f"Scan mislukt: {str(exc)[:150]}"
            )

    elif command == "/alerts_on":
        subscribers.add(chat_id)
        alerts_enabled.add(chat_id)
        send_message(chat_id, "Entry-alerts staan AAN.")

    elif command == "/alerts_off":
        alerts_enabled.discard(chat_id)
        send_message(chat_id, "Entry-alerts staan UIT.")


def telegram_loop():
    global last_update_id
    global last_error

    while True:
        try:
            if not TOKEN:
                last_error = "TELEGRAM_BOT_TOKEN ontbreekt"
                time.sleep(10)
                continue

            response = requests.get(
                f"{API}/getUpdates",
                params={
                    "offset": last_update_id + 1,
                    "timeout": 20,
                },
                timeout=30,
            )
            response.raise_for_status()

            payload = response.json()

            if not payload.get("ok"):
                raise RuntimeError(str(payload))

            for update in payload.get("result", []):
                last_update_id = max(
                    last_update_id,
                    update["update_id"],
                )

                message = update.get("message")

                if message:
                    handle_message(message)

            last_error = None

        except Exception as exc:
            last_error = str(exc)[:200]
            print("Telegram error:", last_error, flush=True)
            time.sleep(5)


def alert_loop():
    global last_scan

    while True:
        try:
            if not alerts_enabled:
                time.sleep(15)
                continue

            data = get_scanner()
            scan_time = float(data.get("updated") or 0)

            if scan_time <= 0 or time.time() - scan_time > 180:
                time.sleep(15)
                continue

            # Alleen nieuwe scans verwerken.
            if last_scan == scan_time:
                time.sleep(15)
                continue

            last_scan = scan_time

            for row in data.get("rows", []):
                if row.get("status") != "PAPER SETUP":
                    continue

                if row.get("score", 0) < 80:
                    continue

                symbol = row.get("symbol")
                side = row.get("side")
                mode = row.get("mode")

                # Maximaal een melding per setup per 5m/15m
                # candle. Dit voorkomt herhaalde meldingen
                # bij iedere nieuwe scanner-update.
                period = 300 if mode == "Scalp" else 900
                candle_bucket = int(scan_time // period)

                signal_id = (
                    symbol,
                    side,
                    mode,
                    candle_bucket,
                )

                if signal_id in seen_signals:
                    continue

                seen_signals.add(signal_id)

                message = (
                    "WEEX ENTRY ALERT\n\n"
                    f"Munt: {symbol}\n"
                    f"Richting: {side}\n"
                    f"Type: {mode}\n"
                    f"Score: {row.get('score')}/100\n"
                    f"Prijsindicatie: {row.get('one', '?')}\n"
                    f"Uitbraakniveau: {row.get('level', '?')}\n\n"
                    "Controleer de actuele WEEX-prijs, "
                    "stop-loss en liquidatierisico.\n"
                    "Geen gegarandeerde winstgevende entry."
                )

                for chat_id in list(alerts_enabled):
                    try:
                        send_message(chat_id, message)
                    except Exception as exc:
                        print("Alert error:", exc, flush=True)

            if len(seen_signals) > 5000:
                seen_signals.clear()

        except Exception as exc:
            print("Scanner error:", exc, flush=True)

        time.sleep(15)


@app.route("/")
def home():
    return (
        "<h1>WEEX Moon Telegram Bot</h1>"
        "<p>Open Telegram en stuur /start naar @WeexMoonBot.</p>"
    )


@app.route("/health")
def health():
    return jsonify({
        "running": True,
        "telegram_configured": bool(TOKEN),
        "subscribers": len(subscribers),
        "alerts_enabled": len(alerts_enabled),
        "last_scanner_update": last_scan,
        "last_error": last_error,
    })


if __name__ == "__main__":
    threading.Thread(
        target=telegram_loop,
        daemon=True,
    ).start()

    threading.Thread(
        target=alert_loop,
        daemon=True,
    ).start()

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )

