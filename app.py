import os,time,threading,requests,math,statistics
from datetime import datetime,timezone
from collections import deque
from flask import Flask,jsonify,Response
app=Flask(__name__)
BASE=os.getenv("WEEX_CANDLES_URL","https://api-contract.weex.com/capi/v3/market/klines")
SYMS=os.getenv("SCAN_SYMBOLS","BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,HBARUSDT,INJUSDT,UNIUSDT").split(",")
MODES={"SCALP":("1m","5m","15m"),"INTRADAY":("15m","1h","4h")}
PERIOD={"1m":60,"5m":300,"15m":900,"1h":3600,"4h":14400}
SCAN_SEC=int(os.getenv("SCAN_INTERVAL_SECONDS","45"))
state={"symbols":{},"setups":[],"paper":[],"lastScan":None,"errors":[]}
lock=threading.RLock();seen=set();active={};history=deque(maxlen=300)
session=requests.Session()
def candles(symbol,interval,limit=125):
    r=session.get(BASE,params={"symbol":symbol,"interval":interval,"limit":limit},timeout=12)
    r.raise_for_status();j=r.json()
    if isinstance(j,dict):
        if j.get("code") not in (None,0,"0","00000","200"):
            raise ValueError("WEEX API: "+str(j.get("msg") or j.get("message") or j.get("code")))
        j=j.get("data",[])
    if not isinstance(j,list):raise ValueError("Unexpected candle format")
    out=[]
    for x in j:
        if isinstance(x,dict):
            t=int(x.get("timestamp",x.get("time",0)));o=float(x["open"]);h=float(x["high"]);l=float(x["low"]);c=float(x["close"]);v=float(x.get("volume",0))
        else:
            t=int(x[0]);o,h,l,c,v=map(float,x[1:6])
        if t<10000000000:t*=1000
        if not all(map(math.isfinite,(o,h,l,c,v))):continue
        out.append({"t":t,"o":o,"h":h,"l":l,"c":c,"v":v})
    out.sort(key=lambda x:x["t"])
    now=time.time()*1000
    return [x for x in out if x["t"]+PERIOD[interval]*1000<=now]
def ema(a,n):
    if len(a)<n:return None
    z=sum(a[:n])/n;k=2/(n+1)
    for v in a[n:]:z=v*k+z*(1-k)
    return z
def atr(a,n=14):
    if len(a)<n+1:return None
    tr=[max(a[i]["h"]-a[i]["l"],abs(a[i]["h"]-a[i-1]["c"]),abs(a[i]["l"]-a[i-1]["c"])) for i in range(1,len(a))]
    return sum(tr[-n:])/n
def trend(a):
    c=[x["c"] for x in a]
    e20=ema(c,20);e50=ema(c,50)
    return "UP" if e20>e50 else "DOWN"
def candidate(symbol,mode,data):
    fast,mid,slow=MODES[mode]
    a,b,c=data[fast],data[mid],data[slow]
    if min(len(a),len(b),len(c))<55:raise ValueError("Not enough closed candles")
    # No trading on stale historical data
    now=time.time()*1000
    if any(now-d[-1]["t"]>PERIOD[tf]*1000*3 for d,tf in ((a,fast),(b,mid),(c,slow))):
        raise ValueError("Stale candles")
    price=a[-1]["c"];volumes=[x["v"] for x in a[-21:-1]]
    ratio=a[-1]["v"]/(sum(volumes)/len(volumes)) if sum(volumes)>0 else 0
    highs=max(x["h"] for x in a[-21:-1]);lows=min(x["l"] for x in a[-21:-1])
    tmid=trend(b);tslow=trend(c);at=atr(a)
    direction="LONG" if price>highs and tmid==tslow=="UP" and ratio>=1.2 else "SHORT" if price<lows and tmid==tslow=="DOWN" and ratio>=1.2 else None
    if not direction:return None
    # The stop is set behind the breakout zone, with ATR as a minimum distance.
    stop=min(lows,price-1.5*at) if direction=="LONG" else max(highs,price+1.5*at)
    risk=abs(price-stop)
    if risk<=0 or risk/price<0.001 or risk/price>0.045:return None
    sign=1 if direction=="LONG" else -1
    return {"id":f"{symbol}:{mode}:{direction}:{a[-1]['t']}","symbol":symbol,"mode":mode,"side":direction,"entry":price,"stop":stop,"tp1":price+sign*2*risk,"tp2":price+sign*3*risk,"riskPct":round(100*risk/price,3),"rr1":2,"rr2":3,"volumeRatio":round(ratio,2),"trendMid":tmid,"trendHigh":tslow,"createdAt":int(time.time()),"candleAt":a[-1]["t"],"status":"PAPER_OPEN","reason":"Closed-candle breakout + aligned trends + volume >= 1.2x","source":"WEEX historical candles; not a live executable quote"}
