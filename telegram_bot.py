"""WEEX Moon Telegram bridge — reads confirmed setups from the new scanner.
No trading orders. Candle prices are not live mark prices.
"""
import os
import time
import json
import logging
import sqlite3
import threading
import requests
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
API = f'https://api.telegram.org/bot{TOKEN}'
SCANNER = os.getenv('SCANNER_URL', 'https://weex-moon-scanner-live.onrender.com/api/status')
DB = os.getenv('DB_PATH', '/tmp/weex_moon.sqlite3')
SCAN_INTERVAL = 30
STATE = {'lastScan': 0, 'lastFetch': 0, 'error': 'Wachten op scanner', 'symbols': {}, 'setups': [], 'telegram': 'initialiseren'}
LOCK = threading.RLock()
SESSION = requests.Session()


def connect():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY, alerts INTEGER NOT NULL DEFAULT 1)')
    conn.execute('CREATE TABLE IF NOT EXISTS delivered (signal_id TEXT NOT NULL, chat_id INTEGER NOT NULL, sent_at REAL NOT NULL, PRIMARY KEY(signal_id,chat_id))')
    conn.commit()
    return conn


def send(chat_id, message):
    if not TOKEN:
        logging.warning('TELEGRAM_BOT_TOKEN ontbreekt')
        return False
    try:
        r = SESSION.post(API + '/sendMessage', json={'chat_id': chat_id, 'text': message}, timeout=15)
        r.raise_for_status()
        response = r.json()
        if not response.get('ok'):
            raise RuntimeError(str(response.get('description', 'Telegram API fout')))
        return True
    except Exception as exc:
        logging.warning('Telegram verzenden mislukt: %s', str(exc)[:180])
        return False


def scanner_read():
    response = SESSION.get(SCANNER, timeout=22, headers={'Cache-Control': 'no-cache'})
    response.raise_for_status()
    payload = response.json()
    timestamp = float(payload.get('lastScan') or 0)
    if not timestamp or time.time() - timestamp > 180 or timestamp - time.time() > 30:
        raise RuntimeError('Scanner niet actueel: laatste scan ouder dan 180 seconden')
    symbols = payload.get('symbols')
    setups = payload.get('setups')
    if not isinstance(symbols, dict) or not isinstance(setups, list):
        raise RuntimeError('Ongeldig formaat van de scanner')
    healthy = {k: v for k, v in symbols.items() if isinstance(v, dict) and v.get('status') == 'OK' and float(v.get('closed1mPrice') or 0) > 0}
    if not healthy:
        errors = payload.get('errors', [])
        raise RuntimeError('Geen bruikbare marktgegevens: ' + '; '.join(map(str, errors[:2])))
    with LOCK:
        STATE.update(lastScan=timestamp, lastFetch=time.time(), error=None, symbols=symbols, setups=setups)
    return payload


def format_signal(signal):
    symbol = signal.get('symbol', '?')
    direction = signal.get('side', '?')
    mode = signal.get('mode', '?')
    entry = signal.get('entry')
    stop = signal.get('stop')
    tp1 = signal.get('tp1')
    volume = signal.get('volumeRatio')
    return (f'WEEX MOON | PAPER SETUP\n{symbol} {direction} | {mode}\n'
            f'Gesloten-candle entry: {entry}\nStop: {stop}\nTP1: {tp1}\n'
            f'Volume: {volume}x\n'
            'Experimenteel signaal, geen order. Controleer actuele WEEX-koers, mark price en risico.')


def notify_new_signals(payload):
    with connect() as db:
        subscribers = [r['chat_id'] for r in db.execute('SELECT chat_id FROM subscribers WHERE alerts=1')]
        for signal in payload.get('setups', []):
            if not isinstance(signal, dict) or signal.get('status') not in ('PAPER_OPEN', 'OPEN', None):
                continue
            signal_id = str(signal.get('id') or '')
            created = float(signal.get('createdAt') or 0)
            if not signal_id or not created or time.time() - created > 600 or created - time.time() > 30:
                continue
            for chat_id in subscribers:
                already = db.execute('SELECT 1 FROM delivered WHERE signal_id=? AND chat_id=?', (signal_id, chat_id)).fetchone()
                if already:
                    continue
                if send(chat_id, format_signal(signal)):
                    db.execute('INSERT OR IGNORE INTO delivered VALUES (?,?,?)', (signal_id, chat_id, time.time()))
        db.execute('DELETE FROM delivered WHERE sent_at < ?', (time.time() - 14 * 86400,))
        db.commit()


