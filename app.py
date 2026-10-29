import os,time,threading,requests
from flask import Flask,jsonify,render_template,request
app=Flask(__name__)
BASE='https://api-contract.weex.com/capi/v3/market/klines'
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','INJUSDT','FETUSDT','UNIUSDT']
state={}; seen={}; lock=threading.Lock()
def candles(symbol,interval,limit=160):
 r=requests.get(BASE,params={'symbol':symbol,'interval':interval,'limit':limit},timeout=12);r.raise_for_status();data=r.json()
 if isinstance(data,dict): data=data.get('data',[])
 out=[{'t':int(x[0]),'o':float(x[1]),'h':float(x[2]),'l':float(x[3]),'c':float(x[4]),'v':float(x[5])} for x in data]
 out.sort(key=lambda x:x['t']);now=int(time.time()*1000);mins={'5m':5,'15m':15,'1h':60}[interval]
 return [x for x in out if x['t']+(mins*60000)<=now]
def ema(vals,n):
 a=2/(n+1);z=vals[0]
 for v in vals[1:]:z=a*v+(1-a)*z
 return z
def scan(symbol):
 a=candles(symbol,'5m');b=candles(symbol,'15m');h=candles(symbol,'1h')
 if min(map(len,[a,b,h]))<55:raise ValueError('Insufficient candles')
 price=a[-1]['c'];trend=lambda d: 'UP' if ema([x['c'] for x in d],20)>ema([x['c'] for x in d],50) else 'DOWN'
 tr15,tr1=trend(b),trend(h);prev=a[-21:-1];hi=max(x['h'] for x in prev);lo=min(x['l'] for x in prev)
 av=sum(x['v'] for x in prev)/len(prev);vol=a[-1]['v']/av if av else 0
 # breakout of prior 20 closed bars, require trend alignment and volume confirmation
 side='LONG' if price>hi and tr15==tr1=='UP' and vol>=1.2 else 'SHORT' if price<lo and tr15==tr1=='DOWN' and vol>=1.2 else None
 atr=sum(max(x['h']-x['l'],abs(x['h']-a[i-1]['c']),abs(x['l']-a[i-1]['c'])) for i,x in list(enumerate(a))[-14:])/14
 if side:
  entry=price;stop=min(lo,price-1.5*atr) if side=='LONG' else max(hi,price+1.5*atr)
  risk=abs(entry-stop);tp1=entry+(risk*2 if side=='LONG' else -risk*2);tp2=entry+(risk*3 if side=='LONG' else -risk*3)
  # no signal if stop distance too tight for meaningful execution
  if risk/entry<0.001:side=None
 else:entry=stop=tp1=tp2=None
 return {'symbol':symbol,'price':price,'status':'CONFIRMED' if side else 'WAIT','side':side,'entry':entry,'sl':stop,'tp1':tp1,'tp2':tp2,'trend15':tr15,'trend1h':tr1,'volumeRatio':round(vol,2),'at':int(time.time()),'candleAt':a[-1]['t'],'candles':a[-100:],'reason':'Breakout + aligned 15m/1h trends + volume' if side else 'No confirmed breakout with trend and volume alignment'}
def notify(s):
 token=os.getenv('TELEGRAM_BOT_TOKEN');chat=os.getenv('TELEGRAM_CHAT_ID')
 if not(token and chat):return
 msg=f"WEEX {s['side']} {s['symbol']}\nEntry {s['entry']:.6g}\nSL {s['sl']:.6g}\nTP1 {s['tp1']:.6g}\nTP2 {s['tp2']:.6g}\nUnvalidated signal. Cross margin / high leverage may liquidate before SL."
 requests.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':msg},timeout=10).raise_for_status()
def worker():
 while True:
  for symbol in SYMS:
   try:
    s=scan(symbol)
    with lock:state[symbol]=s
    if s['side']:
     key=(symbol,s['side'],s['candleAt'])
     if key not in seen:
      seen[key]=time.time()
      try:notify(s)
      except Exception as ex:print('Telegram error',ex,flush=True)
   except Exception as ex:
    with lock:state[symbol]={'symbol':symbol,'status':'ERROR','reason':str(ex),'at':int(time.time())}
  time.sleep(30)
@app.get('/')
def home():return render_template('index.html')
@app.get('/api/signals')
def signals():
 with lock:return jsonify({'signals':list(state.values()),'serverTime':int(time.time())})
threading.Thread(target=worker,daemon=True).start()
if __name__=='__main__':
 app.run(host='0.0.0.0',port=int(os.getenv('PORT',8080)),debug=False)