def telegram(s):
    token=os.getenv("TELEGRAM_BOT_TOKEN");chat=os.getenv("TELEGRAM_CHAT_ID")
    if not(token and chat):return
    message=f"WEEX PAPER SIGNAL {s['mode']} {s['side']} {s['symbol']}\nEntry {s['entry']:.8g}\nSL {s['stop']:.8g}\nTP1 {s['tp1']:.8g}\nTP2 {s['tp2']:.8g}\nUnvalidated strategy. Not an order."
    session.post(f"https://api.telegram.org/bot{token}/sendMessage",json={"chat_id":chat,"text":message},timeout=10).raise_for_status()
def update_paper(symbol,bar):
    # Conservative candle-based simulation: if SL and TP hit within same candle, assume SL first.
    for key,s in list(active.items()):
        if s["symbol"]!=symbol or bar["t"]<=s["candleAt"]:continue
        long=s["side"]=="LONG"
        hit_sl=bar["l"]<=s["stop"] if long else bar["h"]>=s["stop"]
        hit_tp=bar["h"]>=s["tp1"] if long else bar["l"]<=s["tp1"]
        if hit_sl or hit_tp:
            s["status"]="PAPER_SL" if hit_sl else "PAPER_TP1"
            s["closedAt"]=int(time.time())
            # indicative net R with estimated roundtrip fees and slippage
            fee=float(os.getenv("ROUNDTRIP_COST_PCT","0.16"))/100
            s["netR"]=round((-1 if hit_sl else 2)-fee*s["entry"]/abs(s["entry"]-s["stop"]),3)
            history.append(dict(s));del active[key]
def worker():
    while True:
        errors=[];new=[]
        for symbol in SYMS:
            try:
                needed=set(x for m in MODES.values() for x in m)
                data={tf:candles(symbol,tf) for tf in needed}
                with lock:
                    state["symbols"][symbol]={"status":"OK","lastClosed":data["1m"][-1]["t"],"closed1mPrice":data["1m"][-1]["c"],"priceType":"last CLOSED 1m candle (not live mark/last price)","updatedAt":int(time.time())}
                    update_paper(symbol,data["1m"][-1])
                for mode in MODES:
                    s=candidate(symbol,mode,data)
                    if s and s["id"] not in seen:
                        with lock:
                            seen.add(s["id"]);active[s["id"]]=s;new.append(s)
                        try:telegram(s)
                        except Exception as e:errors.append(f"Telegram: {type(e).__name__}: {e}")
            except Exception as e:
                msg=f"{symbol}: {type(e).__name__}: {e}"
                errors.append(msg)
                with lock:state["symbols"][symbol]={"status":"ERROR","reason":msg,"updatedAt":int(time.time())}
        with lock:
            state["setups"]=sorted((dict(x) for x in active.values()),key=lambda x:x["createdAt"],reverse=True)
            state["paper"]=list(history)[-100:]
            state["lastScan"]=int(time.time());state["errors"]=errors
        time.sleep(SCAN_SEC)
@app.get("/api/status")
def status():
    with lock:
        trades=list(history)
        wins=[x for x in trades if x["netR"]>0]
        return jsonify({"symbols":state["symbols"],"setups":state["setups"],"paper":trades[-50:],"lastScan":state["lastScan"],"errors":state["errors"],"metrics":{"closed":len(trades),"winRate":round(100*len(wins)/len(trades),1) if trades else None,"netR":round(sum(x["netR"] for x in trades),2)},"disclaimer":"Experimental paper signals, no auto-orders. Paper results are indicative, not validated live executions. In-memory history resets on restart."})