def scanner_loop():
    while True:
        try:
            payload = scanner_read()
            notify_new_signals(payload)
        except Exception as exc:
            with LOCK:
                STATE['error'] = str(exc)[:240]
            logging.warning('Scanner fout: %s', str(exc)[:240])
        time.sleep(SCAN_INTERVAL)


def process_message(message):
    chat = message.get('chat', {}).get('id')
    if not chat:
        return
    command = (message.get('text') or '').strip().split(' ')[0].split('@')[0].lower()
    if command == '/start':
        with connect() as db:
            db.execute('INSERT INTO subscribers(chat_id,alerts) VALUES (?,1) ON CONFLICT(chat_id) DO UPDATE SET alerts=1', (chat,))
        send(chat, 'WEEX Moon verbonden. Meldingen ingeschakeld.\n/status — scannerstatus\n/scan — recente setups\n/alerts_on — meldingen aan\n/alerts_off — meldingen uit\n/test — test Telegram-bericht\n/whoami — chat-ID')
    elif command == '/whoami':
        send(chat, f'Chat-ID: {chat}')
    elif command in ('/alerts_on', '/alerts_off'):
        enabled = int(command == '/alerts_on')
        with connect() as db:
            db.execute('INSERT INTO subscribers(chat_id,alerts) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET alerts=excluded.alerts', (chat, enabled))
        send(chat, 'Meldingen ingeschakeld.' if enabled else 'Meldingen uitgeschakeld.')
    elif command == '/test':
        send(chat, 'WEEX Moon testbericht ontvangen. Telegram werkt. Dit bevestigt nog geen scanner-signalen.')
    elif command == '/status':
        with LOCK:
            state = dict(STATE)
        age = int(time.time() - state['lastScan']) if state['lastScan'] else None
        ok = age is not None and 0 <= age <= 180 and not state['error']
        healthy = sum(1 for item in state['symbols'].values() if item.get('status') == 'OK')
        send(chat, f'Scanner: {"ACTUEEL" if ok else "NIET ACTUEEL"}\nLaatste scan: {age if age is not None else "?"} seconden geleden\nMarkten OK: {healthy}\nSetups in feed: {len(state["setups"])}\nFout: {state["error"] or "geen"}')
    elif command == '/scan':
        with LOCK:
            state = dict(STATE)
        if not state['lastScan'] or time.time() - state['lastScan'] > 180 or state['error']:
            send(chat, 'Geen actuele scannerdata. Gebruik deze signalen niet.\n' + str(state['error']))
        elif not state['setups']:
            send(chat, 'Scanner actief. Geen bevestigde setups op dit moment. Dat is normaal.')
        else:
            recent = sorted(state['setups'], key=lambda s: float(s.get('createdAt') or 0), reverse=True)[:5]
            send(chat, 'Recente paper-setups (niet per se nieuwe entries):\n\n' + '\n\n'.join(format_signal(s) for s in recent))


def telegram_loop():
    if not TOKEN:
        logging.error('TELEGRAM_BOT_TOKEN ontbreekt')
        return
    offset = None
    while True:
        try:
            args = {'timeout': 20, 'allowed_updates': json.dumps(['message'])}
            if offset is not None:
                args['offset'] = offset
            response = SESSION.get(API + '/getUpdates', params=args, timeout=30)
            response.raise_for_status()
            data = response.json()
            if not data.get('ok'):
                raise RuntimeError(data.get('description', 'Telegram getUpdates fout'))
            for update in data.get('result', []):
                offset = update['update_id'] + 1
                if 'message' in update:
                    process_message(update['message'])
            with LOCK:
                STATE['telegram'] = 'polling actief'
        except Exception as exc:
            with LOCK:
                STATE['telegram'] = 'polling fout: ' + str(exc)[:140]
            logging.warning('Telegram polling fout: %s', str(exc)[:180])
            time.sleep(5)


@app.get('/')
def index():
    return ('WEEX Moon bridge actief. Gebruik /status en /scan in @WeexMoonBot. '
            'Scannergegevens zijn candle-gebaseerd, geen realtime mark prices.\n')


@app.get('/api/status')
def status():
    with LOCK:
        data = dict(STATE)
    data['scanner_current'] = bool(data['lastScan'] and time.time() - data['lastScan'] <= 180 and not data['error'])
    return jsonify(data)


_started = False

def start_workers():
    global _started
    if not _started:
        _started = True
        connect().close()
        threading.Thread(target=scanner_loop, daemon=True).start()
        threading.Thread(target=telegram_loop, daemon=True).start()


start_workers()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')), threaded=True)
