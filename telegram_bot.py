import os,time,json,sqlite3,threading,logging,requests,io
from flask import Flask,request,jsonify,render_template_string
from PIL import Image
app=Flask(__name__)
logging.basicConfig(level=logging.INFO)
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')
API='https://api.telegram.org/bot'+TOKEN
SCANNER=os.getenv('SCANNER_URL','https://weex-mobile-scanner-v5.onrender.com/api/status')
DB=os.getenv('TRADE_DB_PATH','/tmp/weex_v8.db')
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','HBARUSDT','INJUSDT','UNIUSDT')
lock=threading.RLock();cache={'updated':0,'rows':[],'prices':{},'error':'Scanner wordt geladen'}

def db():
 c=sqlite3.connect(DB,timeout=15);c.row_factory=sqlite3.Row;return c

def setup():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS subscribers(chat_id INTEGER PRIMARY KEY,alerts INTEGER DEFAULT 1)')
  c.execute('''CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER,symbol TEXT,side TEXT,entry REAL,leverage REAL,size_usdt REAL,stop REAL,target REAL,liquidation REAL,created REAL,active INTEGER DEFAULT 1,last_price REAL,last_alert TEXT,last_alert_time REAL DEFAULT 0)''')
  c.execute('CREATE TABLE IF NOT EXISTS signal_alerts(key TEXT PRIMARY KEY,created REAL)')
  cols={x[1] for x in c.execute('PRAGMA table_info(trades)').fetchall()}
  for name in ('margin_usdt','snapshot_pnl','snapshot_roi'):
   if name not in cols:c.execute(f'ALTER TABLE trades ADD COLUMN {name} REAL')

def send(chat,msg):
 if not TOKEN or not chat:return False
 try:
  r=requests.post(API+'/sendMessage',json={'chat_id':chat,'text':msg[:3900]},timeout=12);r.raise_for_status();return bool(r.json().get('ok'))
 except Exception as e:logging.warning('Telegram send error %s',str(e)[:110]);return False

def fetch_scanner():
 r=requests.get(SCANNER,timeout=20);r.raise_for_status();data=r.json();rows=data.get('rows',[])
 if not isinstance(rows,list):raise ValueError('Scanner rows ontbreken')
 updated=float(data.get('updated') or 0)
 if not updated or time.time()-updated>180:raise ValueError('Scannergegevens zijn verouderd')
 prices={}
 for row in rows:
  try:
   p=float(row.get('one') or row.get('price') or 0)
   if p>0:prices[row['symbol']]=p
  except (TypeError,ValueError,KeyError):pass
 if not prices:raise ValueError('Geen bruikbare prijzen')
 with lock:cache.update(updated=updated,rows=rows,prices=prices,error=None)
 return data

def monitor():
 with lock:prices=dict(cache['prices'])
 with db() as c:
  for t in c.execute('SELECT * FROM trades WHERE active=1').fetchall():
   p=prices.get(t['symbol'])
   if not p:continue
   c.execute('UPDATE trades SET last_price=? WHERE id=?',(p,t['id']))
   long=t['side']=='LONG';move=(p/t['entry']-1)*(1 if long else -1)*100
   alerts=[]
   if t['liquidation']:
    distance=abs(p/t['liquidation']-1)*100
    if distance<2:alerts.append(('LIQ',f'Prijs op circa {distance:.2f}% van opgegeven liquidatieniveau.'))
   if t['stop'] and ((long and p<=t['stop']) or (not long and p>=t['stop'])):alerts.append(('STOP','Opgegeven stopniveau bereikt. Controleer of je WEEX-order daadwerkelijk uitgevoerd is.'))
   if move<=-1:alerts.append(('LOSS',f'Ongunstige koersbeweging {move:.2f}% · ruwe hefboom-ROI {move*t["leverage"]:.1f}% (niet de WEEX ROI).'))
   if t['target'] and ((long and p>=t['target']) or (not long and p<=t['target'])):alerts.append(('TARGET','Koersdoel bereikt. Herbeoordeel je positie.'))
   if not alerts:continue
   kind,reason=alerts[0]
   if t['last_alert']==kind and time.time()-t['last_alert_time']<1800:continue
   msg=f'WEEX TRADE WAARSCHUWING · {kind}\n{t["symbol"]} {t["side"]} {t["leverage"]}x\nEntry {t["entry"]} · indicatieve koers {p}\n{reason}\nControleer WEEX mark price, margin en orders. Geen automatische sluiting of koopadvies.'
   if send(t['chat_id'],msg):c.execute('UPDATE trades SET last_alert=?,last_alert_time=? WHERE id=?',(kind,time.time(),t['id']))

