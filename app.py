"""WEEX Moon scanner v4. REST polling, confirmed candles and separately labelled quotes."""
import os, time, math, threading, logging, requests, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from flask import Flask, jsonify, Response

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
BASE = "https://api-contract.weex.com/capi/v3/market"
SYMBOLS = list(dict.fromkeys(s.strip().upper() for s in os.getenv(
    "SCAN_SYMBOLS",
    "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,HBARUSDT,INJUSDT,UNIUSDT,"
    "QNTUSDT,RENDERUSDT,RNDRUSDT,XAUUSDT,BNBUSDT,ADAUSDT,AVAXUSDT,"
    "LINKUSDT,SUIUSDT,NEARUSDT,LTCUSDT,APTUSDT,ARBUSDT,OPUSDT,TRXUSDT,PEPEUSDT"
).split(",") if s))
TIMEFRAMES = {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400}
MODES = {"SCALP":("1m","5m","15m","1h"),"INTRADAY":("15m","1h","4h","1d")}
INTERVAL = max(30, int(os.getenv("SCAN_INTERVAL_SECONDS","60")))
state = {"lastScan":None,"lastSuccessfulScan":None,"symbols":{},"setups":[],
         "errors":[],"quotesUpdated":None,"scanDuration":None,"timeframes":list(TIMEFRAMES)}
lock = threading.RLock()
session = requests.Session()
def load_seen():
    try:
        with db() as c:
            return {row[0] for row in c.execute("SELECT id FROM signal_history WHERE created_at > ?",(int(time.time())-86400,))}
    except Exception:
        logging.exception("Could not restore signal deduplication")
        return set()

seen = set()
active = {}
history = []
DB_PATH=os.getenv("SCANNER_DB_PATH","/tmp/weex_moon_signals.sqlite3")
MAX_SIGNAL_AGE_SEC=180
MAX_QUOTE_AGE_SEC=45

