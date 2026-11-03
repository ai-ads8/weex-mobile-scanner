"""WEEX Moon v7.2 Telegram webhook.

Reads the latest scheduled scan from public GitHub raw content.
Does not place trades or trigger a fresh scan.
"""
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Flask, abort, jsonify, request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('weex-moon-telegram')
app = Flask(__name__)

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
API = f'https://api.telegram.org/bot{TOKEN}'
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://weex-moon-bot-live.onrender.com').rstrip('/')
REPO = os.getenv('MOON_GITHUB_REPO', 'ai-ads8/weex-mobile-scanner').strip()
BRANCH = os.getenv('MOON_GITHUB_BRANCH', 'main').strip()
MAX_AGE = max(60, int(os.getenv('MOON_STATE_MAX_AGE_SECONDS', '900')))
SECRET = hashlib.sha256(('weex-secret:' + TOKEN).encode()).hexdigest() if TOKEN else ''
PATH = ('/telegram/' + hashlib.sha256(('weex-path:' + TOKEN).encode()).hexdigest()[:24]
        if TOKEN else '/telegram/unconfigured')
STARTED_AT = int(time.time())


def tg(method, payload):
    if not TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN ontbreekt in Render')
    response = requests.post(API + '/' + method, json=payload, timeout=(5, 20))
    response.raise_for_status()
    result = response.json()
    if not result.get('ok'):
        raise RuntimeError('Telegram: ' + str(result.get('description', 'unknown')))
    return result.get('result')


def send(chat, message):
    for start in range(0, len(message), 3500):
        tg('sendMessage', {'chat_id': chat, 'text': message[start:start + 3500]})


def state_url():
    parts = REPO.split('/')
    if len(parts) != 2 or not all(parts) or not BRANCH:
        raise RuntimeError('MOON_GITHUB_REPO of MOON_GITHUB_BRANCH ongeldig')
    return (f'https://raw.githubusercontent.com/{quote(parts[0], safe="")}/'
            f'{quote(parts[1], safe="")}/{quote(BRANCH, safe="")}/moon_state.json')


def load_state():
    response = requests.get(
        state_url(),
        headers={'User-Agent': 'WEEX-Moon-Telegram-v7.2', 'Cache-Control': 'no-cache'},
        timeout=(5, 15),
    )
    if response.status_code == 404:
        raise RuntimeError('moon_state.json niet gevonden op de publieke GitHub-branch')
    if response.status_code in (401, 403, 429):
        raise RuntimeError(f'GitHub raw-toegang mislukt (HTTP {response.status_code})')
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get('version') != 'v7':
        raise RuntimeError('Ongeldige of onverwachte scannerdata')
    stamp = int(data.get('lastRun') or 0)
    age = int(time.time()) - stamp if stamp else None
    if age is None or age < -60 or age > MAX_AGE:
        raise RuntimeError(
            f'Scannerdata niet actueel (leeftijd: {age if age is not None else "onbekend"} sec)'
        )
    return data, age


def fmt(value):
    try:
        return f'{float(value):.8g}'
    except (TypeError, ValueError):
        return 'â'


def setup_text(signal):
    return (
        f'{signal.get("symbol", "?")} {signal.get("side", "?")} | PAPER SETUP\n'
        f'Score: {signal.get("score", "?")}/100 (geen winstkans)\n'
        f'Candle entry: {fmt(signal.get("entry"))}\n'
        f'Stop: {fmt(signal.get("stop"))}\n'
        f'TP1: {fmt(signal.get("tp1"))}\n'
        f'Volume: {fmt(signal.get("volume"))}x | Trend: {signal.get("aligned", "?")}/3\n'
        'Geen live entry. Controleer koers, spread, margin en liquidatierisico.'
    )


