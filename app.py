"""WEEX Moon scanner v2. REST polling, confirmed candles and separately labelled quotes."""
import os, time, math, threading, logging, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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
seen = set()
active = {}
history = []
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
            "quoteFresh":bool(age is not None and -15<=age<=90),
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
    if len(out)<55: raise ValueError(f"{tf}: te weinig gesloten candles ({len(out)})")
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
            "lastClosedPrice":closes[-1],"lastClosedAt":a[-1]["t"],
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
    # Reject stale/missing quote and excessive bid-ask spread.
    q=quote(symbol)
    if not q["quoteFresh"] or q["spreadPct"] is None or q["spreadPct"]>0.3:
        return None
    distance=atr(a)
    stop=min(low,p-1.5*distance) if side=="LONG" else max(high,p+1.5*distance)
    risk=abs(p-stop)
    if not .001<=risk/p<=.045:return None
    sign=1 if side=="LONG" else -1
    return {"id":f"{symbol}:{mode}:{side}:{a[-1]['t']}",
            "symbol":symbol,"mode":mode,"side":side,"entry":p,"stop":stop,
            "tp1":p+sign*2*risk,"tp2":p+sign*3*risk,
            "riskPct":round(100*risk/p,3),"volumeRatio":round(ratio,2),
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
    entry={"status":"OK" if usable else "PARTIAL","quote":q,"timeframes":info,
           "closed1mPrice":info.get("1m",{}).get("lastClosedPrice"),
           "lastClosed":info.get("1m",{}).get("lastClosedAt"),
           "updatedAt":int(time.time()),"reason":"; ".join(errors[:4]) if errors else None}
    setups=[]
    if usable:
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
    return jsonify(result)

@app.get("/api/quotes")
def quotes():
    with lock:
        return jsonify({"updated":state["quotesUpdated"],
                        "quotes":{s:v.get("quote") for s,v in state["symbols"].items()}})

@app.get("/")
def home():
    return Response("""<!doctype html><html lang="nl"><meta name="viewport" content="width=device-width,initial-scale=1">
    <meta charset="utf-8"><title>WEEX Moon v2</title>
    <style>body{background:#0e1424;color:#f5f7ff;font:15px system-ui;max-width:1000px;margin:auto;padding:22px}
    h1{font-size:32px}b{color:#fff}.muted{color:#a7b7d3}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
    .card{background:#1b2941;border:1px solid #34435d;border-radius:14px;padding:16px}
    .good{color:#68dfb0}.bad{color:#ff9a86}small{color:#a7b7d3}table{width:100%;border-collapse:collapse}
    td{padding:7px 3px;border-bottom:1px solid #34435d}summary{cursor:pointer}</style>
    <h1>WEEX <span style="color:#ff795a">MOON</span> v2</h1>
    <p class="muted">Live REST-quotes + gesloten candles. Geen automatische orders. Prijzen zijn indicatief.</p>
    <p id="status">Laden...</p><div id="cards" class="grid"></div>
    <h2>Bevestigde paper-setups</h2><div id="signals"></div>
    <script>
    const e=x=>String(x??'â').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const p=x=>x==null?'â':Number(x).toLocaleString('en-US',{maximumFractionDigits:8});
    async function load(){try{
      const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);
      const d=await r.json(),age=d.lastScan?Math.round(Date.now()/1000-d.lastScan):null;
      document.getElementById('status').textContent=`Laatste scan: ${age??'â'}s Â· Markten: ${Object.keys(d.symbols).length} Â· Fouten: ${d.errors.length}`;
      document.getElementById('cards').innerHTML=Object.entries(d.symbols).map(([s,v])=>{
       let q=v.quote||{},tf=v.timeframes||{};
       return `<div class="card"><b>${e(s)}</b> <span class="${v.status==='OK'?'good':'bad'}">${e(v.status)}</span>
       <p>Last: <b>${p(q.lastPrice)}</b> Â· Mark: <b>${p(q.markPrice)}</b></p>
       <p>Bid: ${p(q.bidPrice)} Â· Ask: ${p(q.askPrice)} Â· Spread: ${p(q.spreadPct)}%</p>
       <small>Quote: ${p(q.quoteAgeSec)}s oud Â· ${q.quoteFresh?'actueel':'verouderd / ontbreekt'}</small>
       <details><summary>Gesloten candles / trends</summary><table>${Object.entries(tf).map(([t,i])=>`<tr><td>${e(t)}</td><td>${e(i.trend)}</td><td>${p(i.lastClosedPrice)}</td></tr>`).join('')}</table></details>
       ${v.reason?`<p class="bad">${e(v.reason)}</p>`:''}</div>`}).join('');
      document.getElementById('signals').innerHTML=d.setups.length?d.setups.map(s=>`<div class="card">
       <b>${e(s.symbol)} ${e(s.side)} ${e(s.mode)}</b><p>Gesloten candle entry: ${p(s.entry)} Â· SL: ${p(s.stop)} Â· TP1: ${p(s.tp1)}</p>
       <small>${e(s.reason)} Â· geen uitvoerbare orderprijs</small></div>`).join(''):'Geen bevestigde setups. Wachten is normaal.';
    }catch(err){document.getElementById('status').textContent='Scannerfout: '+err.message}}
    load();setInterval(load,20000)</script></html>""",mimetype="text/html")

threading.Thread(target=worker,daemon=True).start()
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")),threaded=True)