def signal_alerts(data):
 with db() as c:
  subs=c.execute('SELECT chat_id FROM subscribers WHERE alerts=1').fetchall()
  if not subs:return
  for row in data['rows']:
   if row.get('status')!='PAPER SETUP' or float(row.get('score') or 0)<85:continue
   sym=row.get('symbol');mode=row.get('mode');side=row.get('side')
   if sym not in SYMBOLS or side not in ('LONG','SHORT'):continue
   bucket=int(float(data['updated'])//(300 if mode=='Scalp' else 900));key=f'{sym}:{side}:{mode}:{bucket}'
   if c.execute('SELECT 1 FROM signal_alerts WHERE key=?',(key,)).fetchone():continue
   msg=f'WEEX SCANNER · experimentele setup\n{sym} {side} · {mode}\nScore {row.get("score")}/100 · indicatieve koers {row.get("one")}\nStatus {row.get("status")}\nScore is geen winstkans; check de actuele chart.'
   if any(send(sub['chat_id'],msg) for sub in subs):c.execute('INSERT OR IGNORE INTO signal_alerts VALUES (?,?)',(key,time.time()))
  c.execute('DELETE FROM signal_alerts WHERE created<?',(time.time()-7*86400,))

def scan_loop():
 while True:
  try:
   data=fetch_scanner();monitor();signal_alerts(data)
  except Exception as e:
   with lock:cache['error']=str(e)[:180]
   logging.warning('Scanner error: %s',str(e)[:140])
  time.sleep(20)

def process_message(msg):
 chat=msg.get('chat',{}).get('id');cmd=(msg.get('text') or '').split(' ')[0].split('@')[0].lower()
 if not chat:return
 with db() as c:
  if cmd=='/start':
   c.execute('INSERT INTO subscribers VALUES (?,1) ON CONFLICT(chat_id) DO UPDATE SET alerts=1',(chat,))
   send(chat,'WEEX Moon v9 verbonden.\n/whoami — chat-ID\n/status — status\n/scan — beste setups\n/trades — posities\n/alerts_on /alerts_off — meldingen')
  elif cmd=='/whoami':send(chat,f'Je Telegram chat-ID: {chat}')
  elif cmd=='/alerts_on':
   c.execute('INSERT INTO subscribers VALUES (?,1) ON CONFLICT(chat_id) DO UPDATE SET alerts=1',(chat,));send(chat,'Meldingen aan')
  elif cmd=='/alerts_off':c.execute('UPDATE subscribers SET alerts=0 WHERE chat_id=?',(chat,));send(chat,'Meldingen uit')
  elif cmd=='/status':
   with lock:d=dict(cache)
   send(chat,f'Scanner: {"actueel" if d["updated"] and time.time()-d["updated"]<180 else "niet actueel"}\nFout: {d["error"] or "geen"}')
  elif cmd=='/scan':
   with lock:rows=list(cache['rows'])
   rows=[r for r in rows if float(r.get('score') or 0)>=50]
   rows.sort(key=lambda r:float(r.get('score') or 0),reverse=True)
   send(chat,'Beste setups:\n'+'\n'.join(f'{r.get("symbol")} {r.get("side")} {r.get("mode")}: {r.get("score")}/100 ({r.get("status")})' for r in rows[:8]))
  elif cmd=='/trades':
   rows=c.execute('SELECT * FROM trades WHERE chat_id=? AND active=1',(chat,)).fetchall()
   send(chat,'Gevolgde trades:\n'+'\n'.join(f'#{t["id"]} {t["symbol"]} {t["side"]} entry {t["entry"]} · prijs {t["last_price"] or "?"}' for t in rows) if rows else 'Geen actieve trades')

def telegram_loop():
 if not TOKEN:return
 offset=0
 while True:
  try:
   r=requests.get(API+'/getUpdates',params={'offset':offset,'timeout':20},timeout=30);r.raise_for_status()
   for update in r.json().get('result',[]):
    offset=max(offset,update['update_id']+1)
    if update.get('message'):process_message(update['message'])
  except Exception as e:logging.warning('Telegram polling error %s',str(e)[:110]);time.sleep(5)

@app.get('/api/status')
def status():
 with lock:d=dict(cache)
 with db() as c:
  trades=[dict(t) for t in c.execute('SELECT * FROM trades WHERE active=1 ORDER BY id DESC').fetchall()]
  chats=[x['chat_id'] for x in c.execute('SELECT chat_id FROM subscribers').fetchall()]
 return jsonify(scanner={'updated':d['updated'],'error':d['error'],'prices':d['prices'],'rows':d['rows']},trades=trades,chat_ids=chats,telegram_configured=bool(TOKEN))

@app.post('/api/screenshot')
def screenshot():
 f=request.files.get('image')
 if not f:return jsonify(error='Selecteer een screenshot'),400
 raw=f.read(7*1024*1024)
 if len(raw)>6*1024*1024:return jsonify(error='Bestand te groot (max 6 MB)'),413
 try:
  im=Image.open(io.BytesIO(raw));im.verify()
 except Exception:return jsonify(error='Geen geldige afbeelding'),400
 return jsonify(ok=True,message='Afbeelding gecontroleerd. De herkenning gebeurt lokaal in je browser; bevestig de gegevens voor opslaan.')

@app.post('/api/trades')
def add_trade():
 j=request.get_json(silent=True) or {};sym=str(j.get('symbol','')).upper();side=str(j.get('side','')).upper()
 if sym not in SYMBOLS or side not in ('LONG','SHORT'):return jsonify(error='Ongeldige munt of richting'),400
 try:
  chat=int(j['chat_id']);entry=float(j['entry']);lev=float(j['leverage'])
  size=float(j['size_usdt']) if j.get('size_usdt') else None
  stop=float(j['stop']) if j.get('stop') else None
  target=float(j['target']) if j.get('target') else None
  liq=float(j['liquidation']) if j.get('liquidation') else None
  margin=float(j['margin_usdt']) if j.get('margin_usdt') else None
  snapshot_pnl=float(j['snapshot_pnl']) if j.get('snapshot_pnl') else None
  snapshot_roi=float(j['snapshot_roi']) if j.get('snapshot_roi') else None
  if not(0<entry and 1<=lev<=200 and all(x is None or x>0 for x in (size,stop,target,liq,margin))):raise ValueError()
 except (ValueError,TypeError,KeyError):return jsonify(error='Controleer entry, leverage en chat-ID'),400
 with db() as c:
  if not c.execute('SELECT 1 FROM subscribers WHERE chat_id=?',(chat,)).fetchone():return jsonify(error='Stuur eerst /start en /whoami naar de Telegram-bot'),400
  cur=c.execute('INSERT INTO trades(chat_id,symbol,side,entry,leverage,size_usdt,stop,target,liquidation,created,margin_usdt,snapshot_pnl,snapshot_roi) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(chat,sym,side,entry,lev,size,stop,target,liq,time.time(),margin,snapshot_pnl,snapshot_roi))
  return jsonify(ok=True,id=cur.lastrowid)

@app.post('/api/trades/<int:trade_id>/close')
def close_trade(trade_id):
 with db() as c:c.execute('UPDATE trades SET active=0 WHERE id=?',(trade_id,))
 return jsonify(ok=True)

HTML=r'''<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WEEX Moon v9</title><script src="https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js"></script><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0a1020;color:#eaf1ff;font:15px system-ui,sans-serif}main{max-width:1100px;margin:auto;padding:20px}h1{font-size:29px;margin:0}h2{font-size:20px;margin:0 0 12px}h3{margin:6px 0}p{color:#aab8d2}header{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0 24px}.tag{padding:7px 11px;background:#1c2b45;border-radius:20px;color:#95e4bf}section{background:#141f34;border:1px solid #2d3c59;border-radius:18px;padding:20px;margin:15px 0}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(225px,1fr));gap:10px}.card{background:#1c2b45;border:1px solid #344766;border-radius:14px;padding:14px}.score{font-size:27px;font-weight:800}.up{color:#6ce6ad}.down{color:#ff9b9b}.muted{color:#aab8d2;font-size:13px}input,select,button{padding:12px;border-radius:10px;border:1px solid #465774;background:#22314d;color:#fff;font:inherit;width:100%}button{width:auto;background:#ff7554;border:0;font-weight:700;cursor:pointer}label{font-size:13px;color:#b5c4dc;display:block;margin-top:12px}label input,label select{margin-top:5px}.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0 12px}.bad{color:#ffaaaa}.good{color:#85e7bb}.line{border-top:1px solid #344766;margin:15px 0}.preview{max-width:100%;max-height:240px;border-radius:10px;display:none}.pill{padding:4px 9px;border-radius:10px;background:#2a3b57;font-size:12px}.notice{background:#26324b;padding:12px;border-radius:10px}#ocrStatus{margin-top:10px}small{color:#aab8d2}@media(max-width:550px){main{padding:12px}section{padding:15px}}

.heroStats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:12px 0}.stat{background:#1b2a43;border:1px solid #344966;border-radius:13px;padding:12px}.stat b{display:block;font-size:23px;margin-top:5px}.signal{position:relative;overflow:hidden}.scorebar{height:7px;background:#34445d;border-radius:20px;overflow:hidden;margin:10px 0}.scorebar i{display:block;height:100%;background:linear-gradient(90deg,#ff8768,#ffc67a,#6de6ae);border-radius:20px}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:12px}.metric{background:#152239;border-radius:10px;padding:10px}.metric small{display:block;font-size:11px}.metric strong{display:block;font-size:14px;margin-top:4px}.detail{margin-top:12px;border-top:1px solid #344766;padding-top:10px}.detail summary{cursor:pointer;color:#ffb59d;font-weight:600}.progress{display:flex;justify-content:space-between;gap:5px;align-items:center}.progress .score{font-size:24px}.roi{font-size:29px;font-weight:800}.risk{border-left:3px solid #ffb36b;padding-left:10px;margin-top:10px}.tradegrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.tradegrid .metric{min-width:0}.tradegrid strong{overflow-wrap:anywhere}.hint{font-size:12px;color:#9eafca}.kpi{font-weight:700}.filters{display:flex;gap:8px;flex-wrap:wrap}.filters select{width:auto;max-width:100%}@media(max-width:550px){.heroStats{grid-template-columns:repeat(3,minmax(0,1fr))}.stat{padding:9px}.stat b{font-size:18px}.tradegrid{grid-template-columns:repeat(2,minmax(0,1fr))}.metrics{gap:6px}.metric{padding:8px}}
</style></head><body><main><header><div><h1>WEEX <span style="color:#ff7554">MOON</span> v9</h1><div class="muted">Live scanner · Screenshot herkenning · Trade monitor</div></div><div id="health" class="tag">Verbinden...</div></header>
<section><div class="row" style="justify-content:space-between"><h2>Markt scanner</h2><div class="row"><select id="mode" style="width:auto" onchange="draw()"><option value="all">Alle setups</option><option>Scalp</option><option>Intraday</option></select><select id="sideFilter" style="width:auto" onchange="draw()"><option value="all">LONG + SHORT</option><option>LONG</option><option>SHORT</option></select></div></div><p>Alleen setups vanaf 50/100. Scores zijn experimenteel, geen winstkans. Open details voor de onderbouwing.</p><div id="overview" class="heroStats"></div><div id="signals" class="grid">Scanner laden...</div></section>
<section><h2>Screenshot → positie herkennen</h2><p>Upload je WEEX screenshot. De browser probeert de positiegegevens automatisch uit te lezen; controleer ze voordat je opslaat. De huidige koers wordt uit de scanner gehaald.</p><input id="photo" type="file" accept="image/*" onchange="recognize()"><div class="row" style="margin-top:12px"><img id="preview" class="preview" alt="Screenshot voorbeeld"><div style="flex:1;min-width:180px"><div id="ocrStatus" class="muted">Nog geen afbeelding gekozen.</div><div id="recognized" class="notice" style="display:none;margin-top:8px"></div></div></div><div class="line"></div><h3>Controleer je trade</h3><div class="form"><label>Telegram chat-ID<input id="chat" inputmode="numeric" placeholder="/whoami bij @WeexMoonBot"></label><label>Munt<select id="symbol"></select></label><label>Richting<select id="side"><option>LONG</option><option>SHORT</option></select></label><label>Entryprijs<input id="entry" type="number" step="any" placeholder="Uit screenshot"></label><label>Huidige koers (scanner)<input id="current" readonly placeholder="Wachten op scan"></label><label>Leverage<input id="leverage" type="number" min="1" max="200" value="10"></label><label>Positiewaarde USDT<input id="size_usdt" type="number" step="any" placeholder="Uit screenshot"></label><label>Margin USDT<input id="margin_usdt" type="number" step="any" placeholder="Uit screenshot"></label><label>Screenshot PNL (USDT)<input id="snapshot_pnl" type="number" step="any" placeholder="Bij upload"></label><label>Screenshot ROI (%)<input id="snapshot_roi" type="number" step="any" placeholder="Bij upload"></label><label>Stop-loss<input id="stop" type="number" step="any" placeholder="Optioneel"></label><label>Take-profit<input id="target" type="number" step="any" placeholder="Optioneel"></label><label>Liquidatieprijs<input id="liquidation" type="number" step="any" placeholder="Optioneel"></label></div><div id="pnl" class="notice" style="margin:15px 0">Hier verschijnen koersbeweging, hefboom-ROI en indicatieve PNL.</div><button onclick="addTrade()">Bevestig en volg trade</button><div id="addmsg" style="margin-top:10px"></div><p class="muted">Stuur /start en /whoami naar <a style="color:#ffb299" href="https://t.me/WeexMoonBot">@WeexMoonBot</a> voor je chat-ID. Zonder gekoppeld WEEX-account is de scannerprijs niet hetzelfde als mark price of actuele PNL.</p></section>
<section><h2>Mijn gevolgde posities</h2><div id="trades">Nog geen trades.</div></section><section><h2>Meldingen</h2><p>Telegram waarschuwt bij opgegeven stop, koersdoel, liquidatieafstand en ongunstige koersbeweging. Geen automatische orders, en geen betrouwbaar advies om bij te kopen. Verifieer de WEEX-positie zelf.</p><small>Deze publieke website heeft geen login: iedereen met de link kan posities bekijken en wijzigen. Gratis Render kan slapen; lokale database kan bij een nieuwe deploy verdwijnen. Gebruik altijd een echte stop-loss op de beurs.</small></section>
</main><script>
const $=id=>document.getElementById(id);const SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','HBARUSDT','INJUSDT','UNIUSDT'];$('symbol').innerHTML=SYMBOLS.map(x=>'<option>'+x+'</option>').join('');let state={scanner:{rows:[],prices:{},updated:0},trades:[]};
const fmt=n=>{n=Number(n);return Number.isFinite(n)?n.toLocaleString('en-US',{maximumFractionDigits:8}):'—'};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,opts={}){const r=await fetch(path,{...opts,cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'HTTP '+r.status);return d}
function safeNum(v){let n=Number(v);return v===null||v===undefined||v===''||!Number.isFinite(n)?null:n}
function signed(v,d=2){return v===null?'—':(v>0?'+':'')+v.toFixed(d)+'%'}
function getField(r,keys){for(let k of keys){let v=r[k];if(v!==undefined&&v!==null&&v!==''&&v!==false)return v}return null}
function metric(label,value){return `<div class="metric"><small>${esc(label)}</small><strong>${esc(value??'Niet beschikbaar')}</strong></div>`}
function draw(){let rows=(state.scanner.rows||[]).filter(r=>Number(r.score)>=50);const m=$('mode').value,side=$('sideFilter').value;rows=rows.filter(r=>(m==='all'||r.mode===m)&&(side==='all'||r.side===side));rows.sort((a,b)=>(Number(b.score)||0)-(Number(a.score)||0));let ready=rows.filter(r=>r.status==='PAPER SETUP').length;
$('overview').innerHTML=`<div class="stat"><small>Setups ≥50</small><b>${rows.length}</b></div><div class="stat"><small>Bevestigd*</small><b>${ready}</b></div><div class="stat"><small>Hoogste score</small><b>${rows.length?Math.round(Number(rows[0].score)):'—'}</b></div>`;
$('signals').innerHTML=rows.length?rows.map(r=>{let price=safeNum(r.one??r.price),level=safeNum(r.level),distance=price&&level?(level/price-1)*100:null;let volume=getField(r,['volume_ratio','volumeRatio','rel_volume','volume','vol_ratio']);let trend=getField(r,['trend','trend_label','trend_dir','ema_trend']);let momentum=getField(r,['momentum','rsi','rsi14']);let timeframe=getField(r,['timeframe','tf','interval']);let ready=r.status==='PAPER SETUP';let score=Math.max(0,Math.min(100,Number(r.score)||0));let context=ready?'Setup volgens scanner bevestigd — controleer prijs en candle.':r.status==='BIJNA'?'Nog geen bevestigde entry. Wacht op trigger.':'Geen bevestigde entry. Niet najagen.';return `<div class="card signal"><div class="progress"><b>${esc(r.symbol)}</b><span class="pill ${r.side==='LONG'?'up':'down'}">${esc(r.side)}</span></div><div class="progress" style="margin-top:10px"><div class="score">${Math.round(score)}<span class="muted">/100</span></div><span class="pill">${esc(r.mode)} · ${esc(r.status)}</span></div><div class="scorebar"><i style="width:${score}%"></i></div><div class="metrics">${metric('Huidige scannerkoers',fmt(price))}${metric('Breakoutniveau',fmt(level))}${metric('Afstand tot breakout',signed(distance))}${metric('Timeframe',timeframe?String(timeframe):String(r.mode||'—'))}</div><div class="risk muted">${esc(context)}</div><details class="detail"><summary>Waarom deze score? · Meer marktinfo</summary><div class="metrics">${metric('Trend',trend??'Niet aangeleverd')}${metric('Momentum / RSI',momentum??'Niet aangeleverd')}${metric('Volume-indicator',volume??'Niet aangeleverd')}${metric('Signaalstatus',r.status??'—')}</div><p class="hint">De v5-scanner levert niet voor ieder signaal afzonderlijke indicatorwaarden. Ontbrekende waarden worden niet verzonnen. Een score is geen kans op winst.</p></details></div>`}).join(''):'Geen setups vanaf 50/100 voor deze filters.';
$('trades').innerHTML=state.trades.length?state.trades.map(t=>{let p=safeNum(state.scanner.prices[t.symbol]||t.last_price),entry=Number(t.entry),lev=Number(t.leverage),move=p&&entry?((p/entry-1)*(t.side==='LONG'?1:-1)*100):null,roi=move===null?null:move*lev;let notional=safeNum(t.size_usdt),margin=safeNum(t.margin_usdt),estimatedPnl=move===null?null:(notional?notional*move/100:margin?margin*roi/100:null);let liq=safeNum(t.liquidation),liqDist=liq&&p?Math.abs((p/liq-1)*100):null;return `<div class="card" style="margin-bottom:12px"><div class="row" style="justify-content:space-between"><b>#${t.id} ${esc(t.symbol)} ${esc(t.side)} · ${fmt(lev)}x</b><button onclick="closeTrade(${t.id})">Stop volgen</button></div><div class="tradegrid" style="margin-top:14px">${metric('Koersbeweging',`<span>${signed(move)}</span>`)}${metric('Hefboom-ROI (schatting)',signed(roi))}${metric('PNL USDT (schatting)',estimatedPnl===null?'—':(estimatedPnl>0?'+':'')+fmt(estimatedPnl))}${metric('Entry',fmt(entry))}${metric('Scannerkoers',fmt(p))}${metric('Liquidatieafstand',signed(liqDist))}</div><div class="muted" style="margin-top:12px">Stop ${fmt(t.stop)} · Target ${fmt(t.target)} · Liquidatie ${fmt(liq)}</div>${t.snapshot_pnl!==null&&t.snapshot_pnl!==undefined?`<div class="notice" style="margin-top:10px">Bij screenshot: WEEX PNL ${fmt(t.snapshot_pnl)} USDT · ROI ${t.snapshot_roi===null?'—':signed(Number(t.snapshot_roi))}. Dit zijn momentopnames.</div>`:''}<p class="hint">Hefboom-ROI ≈ koersbeweging × ${fmt(lev)}. PNL is alleen een schatting op basis van positiewaarde of margin; exclusief kosten, funding en veranderende margin. Scannerkoers ≠ WEEX mark price.</p></div>`}).join(''):'Nog geen posities.';}
async function refresh(){try{const d=await api('/api/status');state=d;let age=d.scanner.updated?Math.round(Date.now()/1000-d.scanner.updated):null;$('health').textContent=age!==null&&age<180?'Scanner live · '+age+'s':'Scanner niet actueel';$('health').className=age!==null&&age<180?'tag good':'tag bad';draw();updatePrice()}catch(e){$('health').textContent='Scanner fout';$('signals').textContent=e.message}}
function updatePrice(){let p=safeNum(state.scanner.prices[$('symbol').value]);$('current').value=p||'';let entry=safeNum($('entry').value),lev=safeNum($('leverage').value),side=$('side').value,notional=safeNum($('size_usdt').value),margin=safeNum($('margin_usdt').value);if(p&&entry>0&&lev){let move=(p/entry-1)*(side==='LONG'?1:-1)*100,roi=move*lev,estimate=notional?notional*move/100:margin?margin*roi/100:null;$('pnl').innerHTML=`<b>Koersbeweging ${signed(move)} · hefboom-ROI ${signed(roi)}</b><div style="margin-top:6px">Geschatte PNL: ${estimate===null?'vul positiewaarde of margin in':fmt(estimate)+' USDT'}</div><small>Geen live WEEX PNL. Exclusief fees, funding, marginwijzigingen en slippage.</small>`}else $('pnl').textContent='Huidige scannerprijs of entry ontbreekt.'}
['symbol','side','entry','leverage','size_usdt','margin_usdt'].forEach(x=>$(x).addEventListener('change',updatePrice));$('entry').addEventListener('input',updatePrice);
function extract(text){let t=text.toUpperCase().replace(/,/g,'');let result={};let sym=t.match(/\b(BTC|ETH|SOL|XRP|DOGE|HBAR|INJ|UNI)\s*[/\-]?\s*(USDT|USD)\b/);if(sym)result.symbol=sym[1]+'USDT';let side=t.match(/\b(LONG|SHORT)\b/);if(side)result.side=side[1];let lev=t.match(/\b(\d{1,3})\s*X\b/);if(lev)result.leverage=Number(lev[1]);let lines=t.split(/\n/).map(x=>x.trim());function numberAfter(labels){for(let i=0;i<lines.length;i++){if(labels.some(w=>lines[i].includes(w))){let rest=lines[i].replace(/^.*?(?:PRICE|PNL|P&L|USDT|ROI|RATIO|AMOUNT|MARGIN)/,'');let match=rest.match(/[+\-]?\d+(?:\.\d+)?/);if(match&&Number(match[0])!==0)return Number(match[0]);for(let j=i+1;j<=Math.min(i+2,lines.length-1);j++){let n=lines[j].match(/^[+\-]?\d+(?:\.\d+)?/);if(n)return Number(n[0])}}}return null}
result.entry=numberAfter(['ENTRY PRICE','AVG. ENTRY','AVG ENTRY','AVERAGE ENTRY','OPEN PRICE']);result.liquidation=numberAfter(['LIQ. PRICE','LIQ PRICE','LIQUIDATION PRICE']);result.margin_usdt=numberAfter(['MARGIN (USDT)','MARGIN USDT']);result.size_usdt=numberAfter(['AMOUNT (USDT)','AMOUNT USDT']);result.snapshot_pnl=numberAfter(['UNREALIZED PNL','UNREALIZED P&L']);result.snapshot_roi=numberAfter(['ROI']);return result}
async function recognize(){let f=$('photo').files[0];if(!f)return;$('recognized').style.display='none';$('ocrStatus').textContent='Screenshot analyseren...';let url=URL.createObjectURL(f);$('preview').src=url;$('preview').style.display='block';try{let r=await Tesseract.recognize(f,'eng',{logger:m=>{if(m.status==='recognizing text')$('ocrStatus').textContent='Tekst herkennen '+Math.round(m.progress*100)+'%'}});let found=extract(r.data.text||'');for(let k of ['symbol','side','entry','leverage','liquidation','margin_usdt','size_usdt','snapshot_pnl','snapshot_roi'])if(found[k]!==undefined&&found[k]!==null&&$(k))$(k).value=found[k];updatePrice();let names=Object.keys(found).filter(k=>found[k]!==null);$('ocrStatus').textContent=names.length?'Herkenning klaar. Controleer alle velden.':'Geen bruikbare velden herkend. Vul gegevens handmatig in.';$('recognized').style.display='block';$('recognized').textContent=names.map(k=>k+': '+found[k]).join(' · ')||'OCR kon geen positiegegevens herkennen. Probeer een scherpere screenshot.';}catch(e){$('ocrStatus').textContent='Automatische herkenning niet beschikbaar: '+e.message+'. Vul velden handmatig in.'}}
async function addTrade(){let body={};['chat','symbol','side','entry','leverage','size_usdt','margin_usdt','snapshot_pnl','snapshot_roi','stop','target','liquidation'].forEach(k=>body[k==='chat'?'chat_id':k]=$(k).value);if(!confirm('Heb je munt, richting, entry en leverage gecontroleerd?'))return;try{let d=await api('/api/trades',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});$('addmsg').textContent='Trade #'+d.id+' wordt gevolgd.';await refresh()}catch(e){$('addmsg').textContent=e.message}}
async function closeTrade(id){if(!confirm('Stop trade volgen?'))return;try{await api('/api/trades/'+id+'/close',{method:'POST'});await refresh()}catch(e){alert(e.message)}}
refresh();setInterval(refresh,20000);
</script></body></html>'''
@app.get('/')
def home():return render_template_string(HTML)
@app.get('/health')
def health():return jsonify(ok=True,version='v8',scanner_updated=cache['updated'])
if __name__=='__main__':
 setup();threading.Thread(target=telegram_loop,daemon=True).start();threading.Thread(target=scan_loop,daemon=True).start();app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
