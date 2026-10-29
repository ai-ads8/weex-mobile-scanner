import os, time, json, sqlite3, threading, logging, requests, io
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string
from PIL import Image

app = Flask(__name__)
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
API = 'https://api.telegram.org/bot' + TOKEN
SCANNER = os.getenv('SCANNER_URL', 'https://weex-mobile-scanner-v5.onrender.com/api/status')
ACCESS = os.getenv('TRADE_APP_PASSWORD', '')
DB = os.getenv('TRADE_DB_PATH', '/tmp/weex_v7.db')
SYMBOLS = ('BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','HBARUSDT','INJUSDT','UNIUSDT')
lock = threading.RLock()
cache = {'updated':0, 'rows':[], 'prices':{}, 'error':'Wachten op scanner', 'last_fetch':0}
log = logging.getLogger('weex')
logging.basicConfig(level=logging.INFO)

def db():
    c = sqlite3.connect(DB, timeout=20, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c

def setup():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY, alerts INTEGER DEFAULT 1)')
        c.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, symbol TEXT NOT NULL,
            side TEXT NOT NULL, entry REAL NOT NULL, leverage REAL NOT NULL,
            size_usdt REAL, stop REAL, target REAL, liquidation REAL,
            created REAL NOT NULL, active INTEGER DEFAULT 1, last_price REAL,
            last_alert TEXT, last_alert_time REAL DEFAULT 0)''')
        c.execute('CREATE TABLE IF NOT EXISTS signal_alerts (key TEXT PRIMARY KEY, created REAL NOT NULL)')

def send(chat_id, msg):
    if not TOKEN: return False
    try:
        r=requests.post(API+'/sendMessage',json={'chat_id':chat_id,'text':msg[:3900]},timeout=12)
        r.raise_for_status()
        return r.json().get('ok',False)
    except Exception as exc:
        log.warning('Telegram send failed: %s',str(exc)[:150]);return False

def fetch_scanner():
    r=requests.get(SCANNER,timeout=20)
    r.raise_for_status(); data=r.json()
    if not isinstance(data.get('rows'),list): raise ValueError('Scanner returned no rows')
    stamp=float(data.get('updated') or 0)
    if stamp<=0 or time.time()-stamp>180: raise ValueError('Scannerdata ouder dan 180 seconden')
    prices={}
    for row in data['rows']:
        try:
            p=float(row.get('one') or row.get('price') or 0)
            if p>0: prices[row['symbol']]=p
        except (TypeError,ValueError,KeyError): pass
    if not prices: raise ValueError('Geen geldige marktprijzen')
    with lock: cache.update(updated=stamp,rows=data['rows'],prices=prices,error=None,last_fetch=time.time())
    return data

def scan_loop():
    while True:
        try:
            data=fetch_scanner()
            monitor_trades()
            alert_setups(data)
        except Exception as exc:
            with lock: cache['error']=str(exc)[:180]
            log.warning('Scanner unavailable: %s',str(exc)[:150])
        time.sleep(20)

def alert_setups(data):
    stamp=float(data['updated'])
    with db() as c:
        subscribers=c.execute('SELECT chat_id FROM subscribers WHERE alerts=1').fetchall()
        if not subscribers:return
        for row in data['rows']:
            if row.get('status')!='PAPER SETUP' or float(row.get('score') or 0)<85: continue
            symbol=row.get('symbol');mode=row.get('mode');side=row.get('side')
            if symbol not in SYMBOLS or mode not in ('Scalp','Intraday') or side not in ('LONG','SHORT'):continue
            bucket=int(stamp//(300 if mode=='Scalp' else 900))
            key=f'{symbol}:{mode}:{side}:{bucket}'
            if c.execute('SELECT 1 FROM signal_alerts WHERE key=?',(key,)).fetchone():continue
            msg=(f'WEEX SETUP ALERT (experimenteel)\n{symbol} {side} · {mode}\n'
                 f'Score {row.get("score")}/100 · Indicatieve prijs {row.get("one", "?")}\n'
                 f'Uitbraakniveau {row.get("level", "?")}\n'
                 'Controleer de actuele prijs, spread en liquidatierisico. Geen winstgarantie.')
            delivered=False
            for sub in subscribers: delivered=send(sub['chat_id'],msg) or delivered
            if delivered:c.execute('INSERT OR IGNORE INTO signal_alerts(key,created) VALUES (?,?)',(key,time.time()))
        c.execute('DELETE FROM signal_alerts WHERE created < ?',(time.time()-86400*7,))

def monitor_trades():
    with lock: prices=dict(cache['prices'])
    with db() as c:
        for t in c.execute('SELECT * FROM trades WHERE active=1').fetchall():
            p=prices.get(t['symbol'])
            if not p:continue
            c.execute('UPDATE trades SET last_price=? WHERE id=?',(p,t['id']))
            side=t['side'];islong=side=='LONG';entry=t['entry']
            change=(p/entry-1)*(1 if islong else -1)*100
            stop=t['stop'];target=t['target'];liq=t['liquidation']
            alerts=[]
            if stop and ((islong and p<=stop) or (not islong and p>=stop)):
                alerts.append(('STOP',f'Stopniveau {stop} bereikt/doorbroken. Controleer direct of je echte WEEX-stop is uitgevoerd.'))
            if target and ((islong and p>=target) or (not islong and p<=target)):
                alerts.append(('TARGET',f'Koersdoel {target} bereikt. Herbeoordeel winstneming en risico.'))
            if liq:
                distance=abs(p/liq-1)*100
                if distance<2:alerts.append(('LIQ',f'Prijs circa {distance:.2f}% van opgegeven liquidatieniveau {liq}. Hoog risico.'))
            if change<=-1.0:alerts.append(('LOSS',f'Koers beweegt {change:.2f}% tegen je positie. Met {t["leverage"]}x is de ruwe hefboombeweging circa {change*t["leverage"]:.1f}% exclusief kosten.'))
            if not alerts:continue
            priority={'LIQ':0,'STOP':1,'LOSS':2,'TARGET':3}
            kind,explanation=sorted(alerts,key=lambda x:priority[x[0]])[0]
            # One alert per type, at most once per 30 minutes; a different type can alert immediately.
            if t['last_alert']==kind and time.time()-t['last_alert_time']<1800:continue
            message=(f'WEEX TRADE MONITOR · {kind}\n{t["symbol"]} {side} · {t["leverage"]}x\n'
                     f'Entry {entry} · laatst beschikbare scannerprijs {p}\n{explanation}\n'
                     'Dit is geen live WEEX-markprijs of orderstatus. Niet automatisch uitbreiden; check margin, fees en stop eerst.')
            if send(t['chat_id'],message):
                c.execute('UPDATE trades SET last_alert=?,last_alert_time=? WHERE id=?',(kind,time.time(),t['id']))

def process_message(message):
    chat_id=message.get('chat',{}).get('id');cmd=(message.get('text') or '').split(' ')[0].split('@')[0].lower()
    if not chat_id:return
    with db() as c:
        if cmd=='/start':
            c.execute('INSERT INTO subscribers(chat_id,alerts) VALUES (?,1) ON CONFLICT(chat_id) DO UPDATE SET alerts=1',(chat_id,))
            send(chat_id,'WEEX Moon v7 verbonden!\n/status — scanner controleren\n/scan — setups\n/alerts_on en /alerts_off — meldingen\n/trades — je gevolgde posities\nOpen het Trade Assistant-dashboard om een positie toe te voegen.')
        elif cmd=='/whoami':
            send(chat_id,f'Je Telegram chat-ID is: {chat_id}\nGebruik dit in het WEEX Moon dashboard.')
        elif cmd=='/alerts_on':
            c.execute('INSERT INTO subscribers(chat_id,alerts) VALUES (?,1) ON CONFLICT(chat_id) DO UPDATE SET alerts=1',(chat_id,));send(chat_id,'Meldingen ingeschakeld.')
        elif cmd=='/alerts_off':
            c.execute('UPDATE subscribers SET alerts=0 WHERE chat_id=?',(chat_id,));send(chat_id,'Meldingen uitgeschakeld.')
        elif cmd=='/status':
            with lock: d=dict(cache)
            age=int(time.time()-d['updated']) if d['updated'] else -1
            send(chat_id,f'WEEX Moon v7\nScanner: {"OK" if age>=0 and age<180 else "NIET ACTUEEL"}\nLaatste update: {age}s geleden\nFout: {d["error"] or "geen"}')
        elif cmd=='/scan':
            with lock: rows=list(cache['rows'])
            rows=[r for r in rows if isinstance(r.get('score'),(int,float))]
            rows.sort(key=lambda r:r['score'],reverse=True)
            send(chat_id,'Beste setups:\n'+'\n'.join(f'{r.get("symbol")} {r.get("side")} {r.get("mode")}: {r["score"]}/100 ({r.get("status")})' for r in rows[:8]) if rows else 'Nog geen actuele scan.')
        elif cmd=='/trades':
            rows=c.execute('SELECT * FROM trades WHERE chat_id=? AND active=1',(chat_id,)).fetchall()
            send(chat_id,'Gevolgde trades:\n'+'\n'.join(f'#{t["id"]} {t["symbol"]} {t["side"]} entry {t["entry"]}, laatst {t["last_price"] or "?"}' for t in rows) if rows else 'Geen actieve trades.')

def telegram_loop():
    if not TOKEN:
        log.warning('TELEGRAM_BOT_TOKEN missing');return
    offset=0
    while True:
        try:
            resp=requests.get(API+'/getUpdates',params={'offset':offset,'timeout':20},timeout=30)
            resp.raise_for_status();payload=resp.json()
            if not payload.get('ok'):raise ValueError('Telegram getUpdates failed')
            for update in payload.get('result',[]):
                offset=max(offset,update['update_id']+1)
                if update.get('message'):process_message(update['message'])
        except Exception as exc:
            log.warning('Telegram polling: %s',str(exc)[:180]);time.sleep(5)

@app.get('/api/status')
def status():
    with lock: d=dict(cache)
    with db() as c:
        trades=[dict(t) for t in c.execute('SELECT * FROM trades WHERE active=1 ORDER BY id DESC').fetchall()]
        chats=[x['chat_id'] for x in c.execute('SELECT chat_id FROM subscribers').fetchall()]
    return jsonify(scanner={'updated':d['updated'],'error':d['error'],'prices':d['prices']},trades=trades,chat_ids=chats,telegram_configured=bool(TOKEN))

@app.post('/api/trades')
def add_trade():
    j=request.get_json(silent=True) or {}
    symbol=str(j.get('symbol','')).upper();side=str(j.get('side','')).upper()
    if symbol not in SYMBOLS or side not in ('LONG','SHORT'):return jsonify(error='Ongeldige munt of richting'),400
    try:
        chat=int(j['chat_id']);entry=float(j['entry']);lev=float(j['leverage'])
        size=float(j['size_usdt']) if j.get('size_usdt') else None
        stop=float(j['stop']) if j.get('stop') else None
        target=float(j['target']) if j.get('target') else None
        liq=float(j['liquidation']) if j.get('liquidation') else None
        if not(0<entry and 1<=lev<=200 and all(x is None or x>0 for x in (size,stop,target,liq))):raise ValueError()
    except (ValueError,TypeError,KeyError):return jsonify(error='Controleer entry, leverage en chat-ID'),400
    with db() as c:
        if not c.execute('SELECT 1 FROM subscribers WHERE chat_id=?',(chat,)).fetchone():return jsonify(error='Stuur eerst /start naar de bot en gebruik /whoami'),400
        cur=c.execute('INSERT INTO trades(chat_id,symbol,side,entry,leverage,size_usdt,stop,target,liquidation,created) VALUES (?,?,?,?,?,?,?,?,?,?)',(chat,symbol,side,entry,lev,size,stop,target,liq,time.time()))
        tid=cur.lastrowid
    send(chat,f'WEEX Moon volgt nu trade #{tid}: {symbol} {side}, entry {entry}, {lev}x. Alleen indicatieve marktmonitoring; geen WEEX-orderkoppeling.')
    return jsonify(ok=True,id=tid)

@app.post('/api/trades/<int:tid>/close')
def close_trade(tid):
    with db() as c:c.execute('UPDATE trades SET active=0 WHERE id=?',(tid,))
    return jsonify(ok=True)

@app.post('/api/screenshot')
def screenshot():
    file=request.files.get('image')
    if not file:return jsonify(error='Kies eerst een screenshot'),400
    raw=file.read(5000001)
    if len(raw)>5000000:return jsonify(error='Afbeelding maximaal 5 MB'),400
    try:
        img=Image.open(io.BytesIO(raw));img.verify()
    except Exception:return jsonify(error='Geen geldige afbeelding'),400
    return jsonify(ok=True,message='Screenshot gecontroleerd. De afbeelding wordt niet opgeslagen. Automatisch uitlezen ontbreekt nog: bevestig hieronder de positiegegevens handmatig.')

HTML='''<!doctype html><html lang="nl"><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>WEEX Moon Trade Assistant v7</title><style>body{background:#101626;color:#f4f6fb;font:16px system-ui;margin:0 auto;max-width:850px;padding:20px}section{background:#1d2a40;border:1px solid #354861;border-radius:18px;padding:18px;margin:14px 0}h1{font-size:27px}h2{font-size:19px}input,select,button{padding:12px;border-radius:10px;background:#2b3c58;color:#fff;border:1px solid #4b5c73;font:inherit;box-sizing:border-box}input,select{width:100%;margin:5px 0 13px}button{background:#fb805d;border:0;cursor:pointer;margin:5px 5px 5px 0}small,.muted{color:#aabbd0}.bad{color:#ffb2a5}.good{color:#8ce7bc}.item{padding:14px;border:1px solid #42536d;border-radius:12px;margin:9px 0}label{display:block;margin-top:6px}#app{display:block}</style><h1>WEEX Moon · Trade Assistant v7</h1><p class="muted">Scanner + Telegram + positie-monitor · geen automatische orders</p><div id="app"><section id="health">Verbinden...</section><section><h2>Screenshot of PNL uploaden</h2><input type="file" accept="image/*" id="photo"><button onclick="upload()">Upload screenshot</button><p id="uploadmsg" class="muted">Screenshot wordt niet opgeslagen. Gegevens moeten nog handmatig worden bevestigd.</p></section><section><h2>Mijn trade toevoegen</h2><p class="muted">Stuur /start naar @WeexMoonBot. Gebruik het chat-ID dat bij /whoami verschijnt.</p><label>Telegram chat-ID</label><input id="chat" type="number"><label>Munt</label><select id="symbol"><option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>XRPUSDT</option><option>DOGEUSDT</option><option>HBARUSDT</option><option>INJUSDT</option><option>UNIUSDT</option></select><label>Richting</label><select id="side"><option>LONG</option><option>SHORT</option></select><label>Entry</label><input id="entry" type="number" step="any"><label>Leverage</label><input id="leverage" type="number" value="10" min="1" max="200"><label>Positiewaarde USDT (optioneel)</label><input id="size_usdt" type="number" step="any"><label>Stop-loss (aanbevolen)</label><input id="stop" type="number" step="any"><label>Take-profit (optioneel)</label><input id="target" type="number" step="any"><label>Liquidatieprijs volgens WEEX (optioneel)</label><input id="liquidation" type="number" step="any"><button onclick="add()">Volg mijn trade</button><p id="addmsg"></p></section><section><h2>Gevolgde trades</h2><div id="trades"></div></section><section><h2>Over de waarschuwingen</h2><p>De app waarschuwt bij stop, doel, liquidatieniveau en ongunstige koersbeweging. Een waarschuwing om risico te herbeoordelen is geen opdracht om een positie te sluiten of uit te breiden.</p><small>Prijzen zijn indicatief op basis van de v5-scanner, geen realtime WEEX mark price. PNL, margin en fees worden niet uit je WEEX-account gelezen. Gratis Render kan slapen en de database op de lokale schijf kan bij een deploy verdwijnen.</small></section></div><script>const el=x=>document.getElementById(x);async function api(path,opts={}){let r=await fetch(path,{...opts,headers:{...(opts.headers||{})}});let j=await r.json();if(!r.ok)throw Error(j.error||'HTTP '+r.status);return j}async function refresh(){let d=await api('/api/status');let age=d.scanner.updated?Math.round(Date.now()/1000-d.scanner.updated):null;el('health').innerHTML='<b class="'+(age!==null&&age<180?'good':'bad')+'">Scanner: '+(age!==null&&age<180?'actueel':'niet actueel')+'</b><p>Laatste scan '+(age===null?'onbekend':age+' seconden geleden')+' · Telegram '+(d.telegram_configured?'ingesteld':'niet ingesteld')+'</p><small>'+ (d.scanner.error||'Geen scannerfout')+'</small>';el('trades').innerHTML=d.trades.map(t=>'<div class="item"><b>#'+t.id+' '+t.symbol+' '+t.side+' · '+t.leverage+'x</b><p>Entry '+t.entry+' · Laatste prijs '+(t.last_price||'wachten')+'</p><p>Stop '+(t.stop||'niet ingesteld')+' · Target '+(t.target||'niet ingesteld')+' · Liquidatie '+(t.liquidation||'niet opgegeven')+'</p><button onclick="closeTrade('+t.id+')">Stop volgen</button></div>').join('')||'Nog geen trades.'}async function upload(){let f=el('photo').files[0];if(!f)return;let fd=new FormData();fd.append('image',f);try{let d=await api('/api/screenshot',{method:'POST',body:fd});el('uploadmsg').textContent=d.message}catch(e){alert(e.message)}}async function add(){let j={};['chat','symbol','side','entry','leverage','size_usdt','stop','target','liquidation'].forEach(x=>j[x==='chat'?'chat_id':x]=el(x).value);try{let d=await api('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(j)});el('addmsg').textContent='Trade #'+d.id+' toegevoegd';await refresh()}catch(e){alert(e.message)}}async function closeTrade(id){if(!confirm('Stop deze trade volgen?'))return;await api('/api/trades/'+id+'/close',{method:'POST'});await refresh()}refresh().catch(()=>{});setInterval(()=>{refresh().catch(()=>{})},15000)</script></html>'''
@app.get('/')
def home():return render_template_string(HTML)

if __name__=='__main__':
    setup()
    threading.Thread(target=telegram_loop,daemon=True).start()
    threading.Thread(target=scan_loop,daemon=True).start()
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))