def db():
    c=sqlite3.connect(DB_PATH,timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS signal_history (id TEXT PRIMARY KEY, symbol TEXT, mode TEXT, side TEXT, candle_at INTEGER, created_at INTEGER, entry REAL, stop REAL, tp1 REAL, volume_ratio REAL, risk_pct REAL)")
    c.commit()
    return c

seen = load_seen()

def save_signal(signal):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO signal_history VALUES (?,?,?,?,?,?,?,?,?,?,?)",(signal["id"],signal["symbol"],signal["mode"],signal["side"],signal["candleAt"],signal["createdAt"],signal["entry"],signal["stop"],signal["tp1"],signal["volumeRatio"],signal["riskPct"]))

def recent_history(limit=50):
    with db() as c:
        rows=c.execute("SELECT id,symbol,mode,side,candle_at,created_at,entry,stop,tp1,volume_ratio,risk_pct FROM signal_history ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
    cols=["id","symbol","mode","side","candleAt","createdAt","entry","stop","tp1","volumeRatio","riskPct"]
    return [dict(zip(cols,row)) for row in rows]

cached_tickers = {}
cached_books = {}
last_quotes = 0

def get_json(path, params=None, timeout=10):
    r = session.get(BASE + path, params=params, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if isinstance(j,dict) and "code" in j and str(j["code"]) not in ("0","00000","200"):
        raise ValueError(str(j.get("msg") or j["code"]))
    return j.get("data",j) if isinstance(j,dict) else j

def as_rows(value):
    if isinstance(value,list): return value
    if isinstance(value,dict) and isinstance(value.get("data"),list): return value["data"]
    return []

def refresh_quotes():
    global cached_tickers, cached_books, last_quotes
    now=time.time()
    if now-last_quotes < 20: return
    tickers = as_rows(get_json("/ticker/24hr",timeout=18))
    books = as_rows(get_json("/ticker/bookTicker",timeout=18))
    if not tickers: raise ValueError("WEEX ticker-feed leeg")
    cached_tickers={x["symbol"]:x for x in tickers if isinstance(x,dict) and x.get("symbol")}
    cached_books={x["symbol"]:x for x in books if isinstance(x,dict) and x.get("symbol")}
    last_quotes=now

def number(x):
    try:
        f=float(x)
        return f if math.isfinite(f) and f>0 else None
    except (TypeError,ValueError): return None

def quote(symbol):
    t=cached_tickers.get(symbol,{})
    b=cached_books.get(symbol,{})
    bid=number(b.get("bidPrice"));ask=number(b.get("askPrice"))
    last=number(t.get("lastPrice"));mark=number(t.get("markPrice"))
    ts=int(b.get("time") or t.get("closeTime") or 0)
    if ts and ts < 10_000_000_000: ts*=1000
    age=(time.time()*1000-ts)/1000 if ts else None
    spread=round((ask-bid)/((ask+bid)/2)*100,4) if bid and ask and ask>=bid else None
    return {"lastPrice":last,"markPrice":mark,"indexPrice":number(t.get("indexPrice")),
            "bidPrice":bid,"askPrice":ask,"spreadPct":spread,"quoteTimestamp":ts or None,
            "quoteAgeSec":round(age,1) if age is not None else None,
            "quoteFresh":bool(age is not None and -15<=age<=MAX_QUOTE_AGE_SEC),
            "source":"WEEX futures REST; polled, not streaming"}

def candles(symbol,tf):
    raw=as_rows(get_json("/klines",{"symbol":symbol,"interval":tf,"limit":120},timeout=12))
    now=time.time()*1000
    out=[]
    for x in raw:
        try:
            ts=int(x[0])
            if ts<10_000_000_000:ts*=1000
            o,h,l,c,v=map(float,x[1:6])
            if all(map(math.isfinite,(o,h,l,c,v))) and ts+TIMEFRAMES[tf]*1000<=now:
                out.append({"t":ts,"o":o,"h":h,"l":l,"c":c,"v":v})
        except (TypeError,ValueError,IndexError,KeyError):continue
    out.sort(key=lambda x:x["t"])
    out=list({x["t"]:x for x in out}.values())
    out.sort(key=lambda x:x["t"])
    if len(out)<55: raise ValueError(f"{tf}: te weinig gesloten candles ({len(out)})")
    step=TIMEFRAMES[tf]*1000
    gaps=[out[i]["t"]-out[i-1]["t"] for i in range(max(1,len(out)-20),len(out))]
    if sum(g==step for g in gaps)<max(10,int(len(gaps)*.8)):
        raise ValueError(f"{tf}: onjuiste candle-intervallen")
    if out[-1]["t"]>now-step: raise ValueError(f"{tf}: nog open candle")
    if any(x["h"] < max(x["o"],x["c"]) or x["l"] > min(x["o"],x["c"]) or x["v"]<0 for x in out[-25:]):
        raise ValueError(f"{tf}: OHLCV inconsistent")
    age=(now-(out[-1]["t"]+TIMEFRAMES[tf]*1000))/1000
    if age>TIMEFRAMES[tf]*2.5:raise ValueError(f"{tf}: oude candles ({int(age)}s)")
    return out

def ema(a,n):
    z=sum(a[:n])/n;k=2/(n+1)
    for x in a[n:]:z=k*x+(1-k)*z
    return z

def atr(a,n=14):
    tr=[max(a[i]["h"]-a[i]["l"],abs(a[i]["h"]-a[i-1]["c"]),
            abs(a[i]["l"]-a[i-1]["c"])) for i in range(1,len(a))]
    return sum(tr[-n:])/n

def timeframe_info(a):
    closes=[x["c"] for x in a]
    e20,e50=ema(closes,20),ema(closes,50)
    return {"trend":"UP" if e20>e50 else "DOWN",
            "lastClosedPrice":closes[-1],"lastClosedAt":a[-1]["t"],"candleCount":len(a),
            "ema20":round(e20,9),"ema50":round(e50,9)}

def candidate(symbol,mode,data,info):
    fast,mid,slow,context=MODES[mode]
    a=data[fast]
    direction=info[mid]["trend"]
    if direction!=info[slow]["trend"] or direction!=info[context]["trend"]:
        return None
    p=a[-1]["c"]; prior=a[-21:-1]
    avg=sum(x["v"] for x in prior)/len(prior)
    ratio=a[-1]["v"]/avg if avg>0 else 0
    high=max(x["h"] for x in prior);low=min(x["l"] for x in prior)
    side="LONG" if direction=="UP" and p>high else "SHORT" if direction=="DOWN" and p<low else None
    if not side or ratio<1.2:return None
    # Only emit signals shortly after the actual triggering candle closes.
    candle_age=time.time()-(a[-1]["t"]/1000+TIMEFRAMES[fast])
    if candle_age<0 or candle_age>MAX_SIGNAL_AGE_SEC:return None
    # Reject stale/missing quote and excessive bid-ask spread.
    q=quote(symbol)
    if not q["quoteFresh"] or not q["lastPrice"] or not q["markPrice"] or q["spreadPct"] is None or q["spreadPct"]>0.3:
        return None
    distance=atr(a)
    stop=min(low,p-1.5*distance) if side=="LONG" else max(high,p+1.5*distance)
    risk=abs(p-stop)
    if not .001<=risk/p<=.045:return None
    if abs(q["lastPrice"]-p)/p>.008:return None
    if q["bidPrice"] is None or q["askPrice"] is None:return None
    # Very wide stops are not actionable for highly leveraged positions.
    if risk/p>.03:return None
    sign=1 if side=="LONG" else -1
    return {"id":f"{symbol}:{mode}:{side}:{a[-1]['t']}",
            "symbol":symbol,"mode":mode,"side":side,"entry":p,"stop":stop,
            "tp1":p+sign*2*risk,"tp2":p+sign*3*risk,
            "riskPct":round(100*risk/p,3),"volumeRatio":round(ratio,2),
            "riskWarning":"Bij hoge leverage kan liquidatie ruim voor de technische stop plaatsvinden. Gebruik eigen positie- en margincontrole.",
            "maxLeverageNotRecommended":True,
            "createdAt":int(time.time()),"candleAt":a[-1]["t"],
            "status":"PAPER_OPEN","reason":"Closed-candle breakout; 3 aligned higher timeframes; volume >=1.2x",
            "source":"WEEX confirmed candles; entry is NOT executable last/mark quote",
            "quote":q}

def scan_symbol(symbol):
    q=quote(symbol)
    if symbol not in cached_tickers:
        return symbol,{"status":"UNAVAILABLE","reason":"Contract niet gevonden in WEEX futures tickers",
                       "quote":q},[],[]
    data={};errors=[]
    for tf in TIMEFRAMES:
        try:data[tf]=candles(symbol,tf)
        except Exception as e:errors.append(f"{tf}: {str(e)[:100]}")
    info={tf:timeframe_info(a) for tf,a in data.items()}
    usable=all(tf in data for mode in MODES.values() for tf in mode)
    entry={"status":"OK" if usable and q["quoteFresh"] else "PARTIAL","quote":q,"timeframes":info,
           "closed1mPrice":info.get("1m",{}).get("lastClosedPrice"),
           "lastClosed":info.get("1m",{}).get("lastClosedAt"),
           "updatedAt":int(time.time()),"reason":"; ".join(errors[:4]) if errors else None}
    setups=[]
    if usable and q["quoteFresh"]:
        for mode in MODES:
            s=candidate(symbol,mode,data,info)
            if s:setups.append(s)
    return symbol,entry,setups,errors

def worker():
    while True:
        start=time.time()
        errors=[]
        try:
            refresh_quotes()
            rows={}
            signals=[]
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures={pool.submit(scan_symbol,s):s for s in SYMBOLS}
                for f in as_completed(futures):
                    sym=futures[f]
                    try:
                        symbol,info,found,problems=f.result()
                        rows[symbol]=info
                        signals.extend(found)
                        if problems:errors.append(f"{sym}: {'; '.join(problems[:2])}")
                    except Exception as e:
                        rows[sym]={"status":"ERROR","reason":str(e)[:200]}
                        errors.append(f"{sym}: {str(e)[:140]}")
            now=int(time.time())
            with lock:
                for s in signals:
                    if s["id"] not in seen:
                        seen.add(s["id"]);active[s["id"]]=s
                        try:save_signal(s)
                        except Exception:logging.exception("Signal journal write failed")
                # Active setups expire after 15 minutes to avoid displaying stale entries.
                active_copy=[s for s in active.values() if now-s["createdAt"]<900]
                active.clear()
                active.update({s["id"]:s for s in active_copy})
                state.update(symbols=rows,setups=sorted(active_copy,key=lambda s:s["createdAt"],reverse=True),
                             lastScan=now,quotesUpdated=last_quotes,errors=errors[:30],
                             scanDuration=round(time.time()-start,1))
                if any(x["status"]=="OK" for x in rows.values()):
                    state["lastSuccessfulScan"]=now
        except Exception as e:
            logging.exception("Scan failure")
            with lock:state["errors"]=[f"Scanner: {str(e)[:200]}"]
        time.sleep(max(5,INTERVAL-(time.time()-start)))

@app.get("/api/status")
def status():
    with lock:
        result=dict(state)
        result["symbols"]=dict(state["symbols"])
        result["setups"]=list(state["setups"])
        result["errors"]=list(state["errors"])
    result["metrics"]={"closed":0,"winRate":None,"netR":0}
    result["paper"]=[]
    result["scannerVersion"]="v4"
    return jsonify(result)

@app.get("/api/history")
def signal_history():
    try:return jsonify({"signals":recent_history(),"persistence":"ephemeral on Render unless persistent disk configured"})
    except Exception as e:return jsonify({"signals":[],"error":str(e)[:150]}),503

@app.get("/api/health")
def health():
    with lock:
        age=time.time()-state["lastScan"] if state["lastScan"] else None
        ok=sum(1 for v in state["symbols"].values() if v.get("status")=="OK")
    return jsonify({"version":"v4","scannerCurrent":bool(age is not None and 0<=age<180 and ok>0),"lastScanAgeSec":round(age,1) if age is not None else None,"marketsOK":ok,"telegramBridge":"separate service; verify with /test and /status"})

@app.get("/api/quotes")
def quotes():
    with lock:
        return jsonify({"updated":state["quotesUpdated"],
                        "quotes":{s:v.get("quote") for s,v in state["symbols"].items()}})

@app.get("/")
def home():
    return Response(r"""<!doctype html><html lang="nl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WEEX Moon v4</title>
<style>
:root{color-scheme:dark;--bg:#0b101c;--card:#19263a;--line:#30415b;--muted:#9caec6;--green:#6ee4b4;--red:#ff8d9d}
*{box-sizing:border-box}body{background:radial-gradient(ellipse at top,#20314b,var(--bg) 50%);color:#f4f7ff;font:14px system-ui;margin:0}
main{max-width:1120px;margin:auto;padding:26px 16px 70px}
header{display:flex;justify-content:space-between;align-items:center;gap:10px}
.brand{font-weight:900;font-size:25px;letter-spacing:-1px}.brand em{font-style:normal;color:#ff8066}
h1{font-size:30px;letter-spacing:-1px;margin:28px 0 5px}h2{margin:32px 0 15px}
.muted{color:var(--muted)}.pill{padding:8px 12px;border:1px solid var(--line);border-radius:30px;font-size:12px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:24px 0}
.stat,.card,.setup{border:1px solid var(--line);background:linear-gradient(135deg,#1b2b42,var(--card));border-radius:15px;padding:16px}
.stat small{display:block;color:var(--muted);font-size:11px}.stat strong{font-size:25px}
.tools{display:flex;gap:8px;margin-bottom:16px}input,select{min-width:0;background:#19263a;color:white;border:1px solid var(--line);border-radius:10px;padding:12px;font:inherit}
input{flex:1}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:12px}
.head{display:flex;justify-content:space-between;align-items:center}.head b{font-size:17px}
.tag{font-size:11px;border-radius:7px;padding:5px 8px;background:#38445a;color:#f5b69b}.tag.ok{color:var(--green);background:#184237}
.price{font-size:27px;font-weight:800;margin:16px 0 3px}.sub{font-size:12px;color:var(--muted)}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:16px 0}
.metric{background:#101b2d;border-radius:9px;padding:10px}.metric small{display:block;color:var(--muted);margin-bottom:5px}
.trends{display:flex;gap:4px;margin:10px 0}.tf{flex:1;text-align:center;background:#29374d;padding:7px 2px;border-radius:5px;font-size:10px}
.tf.up{background:#18463c;color:#7fe9bf}.tf.down{background:#492a39;color:#ff9dad}
details{border-top:1px solid var(--line);padding-top:12px;margin-top:15px}summary{cursor:pointer}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px}td,th{padding:8px 2px;text-align:left;border-bottom:1px solid #35435a}th{color:var(--muted)}
.error{color:#ffac9d;font-size:12px;overflow-wrap:anywhere}.empty{border:1px dashed var(--line);border-radius:13px;padding:20px;color:var(--muted)}
footer{color:var(--muted);font-size:12px;line-height:1.7;margin-top:35px}
@media(max-width:500px){.grid{grid-template-columns:1fr}.stat{padding:11px}.stat strong{font-size:21px}.brand{font-size:21px}}
</style>
<main><header><div class="brand">WEEX <em>MOON</em> v4</div><span class="pill" id="health">Verbinden...</span></header>
<h1>Market intelligence</h1><p class="muted">Koersen, gevalideerde gesloten candles en paper-setups. Geen automatische orders. Alleen actuele, bevestigde signalen.</p>
<div class="stats"><div class="stat"><small>MARKTEN OK</small><strong id="ok">—</strong></div><div class="stat"><small>SCAN LEEFTIJD</small><strong id="age">—</strong></div><div class="stat"><small>SETUPS</small><strong id="count">—</strong></div></div>
<div class="tools"><input id="search" placeholder="Zoek BTC, QNT, INJ..."><select id="filter"><option value="all">Alle</option><option value="ok">OK</option><option value="issues">Problemen</option></select></div>
<div id="markets" class="grid"><div class="empty">Laden...</div></div>
<h2>Bevestigde paper-setups</h2><p class="muted">Stopniveaus zijn technische referenties, geen liquidatiebescherming bij leverage.</p><div id="signals" class="empty">Wachten op bevestigde setups.</div>
<footer>Last Price = laatste gemelde transactie. Mark Price = futuresreferentie. Candle Close = bevestigde historische koers. REST-quotes zijn momentopnames, geen uitvoerbare handelsprijzen. Gebruik geen hoge leverage op basis van deze signalen.</footer></main>
<script>
const $=id=>document.getElementById(id),esc=x=>String(x??'—').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),num=x=>x==null?'—':Number(x).toLocaleString('en-US',{maximumFractionDigits:8});
const tfs=['1m','5m','15m','30m','1h','4h','1d'];let current=null;
function render(d){current=d;let all=Object.entries(d.symbols||{}),ok=all.filter(([s,v])=>v.status==='OK').length,age=d.lastScan?Math.max(0,Math.round(Date.now()/1000-d.lastScan)):null;
$('ok').textContent=ok+'/'+all.length;$('age').textContent=age==null?'—':age+'s';$('count').textContent=(d.setups||[]).length;
$('health').textContent=age!=null&&age<180&&ok>0?'● Actief':'● Controleren';$('health').style.color=age!=null&&age<180&&ok>0?'#6ee4b4':'#ffb69b';
let query=$('search').value.toUpperCase(),filter=$('filter').value;
all=all.filter(([s,v])=>s.includes(query)&&(filter==='all'||(filter==='ok'?v.status==='OK':v.status!=='OK'))).sort((a,b)=>(a[1].status==='OK'?0:1)-(b[1].status==='OK'?0:1)||a[0].localeCompare(b[0]));
$('markets').innerHTML=all.length?all.map(([s,v])=>{let q=v.quote||{},tf=v.timeframes||{};
return '<div class="card"><div class="head"><b>'+esc(s)+'</b><span class="tag '+(v.status==='OK'?'ok':'')+'">'+esc(v.status)+'</span></div>'+
'<div class="price">'+num(q.lastPrice)+'</div><div class="sub">Last Price · '+(q.quoteFresh?'quote '+num(q.quoteAgeSec)+'s oud':'quote niet actueel')+'</div>'+
'<div class="metrics"><div class="metric"><small>Mark Price</small><b>'+num(q.markPrice)+'</b></div><div class="metric"><small>Spread</small><b>'+num(q.spreadPct)+'%</b></div><div class="metric"><small>Bid</small><b>'+num(q.bidPrice)+'</b></div><div class="metric"><small>Ask</small><b>'+num(q.askPrice)+'</b></div></div>'+
'<div class="sub">TREND PER TIMEFRAME</div><div class="trends">'+tfs.map(t=>'<div class="tf '+(tf[t]?(tf[t].trend==='UP'?'up':'down'):'')+'">'+t+'<div>'+(tf[t]?(tf[t].trend==='UP'?'↑':'↓'):'·')+'</div></div>').join('')+'</div>'+
'<details><summary>Gesloten candles en tijden</summary><table><tr><th>TF</th><th>Trend</th><th>Close</th><th>UTC</th></tr>'+
tfs.map(t=>{let x=tf[t];return '<tr><td>'+t+'</td><td>'+esc(x?.trend)+'</td><td>'+num(x?.lastClosedPrice)+'</td><td>'+(x?.lastClosedAt?new Date(x.lastClosedAt).toLocaleTimeString('nl-NL',{timeZone:'UTC',hour:'2-digit',minute:'2-digit'}):'—')+'</td></tr>'}).join('')+'</table></details>'+
(v.reason?'<p class="error">'+esc(v.reason)+'</p>':'')+'</div>'}).join(''):'<div class="empty">Geen markten gevonden.</div>';
$('signals').className=d.setups?.length?'grid':'empty';$('signals').innerHTML=d.setups?.length?d.setups.map(s=>'<div class="setup"><b>'+esc(s.symbol)+' '+esc(s.side)+'</b><p>Entry '+num(s.entry)+' · SL '+num(s.stop)+' · TP1 '+num(s.tp1)+'</p><small>'+esc(s.reason)+'</small></div>').join(''):'Geen bevestigde setups; wachten is normaal.';
}
async function refresh(){try{let r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);render(await r.json())}catch(e){$('health').textContent='● Verbinding mislukt';$('health').style.color='#ff8d9d'}}
$('search').addEventListener('input',()=>current&&render(current));$('filter').addEventListener('change',()=>current&&render(current));refresh();setInterval(refresh,15000);
</script></html>""",mimetype="text/html")

threading.Thread(target=worker,daemon=True).start()
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),threaded=True)


