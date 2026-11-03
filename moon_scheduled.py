import os,json,time,math,urllib.request,urllib.parse
from pathlib import Path
BASE='https://api-contract.weex.com/capi/v3/market'
SYMS=os.getenv('MOON_SYMBOLS','BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,HBARUSDT,UNIUSDT,INJUSDT,TRXUSDT,APTUSDT,QNTUSDT,ARBUSDT,OPUSDT,SUIUSDT,NEARUSDT,ADAUSDT').split(',')
TFS={'5m':300,'15m':900,'1h':3600}
def api(path,params=None):
 url=BASE+path+('?' + urllib.parse.urlencode(params) if params else '')
 for attempt in range(3):
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'WEEXMoon/7'}),timeout=18) as r: j=json.load(r)
   if isinstance(j,dict):
    if str(j.get('code','0')) not in ('0','00000','200'):raise ValueError(str(j.get('msg','API error')))
    return j.get('data',j)
   return j
  except Exception:
   if attempt==2:raise
   time.sleep(2**attempt)
def rows(x):return x if isinstance(x,list) else x.get('data',[]) if isinstance(x,dict) else []
def num(x):
 try:
  v=float(x)
  return v if math.isfinite(v) and v>0 else None
 except (TypeError,ValueError):return None
def candles(sym,tf):
 now=time.time()*1000; a=[]
 for x in rows(api('/klines',{'symbol':sym,'interval':tf,'limit':120})):
  try:
   ts=int(x[0]);ts=ts*1000 if ts<10**10 else ts
   o,h,l,c,v=map(float,x[1:6])
   if ts+TFS[tf]*1000<=now and all(map(math.isfinite,(o,h,l,c,v))) and h>=max(o,c) and l<=min(o,c) and v>=0:a.append(dict(t=ts,o=o,h=h,l=l,c=c,v=v))
  except (TypeError,ValueError,IndexError):pass
 a=sorted({x['t']:x for x in a}.values(),key=lambda x:x['t'])
 if len(a)<55:raise ValueError('too few closed candles')
 if sum(a[i]['t']-a[i-1]['t']==TFS[tf]*1000 for i in range(len(a)-19,len(a)))<16:raise ValueError('candle gaps')
 if (now-a[-1]['t']-TFS[tf]*1000)/1000>2.5*TFS[tf]:raise ValueError('stale candles')
 return a
def ema(a,n):
 v=sum(a[:n])/n
 for x in a[n:]:v+=2/(n+1)*(x-v)
 return v
def setup(sym,data,quote):
 a=data['5m'];p=a[-1]['c'];prev=a[-21:-1]
 trends={tf:'UP' if ema([x['c'] for x in arr],20)>ema([x['c'] for x in arr],50) else 'DOWN' for tf,arr in data.items()}
 direction=trends['15m'];aligned=sum(x==direction for x in trends.values())
 if aligned<2:return None
 high=max(x['h'] for x in prev);low=min(x['l'] for x in prev)
 side='LONG' if direction=='UP' and p>high else 'SHORT' if direction=='DOWN' and p<low else None
 if not side:return None
 avg=sum(x['v'] for x in prev)/20;ratio=a[-1]['v']/avg if avg>0 else 0
 age=time.time()-a[-1]['t']/1000-300
 if ratio<1.1 or not 0<=age<=240:return None
 bid,ask,mark,last,quote_age=quote;spread=(ask-bid)/((ask+bid)/2)*100
 if not 0<=spread<=.3 or not -15<=quote_age<=45 or abs(last-p)/p>.008:return None
 atr=sum(max(a[i]['h']-a[i]['l'],abs(a[i]['h']-a[i-1]['c']),abs(a[i]['l']-a[i-1]['c'])) for i in range(len(a)-14,len(a)))/14
 stop=min(low,p-1.5*atr) if side=='LONG' else max(high,p+1.5*atr);risk=abs(p-stop)
 if not .001<=risk/p<=.045:return None
 score=max(0,min(100,round(min(25,max(0,(ratio-1)*12))+(25 if aligned==3 else 10)+max(0,20-age/12-max(0,quote_age)/5)+max(0,15-spread*45)+max(0,15-abs(100*risk/p-1)*6))))
 return dict(id=f"{sym}:5m:{side}:{a[-1]['t']}",symbol=sym,side=side,score=score,entry=p,stop=stop,tp1=p+(2*risk if side=='LONG' else -2*risk),mark=mark,last=last,volume=round(ratio,2),aligned=aligned)
