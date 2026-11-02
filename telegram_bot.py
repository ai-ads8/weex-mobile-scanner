

"""WEEX Moon v6.2: Telegram webhook, diagnostics and paper alerts.

No orders are placed. Free Render services can sleep; continuous alerts are
not guaranteed. Persistent DB requires an externally mounted persistent path.
"""
import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time

import requests
from flask import Flask, abort, jsonify, request

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('weex-moon')
app = Flask(__name__)
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
API = f'https://api.telegram.org/bot{TOKEN}'
SCANNER = os.getenv('SCANNER_URL', 'https://weex-moon-scanner-live.onrender.com/api/status').strip()
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://weex-moon-bot-live.onrender.com').rstrip('/')
DB = os.getenv('DB_PATH', '/tmp/weex_moon.sqlite3')
MIN_ALERT_SCORE = max(0, min(100, int(os.getenv('MIN_ALERT_SCORE', '80'))))
MAX_SIGNAL_AGE = max(30, int(os.getenv('MAX_ALERT_AGE_SECONDS', '180')))
SECRET = hashlib.sha256(('weex-secret:' + TOKEN).encode()).hexdigest() if TOKEN else ''
PATH = '/telegram/' + hashlib.sha256(('weex-path:' + TOKEN).encode()).hexdigest()[:24] if TOKEN else '/telegram/unconfigured'
LOCK = threading.RLock()
STARTED = False
STATE = {'lastScan': 0, 'lastFetch': 0, 'error': 'Wachten op scanner', 'symbols': {}, 'setups': [], 'telegram': 'initialiseren', 'lastWebhookAt': 0, 'lastCommandAt': 0, 'lastSendAt': 0, 'lastSendError': None, 'lastWebhookError': None}


def db_open():
    db = sqlite3.connect(DB, timeout=10)
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY, alerts INTEGER NOT NULL DEFAULT 1)')
    db.execute('CREATE TABLE IF NOT EXISTS delivered (signal_id TEXT, chat_id INTEGER, sent_at REAL, PRIMARY KEY(signal_id, chat_id))')
    db.execute('CREATE TABLE IF NOT EXISTS updates (update_id INTEGER PRIMARY KEY, processed_at REAL NOT NULL)')
    db.commit()
    return db