def process_message(message):
    chat = (message.get('chat') or {}).get('id')
    if chat is None:
        return
    command = (message.get('text') or '').strip().split(' ')[0].split('@')[0].lower()
    if command == '/test':
        send(chat, 'WEEX Moon v7.2 OK. Telegram-webhook reageert.')
    elif command == '/whoami':
        send(chat, f'Chat-ID: {chat}')
    elif command in ('/start', '/help'):
        send(chat,
             'WEEX Moon v7.2\n'
             '/scan - laatste opgeslagen scan\n'
             '/status - scannerstatus\n'
             '/test - webhook testen\n'
             '/whoami - chat-ID\n'
             '/alerts_on /alerts_off - informatie over meldingen\n'
             'Automatische alerts worden rechtstreeks door GitHub Actions verstuurd.')
    elif command in ('/alerts_on', '/alerts_off'):
        send(chat,
             'Automatische v7-meldingen worden door GitHub Actions verstuurd. '
             'Dit commando wijzigt die meldingen niet.')
    elif command in ('/scan', '/status'):
        try:
            state, age = load_state()
        except Exception as exc:
            log.warning('Scanner state unavailable: %s', exc)
            send(chat,
                 'WEEX Moon v7.2 | GEEN ACTUELE SCANNERDATA\n'
                 f'{str(exc)[:240]}\n'
                 'Gebruik geen oude setups als actuele entries.')
            return
        markets = int(state.get('marketsOK') or 0)
        setups = state.get('setups') if isinstance(state.get('setups'), list) else []
        errors = state.get('errors') if isinstance(state.get('errors'), list) else []
        stamp = datetime.fromtimestamp(int(state['lastRun']), tz=timezone.utc).strftime('%d-%m %H:%M UTC')
        if command == '/status':
            send(chat,
                 f'WEEX Moon v7.2 | ACTUEEL\n'
                 f'Laatste scan: {stamp}\n'
                 f'Leeftijd: {age} sec\n'
                 f'Markten OK: {markets}/8\n'
                 f'Paper-setups: {len(setups)}\n'
                 f'Fouten: {len(errors)}\n'
                 'Bron: GitHub Actions / moon_state.json\n'
                 'Geplande scans, niet realtime.')
        else:
            valid = [s for s in setups if isinstance(s, dict) and s.get('id')]
            valid.sort(key=lambda s: float(s.get('score') or 0), reverse=True)
            if not valid:
                send(chat,
                     f'WEEX Moon v7.2 | Laatste scan {age} sec geleden\n'
                     f'{markets}/8 markten gecontroleerd. Geen bevestigde paper-setups.\n'
                     'Dit is geen nieuwe realtime scan.')
            else:
                send(chat,
                     f'WEEX Moon v7.2 | Laatste scan {age} sec geleden\n'
                     'Historische paper-setups, GEEN live entries:\n\n' +
                     '\n\n'.join(setup_text(s) for s in valid[:3]))
    elif command.startswith('/'):
        send(chat, 'Onbekend commando. Gebruik /start /scan /status /test /whoami')


@app.post(PATH)
def webhook():
    supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not SECRET or not hmac.compare_digest(supplied, SECRET):
        abort(403)
    update = request.get_json(silent=True) or {}
    if update.get('message'):
        try:
            process_message(update['message'])
        except Exception:
            log.exception('Telegram update failed')
            return jsonify(ok=False), 500
    return jsonify(ok=True)


@app.get('/')
def home():
    return 'WEEX Moon v7.2 Telegram webhook running'


@app.get('/api/health')
def health():
    return jsonify(version='v7.2', uptimeSeconds=int(time.time()) - STARTED_AT,
                   webhookConfigured=bool(TOKEN), scanner='GitHub Actions raw state')


def configure_webhook():
    if not TOKEN:
        log.error('TELEGRAM_BOT_TOKEN ontbreekt')
        return
    import threading

    def work():
        for attempt in range(5):
            try:
                tg('setWebhook', {
                    'url': PUBLIC_URL + PATH,
                    'secret_token': SECRET,
                    'allowed_updates': ['message'],
                    'drop_pending_updates': False,
                })
                log.info('Telegram webhook configured')
                return
            except Exception:
                log.exception('Webhook registration attempt %s failed', attempt + 1)
                time.sleep(10 * (attempt + 1))

    threading.Thread(target=work, daemon=True).start()


configure_webhook()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')), threaded=True)