def telegram(token,chat,msg):
 req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',data=json.dumps({'chat_id':chat,'text':msg}).encode(),headers={'Content-Type':'application/json'},method='POST')
 with urllib.request.urlopen(req,timeout=20) as r:
  if not json.load(r).get('ok'):raise RuntimeError('Telegram send failed')
def main():
 token=os.getenv('TELEGRAM_BOT_TOKEN','');chat=os.getenv('TELEGRAM_CHAT_ID','')
 if not token or not chat:raise RuntimeError('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets')
 file=Path('moon_state.json');state=json.loads(file.read_text()) if file.exists() else {}
 now=int(time.time());sent={k:v for k,v in state.get('sent',{}).items() if isinstance(v,(int,float)) and now-v<604800}
 tickers={x.get('symbol'):x for x in rows(api('/ticker/24hr')) if isinstance(x,dict)}
 books={x.get('symbol'):x for x in rows(api('/ticker/bookTicker')) if isinstance(x,dict)}
 ok=0;errors=[];signals=[]
 for sym in SYMS:
  sym=sym.strip().upper()
  try:
   t=tickers[sym];b=books[sym]
   bid,ask,mark,last=map(num,(b.get('bidPrice'),b.get('askPrice'),t.get('markPrice'),t.get('lastPrice')))
   ts=int(b.get('time') or t.get('closeTime') or 0);ts=ts*1000 if ts<10**10 else ts
   age=time.time()-ts/1000 if ts else 99999
   if not all((bid,ask,mark,last)) or not -15<=age<=45:raise ValueError('missing or stale quote')
   data={tf:candles(sym,tf) for tf in TFS};ok+=1
   s=setup(sym,data,(bid,ask,mark,last,age))
   if s:signals.append(s)
  except Exception as e:errors.append(f'{sym}: {type(e).__name__}: {str(e)[:90]}')
 print(f'WEEX Moon v7: {ok}/{len(SYMS)} markets, {len(signals)} setups; errors: {errors}',flush=True)
 if ok==0:raise RuntimeError('No healthy markets; alerts disabled')
 for s in sorted(signals,key=lambda x:x['score'],reverse=True):
  if s['score']<int(os.getenv('MOON_MIN_SCORE','80')) or s['id'] in sent:continue
  msg=(f"WEEX MOON v7 | PAPER SETUP\n{s['symbol']} {s['side']} | 5m SCALP\nScore {s['score']}/100 (geen winstkans)\n"+f"Candle entry {s['entry']:.8g}\nStop {s['stop']:.8g}\nTP1 {s['tp1']:.8g}\nMark {s['mark']:.8g} | Last {s['last']:.8g}\n"+f"Volume {s['volume']}x | Trend {s['aligned']}/3\nGeen live order. Controleer actuele koers, margin en liquidatierisico.")
  telegram(token,chat,msg);sent[s['id']]=now
 file.write_text(json.dumps({'version':'v7','lastRun':now,'marketsOK':ok,'errors':errors[:20],'setups':signals[:10],'sent':sent},indent=2)+'\n')
 if os.getenv('MOON_TEST_MESSAGE')=='1':telegram(token,chat,f'WEEX Moon v7 TEST OK: {ok}/{len(SYMS)} markten; {len(signals)} paper-setups. Geen tradesignaal.')
if __name__=='__main__':main()