def tg(method, payload=None, timeout=(5, 15)):
    if not TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN ontbreekt')
    response = requests.post(API + '/' + method, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get('ok'):
        raise RuntimeError(f'Telegram {method} failed: {str(result.get("description", "unknown"))[:150]}')
    return result.get('result')


def send(chat, message):
    remaining = str(message)
    while remaining:
        if len(remaining) <= 3500:
            part, remaining = remaining, ''
        else:
            cut = remaining.rfind('\n\n', 0, 3500)
            if cut < 500:
                cut = 3500
            part, remaining = remaining[:cut], remaining[cut:].lstrip()
        try:
            tg('sendMessage', {'chat_id': chat, 'text': part})
            with LOCK:
                STATE['lastSendAt'] = time.time()
                STATE['lastSendError'] = None
            log.info('Telegram sendMessage OK chat=%s chars=%s', chat, len(part))
        except Exception as exc:
            with LOCK:
                STATE['lastSendError'] = f'{type(exc).__name__}: {str(exc)[:160]}'
            log.exception('Telegram sendMessage FAILED chat=%s', chat)
            return False
    return True


def fmt(value):
    try:
        return f'{float(value):.8f}'.rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return '—'


def score(s):
    try:
        return float(s.get('score') or 0)
    except (ValueError, TypeError):
        return 0.0


def created_at(s):
    try:
        return float(s.get('createdAt') or 0)
    except (ValueError, TypeError):
        return 0.0


def signal_text(s):
    return (f'WEEX MOON | PAPER SETUP\n'
            f'{s.get("symbol", "?")} {s.get("side", "?")} | {s.get("mode", "?")}\n'
            f'Score: {fmt(s.get("score"))}/100 | {s.get("tier", "SETUP")} (geen winstkans)\n'
            f'Candle entry: {fmt(s.get("entry"))}\n'
            f'Stop: {fmt(s.get("stop"))}\n'
            f'TP1: {fmt(s.get("tp1"))}\n'
            f'Volume: {fmt(s.get("volumeRatio"))}x\n'
            f'Stopafstand: {fmt(s.get("riskPct"))}% | Trendbevestiging: {s.get("alignment", "—")}/3\n'
            'Paper signaal, geen live entry of order. Controleer mark price, margin en risico.')


def subscribers():
    with db_open() as db:
        return [r[0] for r in db.execute('SELECT chat_id FROM subscribers WHERE alerts=1')]


def set_subscription(chat, enabled):
    with db_open() as db:
        db.execute('INSERT INTO subscribers(chat_id,alerts) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET alerts=excluded.alerts', (chat, int(enabled)))


def scanner_snapshot():
    with LOCK:
        state = dict(STATE)
    age = int(time.time() - state['lastScan']) if state['lastScan'] else None
    healthy = sum(1 for x in state['symbols'].values() if isinstance(x, dict) and x.get('status') == 'OK')
    current = age is not None and 0 <= age <= 180 and not state['error'] and healthy > 0
    return state, age, healthy, current


def process_message(msg):
    chat = msg.get('chat', {}).get('id')
    if chat is None:
        return
    cmd = ((msg.get('text') or '').strip().split(' ')[0].split('@')[0].lower())
    with LOCK:
        STATE['lastCommandAt'] = time.time()
    log.info('Telegram command received chat=%s command=%s', chat, cmd[:35])
    if cmd == '/test':
        send(chat, 'WEEX Moon v6.2 OK. Telegram-webhook reageert.')
    elif cmd == '/whoami':
        send(chat, f'Chat-ID: {chat}')
    elif cmd in ('/start', '/alerts_on', '/alerts_off'):
        enabled = cmd != '/alerts_off'
        set_subscription(chat, enabled)
        send(chat, 'WEEX Moon verbonden. Meldingen ' + ('AAN' if enabled else 'UIT') + f'.\nAutomatische minimumscore: {MIN_ALERT_SCORE}/100.\n/status /scan /test /alerts_on /alerts_off /whoami')
    elif cmd in ('/status', '/scan'):
        state, age, healthy, current = scanner_snapshot()
        if cmd == '/status':
            send(chat, f'Scanner: {"ACTUEEL" if current else "NIET ACTUEEL"}\nLaatste scan: {age if age is not None else "?"} sec\nMarkten OK: {healthy}\nSetups: {len(state["setups"])}\nFout: {state["error"] or "geen"}\nAutomatische minimumscore: {MIN_ALERT_SCORE}/100')
        elif not current:
            send(chat, 'Geen actuele scannerdata. Gebruik geen oude signalen.\n' + str(state['error']))
        else:
            valid = [s for s in state['setups'] if isinstance(s, dict) and s.get('id') and 0 <= time.time() - created_at(s) <= MAX_SIGNAL_AGE]
            valid.sort(key=lambda s: (score(s), created_at(s)), reverse=True)
            if not valid:
                send(chat, 'Scanner actief. Geen recente bevestigde setups.')
            else:
                send(chat, 'Beste recente paper-setups (geen live entries):\n\n' + '\n\n'.join(signal_text(s) for s in valid[:3]))
    elif cmd.startswith('/'):
        send(chat, 'Onbekend commando. Gebruik /start /test /status /scan /alerts_on /alerts_off /whoami')


def notify(payload):
    now = time.time()
    signals = [s for s in payload.get('setups', []) if isinstance(s, dict) and s.get('id') and s.get('status') in ('PAPER_OPEN', 'OPEN', None) and score(s) >= MIN_ALERT_SCORE and 0 <= now - created_at(s) <= MAX_SIGNAL_AGE]
    signals.sort(key=lambda s: (score(s), created_at(s)), reverse=True)
    # Group nearby same-symbol/same-direction alerts; avoid duplicate notifications.
    groups = {}
    for s in signals:
        key = (s.get('symbol'), s.get('side'))
        groups.setdefault(key, []).append(s)
    chats = subscribers()
    for group in groups.values():
        for chat in chats:
            ids = [str(s['id']) for s in group]
            with db_open() as db:
                placeholders = ','.join('?' for _ in ids)
                existing = {r[0] for r in db.execute('SELECT signal_id FROM delivered WHERE chat_id=? AND signal_id IN (' + placeholders + ')', (chat, *ids))}
            fresh = [s for s in group if str(s['id']) not in existing]
            if not fresh:
                continue
            best = max(fresh, key=score)
            message = signal_text(best)
            if len(fresh) > 1:
                message += f'\nBevestiging: {len(fresh)} setups in dezelfde marktbeweging.'
            if send(chat, message):
                with db_open() as db:
                    db.executemany('INSERT OR IGNORE INTO delivered(signal_id,chat_id,sent_at) VALUES (?,?,?)', [(str(s['id']), chat, now) for s in fresh])
    with db_open() as db:
        db.execute('DELETE FROM delivered WHERE sent_at < ?', (now - 14 * 86400,))
        db.execute('DELETE FROM updates WHERE processed_at < ?', (now - 7 * 86400,))


def scanner_loop():
    was_healthy = None
    last_warning = 0
    while True:
        try:
            response = requests.get(SCANNER, timeout=(5, 15), headers={'Cache-Control': 'no-cache'})
            response.raise_for_status()
            payload = response.json()
            stamp = float(payload.get('lastScan') or 0)
            symbols = payload.get('symbols')
            setups = payload.get('setups')
            if not stamp or not 0 <= time.time() - stamp <= 180:
                raise RuntimeError('Scannerdata verouderd')
            if not isinstance(symbols, dict) or not isinstance(setups, list):
                raise RuntimeError('Ongeldig scannerformaat')
            healthy = sum(1 for x in symbols.values() if isinstance(x, dict) and x.get('status') == 'OK' and float(x.get('closed1mPrice') or 0) > 0)
            if healthy == 0:
                raise RuntimeError('Geen gezonde markten')
            with LOCK:
                STATE.update(lastScan=stamp, lastFetch=time.time(), error=None, symbols=symbols, setups=setups)
            if was_healthy is False:
                for chat in subscribers():
                    send(chat, 'WEEX Moon: scanner weer ACTUEEL.')
            was_healthy = True
            notify(payload)
        except Exception as exc:
            error = f'{type(exc).__name__}: {str(exc)[:120]}'
            with LOCK:
                STATE['error'] = error
            log.warning('Scanner unavailable: %s', error)
            if was_healthy is not False and time.time() - last_warning > 900:
                last_warning = time.time()
                for chat in subscribers():
                    send(chat, 'WEEX Moon: scanner NIET ACTUEEL. Gebruik geen oude signalen.')
            was_healthy = False
        time.sleep(30)


def configure_webhook():
    while True:
        try:
            tg('setWebhook', {'url': PUBLIC_URL + PATH, 'secret_token': SECRET, 'allowed_updates': ['message'], 'drop_pending_updates': False}, timeout=(5, 20))
            info = tg('getWebhookInfo')
            with LOCK:
                STATE['telegram'] = 'webhook ingesteld'
                STATE['lastWebhookError'] = info.get('last_error_message')
            log.info('Webhook configured pending=%s last_error=%s', info.get('pending_update_count'), info.get('last_error_message'))
            return
        except Exception as exc:
            with LOCK:
                STATE['telegram'] = 'webhook instellen mislukt'
                STATE['lastWebhookError'] = f'{type(exc).__name__}: {str(exc)[:150]}'
            log.exception('Webhook setup failed')
            time.sleep(30)


def webhook_monitor():
    while True:
        time.sleep(300)
        if not TOKEN:
            continue
        try:
            info = tg('getWebhookInfo')
            expected = PUBLIC_URL + PATH
            actual = info.get('url') or ''
            with LOCK:
                STATE['lastWebhookError'] = info.get('last_error_message')
                STATE['telegram'] = 'webhook ingesteld' if actual == expected else 'webhook URL mismatch'
            log.info('Webhook health configured=%s pending=%s last_error=%s', actual == expected, info.get('pending_update_count'), info.get('last_error_message'))
            if actual != expected:
                log.warning('Webhook URL mismatch; re-registering')
                configure_webhook()
        except Exception:
            log.exception('Webhook monitor failed')


@app.post(PATH)
def webhook():
    supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not SECRET or not hmac.compare_digest(supplied, SECRET):
        log.warning('Rejected webhook: invalid secret')
        abort(403)
    update = request.get_json(silent=True) or {}
    update_id = update.get('update_id')
    msg = update.get('message')
    with LOCK:
        STATE['lastWebhookAt'] = time.time()
    log.info('Webhook received update_id=%s has_message=%s', update_id, bool(msg))
    try:
        if update_id is not None:
            with db_open() as db:
                exists = db.execute('SELECT 1 FROM updates WHERE update_id=?', (update_id,)).fetchone()
            if exists:
                return jsonify(ok=True, duplicate=True)
        if msg:
            process_message(msg)
        if update_id is not None:
            with db_open() as db:
                db.execute('INSERT OR IGNORE INTO updates(update_id,processed_at) VALUES (?,?)', (update_id, time.time()))
        return jsonify(ok=True)
    except Exception:
        log.exception('Webhook processing failed update_id=%s', update_id)
        return jsonify(ok=False), 500


@app.get('/')
def home():
    return 'WEEX Moon v6.2 webhook server running'


@app.get('/api/status')
def status():
    with LOCK:
        data = dict(STATE)
    data['scanner_current'] = bool(data['lastScan'] and 0 <= time.time() - data['lastScan'] <= 180 and not data['error'])
    data['version'] = 'v6.2'
    data['min_alert_score'] = MIN_ALERT_SCORE
    data['db_persistent'] = not DB.startswith('/tmp/')
    return jsonify(data)


def start():
    global STARTED
    if STARTED:
        return
    STARTED = True
    db_open().close()
    threading.Thread(target=scanner_loop, daemon=True, name='scanner').start()
    if TOKEN:
        threading.Thread(target=configure_webhook, daemon=True, name='webhook-setup').start()
        threading.Thread(target=webhook_monitor, daemon=True, name='webhook-monitor').start()
    else:
        log.error('TELEGRAM_BOT_TOKEN ontbreekt')


start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')), threaded=True)
