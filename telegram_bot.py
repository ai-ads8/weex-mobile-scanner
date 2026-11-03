"""WEEX Moon v7.1 Telegram webhook; reads scheduled GitHub scan state.

This bot never places trades. GitHub Actions owns scheduled scans and alerts.
/scan displays the latest stored result; it does not initiate a new scan.
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
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
API = f'https://api.telegram.org/bot{TOKEN}'
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://weex-moon-bot-live.onrender.com').rstrip('/')
REPO = os.getenv('MOON_GITHUB_REPO', 'ai-ads8/weex-mobile-scanner').strip()
BRANCH = os.getenv('MOON_GITHUB_BRANCH', 'main').strip()
GITHUB_TOKEN = os.getenv('MOON_GITHUB_TOKEN', '').strip()  # optional for private repositories
MAX_AGE = int(os.getenv('MOON_STATE_MAX_AGE_SECONDS', '900'))
SECRET = hashlib.sha256(('weex-secret:' + TOKEN).encode()).hexdigest() if TOKEN else ''
PATH = ('/telegram/' + hashlib.sha256(('weex-path:' + TOKEN).encode()).hexdigest()[:24]
        if TOKEN else '/telegram/unconfigured')
STARTED_AT = int(time.time())


def tg(method, payload):
    if not TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN ontbreekt in Render')
    r = requests.post(API + '/' + method, json=payload, timeout=(5, 20))
    r.raise_for_status()
    data = r.json()
    if not data.get('ok'):
        raise RuntimeError('Telegram: ' + str(data.get('description', 'unknown')))
    return data.get('result')


def send(chat, message):
    for start in range(0, len(message), 3500):
        tg('sendMessage', {'chat_id': chat, 'text': message[start:start + 3500]})


def state_url():
    if not REPO or '/' not in REPO:
        raise RuntimeError('MOON_GITHUB_REPO ongeldig')
    return f'https://api.github.com/repos/{REPO}/contents/moon_state.json'


def load_state():
    headers = {'Accept': 'application/vnd.github.raw+json', 'User-Agent': 'WEEX-Moon-Telegram-v7.1'}
    if GITHUB_TOKEN:
        headers['Authorization'] = 'Bearer ' + GITHUB_TOKEN
    r = requests.get(state_url(), params={'ref': BRANCH}, headers=headers, timeout=(5, 15))
    if r.status_code == 404:
        raise RuntimeError('GitHub-bestand niet bereikbaar. Is de repo privÃ©? Stel dan MOON_GITHUB_TOKEN in.')
    if r.status_code in (401, 403):
        raise RuntimeError('GitHub-toegang geweigerd of limiet bereikt')
    r.raise_for_status()
    data = r.json()
    # Some GitHub responses return the file as base64 instead of raw JSON.
    if isinstance(data, dict) and data.get('encoding') == 'base64' and data.get('content'):
        import base64
        data = json.loads(base64.b64decode(data['content']).decode('utf-8'))
    if not isinstance(data, dict):
        raise RuntimeError('Ongeldige moon_state.json')
    stamp = int(data.get('lastRun') or 0)
    age = int(time.time()) - stamp if stamp else None
    if age is None or age < -60 or age > MAX_AGE:
        raise RuntimeError(f'Scannerdata niet actueel (leeftijd: {age if age is not None else "onbekend"} sec)')
    return data, age


def fmt(value):
    try:
        return f'{float(value):.8g}'
    except (TypeError, ValueError):
        return 'â'


def setup_text(s):
    return (f'{s.get("symbol", "?")} {s.get("side", "?")} | PAPER SETUP\n'
            f'Score: {s.get("score", "?")}/100 (geen winstkans)\n'
            f'Candle entry: {fmt(s.get("entry"))}\n'
            f'Stop: {fmt(s.get("stop"))}\n'
            f'TP1: {fmt(s.get("tp1"))}\n'
            f'Volume: {fmt(s.get("volume"))}x | Trend: {s.get("aligned", "?")}/3\n'
            'Geen live entry. Controleer actuele koers, spread, margin en liquidatierisico.')


def process_message(message):
    chat = (message.get('chat') or {}).get('id')
    if chat is None:
        return
    command = (message.get('text') or '').strip().split(' ')[0].split('@')[0].lower()
    if command == '/test':
        send(chat, 'WEEX Moon v7.1 OK. Telegram-webhook reageert.')
    elif command == '/whoami':
        send(chat, f'Chat-ID: {chat}')
    elif command in ('/start', '/help'):
        send(chat, 'WEEX Moon v7.1\n/scan - laatste GitHub-scan\n/status - scannerstatus\n'
             '/test - webhook testen\n/whoami - chat-ID\n'
             '/alerts_on /alerts_off - informatie over meldingen\n'
             'Automatische alerts worden rechtstreeks door GitHub Actions verstuurd.')
    elif command in ('/alerts_on', '/alerts_off'):
        send(chat, 'Automatische v7-meldingen worden beheerd door GitHub Actions. '
             'Deze Telegram-commandoâs wijzigen die instellingen niet. '
             'Gebruik de GitHub Actions secrets en workflow om meldingen te beheren.')
    elif command in ('/scan', '/status'):
        try:
            state, age = load_state()
        except Exception as exc:
            logging.warning('GitHub state unavailable: %s', exc)
            send(chat, 'WEEX Moon v7: GEEN ACTUELE SCANNERDATA.\n'
                 f'{str(exc)[:240]}\nGebruik geen oude setups als actuele entries.')
            return
        markets = int(state.get('marketsOK') or 0)
        setups = state.get('setups') if isinstance(state.get('setups'), list) else []
        errors = state.get('errors') if isinstance(state.get('errors'), list) else []
        stamp = datetime.fromtimestamp(int(state['lastRun']), tz=timezone.utc).strftime('%d-%m %H:%M UTC')
        if command == '/status':
            send(chat, f'WEEX Moon v7 | ACTUEEL\nLaatste scan: {stamp}\n'
                 f'Leeftijd: {age} sec\nMarkten OK: {markets}/8\n'
                 f'Paper-setups: {len(setups)}\nFouten: {len(errors)}\n'
                 'Bron: GitHub Actions / moon_state.json\n'
                 'GitHub-scans zijn gepland, niet realtime.')
        else:
            valid = [s for s in setups if isinstance(s, dict) and s.get('id')]
            valid.sort(key=lambda s: float(s.get('score') or 0), reverse=True)
            if not valid:
                send(chat, f'WEEX Moon v7 | Laatste scan {age} sec geleden\n'
                     f'{markets}/8 markten gecontroleerd. Geen bevestigde paper-setups.\n'
                     'Dit is geen nieuwe realtime scan.')
            else:
                send(chat, f'WEEX Moon v7 | Laatste scan {age} sec geleden\n'
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
            logging.exception('Telegram update failed')
            return jsonify(ok=False), 500
    return jsonify(ok=True)


@app.get('/')
def home():
    return 'WEEX Moon v7.1 Telegram webhook running'


@app.get('/api/health')
def health():
    return jsonify(version='v7.1', uptimeSeconds=int(time.time()) - STARTED_AT,
                   webhookConfigured=bool(TOKEN), scanner='GitHub Actions state')


def configure_webhook():
    if not TOKEN:
        logging.error('TELEGRAM_BOT_TOKEN ontbreekt')
        return
    import threading
    def work():
        for attempt in range(5):
            try:
                tg('setWebhook', {'url': PUBLIC_URL + PATH,
                                  'secret_token': SECRET,
                                  'allowed_updates': ['message'],
                                  'drop_pending_updates': False})
                logging.info('Telegram webhook configured')
                return
            except Exception:
                logging.exception('Webhook registration attempt %s failed', attempt + 1)
                time.sleep(10 * (attempt + 1))
    threading.Thread(target=work, daemon=True).start()


configure_webhook()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')), threaded=True)