@app.get("/api/signals")
def legacy():return status()
HTML=r"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WEEX Scanner v3</title><style>body{background:#0b1020;color:#f6f7fb;font:15px system-ui;margin:0;padding:18px}main{max-width:820px;margin:auto}h1{font-size:25px}h2{font-size:18px}.sub{color:#a8b4cc}.panel{background:#172037;border:1px solid #28344d;border-radius:15px;padding:16px;margin:12px 0}.ok{color:#64e0ad}.bad{color:#ff9a9a}.pill{background:#26324c;border-radius:8px;padding:5px 8px;display:inline-block;margin:3px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.label{font-size:12px;color:#a8b4cc}.value{font-size:17px;font-weight:700;overflow-wrap:anywhere}button{background:#293d65;color:white;border:0;border-radius:10px;padding:10px 14px;margin:5px 4px 5px 0}.active{background:#ff684d}small{color:#a8b4cc}#errors{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><main><h1>WEEX Mobile Scanner <span class="pill">v3 · PAPER</span></h1><p class="sub">Twee strategieën: scalping en intraday. Geen automatische orders.</p><div class="panel"><b id="health">Verbinden...</b><p id="clock" class="sub"></p><small>Prijzen zijn afgesloten 1m-candles, dus NIET realtime Last/Mark Price. Niet gebruiken als uitvoeringsprijs.</small></div><div><button class="active" onclick="setMode('ALL',this)">Alle setups</button><button onclick="setMode('SCALP',this)">Scalp</button><button onclick="setMode('INTRADAY',this)">Intraday</button></div><h2>Signalen op papier</h2><div id="signals" class="panel">Laden...</div><h2>Gevolgde markten</h2><div id="markets" class="panel"></div><h2>Papertrading-resultaten</h2><div id="metrics" class="panel"></div><h2>Datakwaliteit</h2><div id="errors" class="panel"></div><p class="sub">Signalen zijn experimenteel en niet gebacktest. Paperresultaten kunnen afwijken van echte fills door spread, slippage en funding. Met 200x Cross kan liquidatie optreden voordat je stop-loss wordt uitgevoerd.</p></main><script>let mode='ALL';function setMode(m,btn){mode=m;document.querySelectorAll('button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');draw()}let data=null;const fmt=n=>typeof n==='number'?n.toLocaleString('en-US',{maximumFractionDigits:7}):'—';function draw(){if(!data)return;let now=Date.now()/1000;document.getElementById('health').textContent=data.lastScan&&now-data.lastScan<180?'● Scanner actief':'● Scanner verouderd / start op';document.getElementById('health').className=data.lastScan&&now-data.lastScan<180?'ok':'bad';document.getElementById('clock').textContent='Laatste scan: '+(data.lastScan?new Date(data.lastScan*1000).toLocaleString():'nog niet beschikbaar');let a=data.setups.filter(x=>mode==='ALL'||x.mode===mode);document.getElementById('signals').innerHTML=a.length?a.map(x=>`<div class="panel"><b>${x.symbol} · ${x.mode} · ${x.side}</b><p class="sub">PAPER · ${new Date(x.createdAt*1000).toLocaleString()}</p><div class="grid">${[['Entry',x.entry],['Stop-loss',x.stop],['TP1 (2R)',x.tp1],['TP2 (3R)',x.tp2],['Risico %',x.riskPct],['Volume ratio',x.volumeRatio]].map(y=>`<div><div class="label">${y[0]}</div><div class="value">${fmt(y[1])}</div></div>`).join('')}</div><p><small>${x.reason}</small></p></div>`).join(''):'Geen nieuwe bevestigde setups. WAIT is normaal.';document.getElementById('markets').innerHTML=Object.entries(data.symbols).map(([k,v])=>`<div style="padding:8px 0;border-bottom:1px solid #28344d"><b>${k}</b> <span class="${v.status==='OK'?'ok':'bad'}">${v.status}</span><div class="sub">${v.status==='OK'?'Laatste gesloten 1m: '+fmt(v.closed1mPrice):v.reason}</div></div>`).join('');document.getElementById('metrics').innerHTML=`Gesloten papertrades: <b>${data.metrics.closed}</b> · Winrate: <b>${data.metrics.winRate===null?'Nog geen data':data.metrics.winRate+'%'}</b> · Netto R (geschat): <b>${data.metrics.netR}</b><p><small>Historie reset bij herstart. Geen statistisch bewijs van winstgevendheid.</small></p>`;document.getElementById('errors').textContent=data.errors.length?data.errors.join('\n'):'Geen API-fouten in de laatste scan.'}async function load(){try{let r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);data=await r.json();draw()}catch(e){document.getElementById('health').textContent='Verbinding mislukt: '+e.message;document.getElementById('health').className='bad'}}load();setInterval(load,15000)</script></body></html>"""
@app.get("/")
def home():return Response(HTML,mimetype="text/html")
_started=False
def start_worker():
    global _started
    if not _started:
        _started=True;threading.Thread(target=worker,daemon=True).start()
start_worker()
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")),debug=False)
