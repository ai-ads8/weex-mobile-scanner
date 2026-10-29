"""WEEX Moon Telegram bridge — reads confirmed setups from the new scanner.
No trading orders. Candle prices are not live mark prices.
"""
import os
import time
import json
import logging
import sqlite3
import threading
import fcntl
import re
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
LAST_HEALTH_ALERT = 0
LAST_HEALTH_STATE = None
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
        logging.warning('Telegram verzenden mislukt: %s', type(exc).__name__)
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


def fmt(v):
    try:return f'{float(v):.8f}'.rstrip('0').rstrip('.')
    except (TypeError,ValueError):return '—'

def format_signal(signal):
    risk=signal.get('riskPct')
    score=signal.get('score')
    return (f"WEEX MOON | PAPER SETUP\n{signal.get('symbol','?')} {signal.get('side','?')} | {signal.get('mode','?')}\n"
            f"Score: {score if score is not None else '—'}/100 | {signal.get('tier','SETUP')} (geen winstkans)\n"
            f"Candle entry: {fmt(signal.get('entry'))}\nStop: {fmt(signal.get('stop'))}\n"
            f"TP1: {fmt(signal.get('tp1'))}\nVolume: {fmt(signal.get('volumeRatio'))}x\n"
            f"Stopafstand: {fmt(risk)}% | Trendbevestiging: {signal.get('alignment','—')}/3\n"
            "LET OP: Hoge leverage kan liquidatie veroorzaken voor de technische stop. "
            "Paper signaal, geen order. Controleer mark price, margin en risico.")

def notify_new_signals(payload):
    now=time.time()
    with connect() as db:
        subscribers=[r['chat_id'] for r in db.execute('SELECT chat_id FROM subscribers WHERE alerts=1')]
        candidates=[s for s in payload.get('setups',[]) if isinstance(s,dict)
                    and s.get('status') in ('PAPER_OPEN','OPEN',None)
                    and s.get('id') and 0<=now-float(s.get('createdAt') or 0)<=600]
        # Group same-symbol/same-direction setups that arrive together into one alert.
        groups={}
        for s in candidates:
            key=(s.get('symbol'),s.get('side'))
            groups.setdefault(key,[]).append(s)
        for (symbol,side),items in groups.items():
            items.sort(key=lambda s:float(s.get('createdAt') or 0))
            # Only group signals whose first detection is within a 5-minute window.
            batches=[]
            for s in items:
                if not batches or float(s.get('createdAt') or 0)-float(batches[-1][0].get('createdAt') or 0)>300:
                    batches.append([s])
                else:batches[-1].append(s)
            for batch in batches:
                ids=[str(s['id']) for s in batch]
                primary=max(batch,key=lambda s:(s.get('score') or 0))
                message=format_signal(primary)
                if len(batch)>1:
                    modes=', '.join(sorted({str(s.get('mode','?')) for s in batch}))
                    message+=f'\nBevestiging: {len(batch)} setups ({modes}); zelfde marktbeweging.'
                for chat_id in subscribers:
                    existing={r[0] for r in db.execute('SELECT signal_id FROM delivered WHERE chat_id=? AND signal_id IN ('+','.join('?' for _ in ids)+')',(chat_id,*ids))}
                    if len(existing)==len(ids):continue
                    if existing:
                        fresh=[s for s in batch if str(s['id']) not in existing]
                        message_to_send=format_signal(max(fresh,key=lambda s:(s.get('score') or 0)))
                    else:message_to_send=message
                    if send(chat_id,message_to_send):
                        db.executemany('INSERT OR IGNORE INTO delivered VALUES (?,?,?)',[(i,chat_id,now) for i in ids])
        db.execute('DELETE FROM delivered WHERE sent_at < ?',(now-14*86400,))
        db.commit()


def scanner_loop():
    global LAST_HEALTH_ALERT, LAST_HEALTH_STATE
    while True:
        try:
            payload = scanner_read()
            if LAST_HEALTH_STATE is False:
                with connect() as db:
                    for r in db.execute('SELECT chat_id FROM subscribers WHERE alerts=1'):
                        send(r['chat_id'],'WEEX Moon: scanner weer ACTUEEL. Nieuwe signalen worden weer gecontroleerd.')
            LAST_HEALTH_STATE=True
            notify_new_signals(payload)
        except Exception as exc:
            with LOCK:
                STATE['error'] = str(exc)[:240]
            logging.warning('Scanner fout: %s', str(exc)[:240])
            if LAST_HEALTH_STATE is not False and time.time()-LAST_HEALTH_ALERT>900:
                LAST_HEALTH_ALERT=time.time()
                with connect() as db:
                    for r in db.execute('SELECT chat_id FROM subscribers WHERE alerts=1'):
                        send(r['chat_id'],'WEEX Moon waarschuwing: scanner NIET ACTUEEL. Geen signalen vertrouwen tot /status weer ACTUEEL is.')
            LAST_HEALTH_STATE=False
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
    # Prevent duplicate polling workers on the same host.
    lock_file = open("/tmp/weex_telegram_poll.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.error("Telegram polling already active in this instance")
        return
    if not TOKEN:
        logging.error('TELEGRAM_BOT_TOKEN ontbreekt')
        return
    offset = None
    consecutive_conflicts = 0
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
            consecutive_conflicts = 0
            with LOCK:
                STATE['telegram'] = 'polling actief'
        except Exception as exc:
            # Never print Telegram API URLs: they contain the bot token.
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            is_conflict = status == 409
            if is_conflict:
                consecutive_conflicts += 1
                reason = '409 Conflict: another bot poller is using this token'
                delay = min(120, 10 * consecutive_conflicts)
            else:
                reason = f'Telegram polling error ({type(exc).__name__}, HTTP {status or "?"})'
                delay = 5
            with LOCK:
                STATE['telegram'] = reason
            logging.warning('%s; retry in %ss', reason, delay)
            time.sleep(delay)


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
