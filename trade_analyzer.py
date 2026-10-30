"""WEEX Moon Trade Analyzer v1.1 — transparent rule-based position check.

Not personal financial advice. Uses the scanner's latest WEEX mark price; never
claims to know the liquidation price unless the user enters the WEEX value.
"""
import time

from flask import Response, jsonify, request

FEE_PCT_PER_SIDE = 0.06  # assumption; WEEX fee tier not read from account

HTML = r"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0a0f1a">
<title>Trade Analyzer</title><style>
:root{color-scheme:dark;--bg:#0a0f1a;--card:#131c2c;--line:#243349;--muted:#8fa2bd;--accent:#ff7a5c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eef3fb;font:16px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:620px;margin:auto;padding:20px 16px 60px}a{color:var(--accent);text-decoration:none}
h1{font-size:26px;margin:14px 0 4px}p,small{color:var(--muted)}
label{display:block;margin:12px 0 5px;font-size:13px;color:#c3d0e2}
input,select,button{width:100%;padding:13px;background:var(--card);border:1px solid var(--line);border-radius:11px;color:#fff;font:inherit}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}button{background:var(--accent);color:#1a0d08;border:0;font-weight:700;margin-top:18px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin-top:18px}
.status{font-size:20px;font-weight:800}.s-HOLD{color:#5fe0a8}.s-PROTECT{color:#9fd8ff}.s-PARTIAL{color:#9fd8ff}.s-REDUCE,.s-SETUP{color:#ff8597}.s-INSUFFICIENT{color:#ffc46b}
.kv{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:12px}.kv div{background:#0e1625;border-radius:10px;padding:8px;font-size:14px}.kv small{display:block}
ul{padding-left:18px;margin:8px 0}li{margin:4px 0}
</style></head><body><main><a href="/">← Scanner</a><h1>Trade Analyzer</h1>
<p>Regelgebaseerde check van je open positie met de laatste WEEX mark price. Geen orders, geen advies.</p>
<label>Pair</label><input id="symbol" value="XRPUSDT" autocapitalize="characters">
<div class="grid"><div><label>Richting</label><select id="side"><option>LONG</option><option>SHORT</option></select></div>
<div><label>Leverage</label><input id="leverage" type="number" value="20" min="1" max="200"></div>
<div><label>Entry</label><input id="entry" type="number" step="any" inputmode="decimal"></div>
<div><label>Margin (USDT)</label><input id="margin" type="number" step="any" inputmode="decimal"></div>
<div><label>Positiewaarde (USDT, uit WEEX)</label><input id="notional" type="number" step="any" inputmode="decimal" placeholder="optioneel"></div>
<div><label>Liquidatieprijs (uit WEEX)</label><input id="liquidation" type="number" step="any" inputmode="decimal" placeholder="optioneel"></div>
<div><label>Stop</label><input id="stop" type="number" step="any" inputmode="decimal" placeholder="aanbevolen"></div>
<div><label>Doel</label><input id="target" type="number" step="any" inputmode="decimal" placeholder="optioneel"></div></div>
<button onclick="analyze()">Analyseer positie</button><div id="result" class="panel">Vul je positie in.</div>
<small>PNL is een schatting (excl. funding; fees als aanname). Bij Cross Combined hangt liquidatie af van je hele account.</small>
</main><script>
const n=x=>x==null?'—':Number(x).toLocaleString('en-US',{maximumFractionDigits:8}),e=x=>String(x??'—').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function analyze(){let el=document.getElementById('result');el.textContent='Ophalen…';let p=new URLSearchParams();
for(const k of ['symbol','side','leverage','entry','margin','notional','liquidation','stop','target'])p.set(k,document.getElementById(k).value);
try{let r=await fetch('/api/trade-analyzer?'+p,{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.error||'Serverfout');
el.innerHTML='<div class="status s-'+e(d.status.split(' ')[0].split('/')[0])+'">'+e(d.status)+'</div><ul>'+d.reasons.map(x=>'<li>'+e(x)+'</li>').join('')+'</ul>'+
'<div class="kv"><div><small>Mark price</small>'+n(d.markPrice)+'</div><div><small>Koersverschil</small>'+n(d.priceChangePct)+'%</div>'+
'<div><small>Geschatte PNL</small>'+n(d.estimatedPnl)+' USDT</div><div><small>ROI op margin</small>'+n(d.estimatedRoiPct)+'%</div>'+
'<div><small>Verlies bij stop</small>'+(d.lossAtStopPctOfMargin==null?'geen stop':n(d.lossAtStopPctOfMargin)+'% margin')+'</div><div><small>Afstand liquidatie</small>'+(d.liquidationDistancePct==null?'niet opgegeven':n(d.liquidationDistancePct)+'%')+'</div>'+
'<div><small>Trend mee / tegen</small>'+d.alignedTimeframes+' / '+d.oppositeTimeframes+' (5m-4h)</div><div><small>1h range</small>'+n(d.levels.h1Low)+' – '+n(d.levels.h1High)+'</div></div>'+
'<p>'+e(d.uncertainty)+'</p><small>Quote '+e(d.quoteAgeSec)+'s oud · '+e(d.warning)+'</small>'}catch(err){el.textContent='Geen analyse: '+err.message}}
</script></body></html>"""


def _num(name, required=False):
    raw = (request.args.get(name) or "").strip()
    if not raw:
        if required:
            raise ValueError(name)
        return None
    v = float(raw)
    if v <= 0:
        raise ValueError(name)
    return v


def analyze_position(side, entry, price, leverage, margin=None, notional=None, stop=None, target=None,
                     liquidation=None, trends=None, atr5=None):
    """Pure function (tested). Returns dict with status, reasons and numbers."""
    sign = 1 if side == "LONG" else -1
    if notional is None and margin:
        notional = margin * leverage
    if margin is None and notional:
        margin = notional / leverage
    qty = notional / entry if notional else None
    pnl = qty * sign * (price - entry) if qty else None
    roi = 100 * pnl / margin if pnl is not None and margin else None
    fees = notional * 2 * FEE_PCT_PER_SIDE / 100 if notional else None
    risk = abs(entry - stop) if stop else None
    r_now = sign * (price - entry) / risk if risk else None
    loss_at_stop = 100 * (risk / entry) * leverage if risk else None
    liq_dist = 100 * abs(price - liquidation) / price if liquidation else None
    stop_beyond_liq = bool(stop and liquidation and sign * (stop - liquidation) <= 0)
    trends = trends or {}
    want = "UP" if side == "LONG" else "DOWN"
    tfs = ("5m", "15m", "1h", "4h")
    aligned = sum((trends.get(t) or {}).get("trend") == want for t in tfs)
    opposite = sum((trends.get(t) or {}).get("trend") not in (None, want) for t in tfs)

    conds = []
    if stop and sign * (price - stop) <= 0:
        conds.append(("SETUP INVALIDATED", "Koers staat voorbij je stop/invalidatie."))
    if target and sign * (price - target) >= 0:
        conds.append(("PARTIAL EXIT CONDITION MET", "Je doel is bereikt."))
    elif r_now is not None and r_now >= 2:
        conds.append(("PARTIAL EXIT CONDITION MET", f"+{r_now:.1f}R t.o.v. je stopafstand."))
    if stop_beyond_liq:
        conds.append(("REDUCE EXPOSURE", "Je stop ligt voorbij de liquidatieprijs: je wordt eerder geliquideerd dan gestopt."))
    if loss_at_stop is not None and loss_at_stop >= 90:
        conds.append(("REDUCE EXPOSURE", f"Verlies bij stop ≈ {loss_at_stop:.0f}% van je margin (stopafstand × leverage)."))
    if not stop and leverage >= 20:
        conds.append(("REDUCE EXPOSURE", f"Geen stop bij {leverage:g}x leverage."))
    if liquidation and atr5 and abs(price - liquidation) < 1.5 * atr5:
        conds.append(("REDUCE EXPOSURE", "Liquidatie ligt binnen 1,5× de gemiddelde 5m-beweging."))
    if r_now is not None and 1 <= r_now < 2:
        conds.append(("PROTECT PROFITS", f"+{r_now:.1f}R: stop naar entry verkleint je risico."))
    elif r_now is None and roi is not None and roi >= 50:
        conds.append(("PROTECT PROFITS", f"ROI +{roi:.0f}% zonder stop: winst kan snel verdwijnen."))
    if opposite >= 3:
        conds.append(("HOLD / MONITOR", f"{opposite}/4 timeframes bewegen tegen je positie; niet bijkopen."))

    order = ["SETUP INVALIDATED", "REDUCE EXPOSURE", "PARTIAL EXIT CONDITION MET", "PROTECT PROFITS", "HOLD / MONITOR"]
    status = min((c[0] for c in conds), key=order.index, default="HOLD / MONITOR")
    reasons = [c[1] for c in conds] or ["Geen vooraf ingestelde exitvoorwaarde geraakt. Blijf je eigen exitregels volgen."]
    if aligned <= opposite:
        reasons.append("Bijkopen verhoogt je risico: trend steunt de positie niet duidelijk.")
    return {"status": status, "reasons": reasons,
            "priceChangePct": round(100 * (price / entry - 1), 3),
            "notional": round(notional, 2) if notional else None,
            "estimatedPnl": round(pnl, 2) if pnl is not None else None,
            "estimatedRoiPct": round(roi, 2) if roi is not None else None,
            "estimatedFeesRoundtrip": round(fees, 2) if fees else None,
            "rMultiple": round(r_now, 2) if r_now is not None else None,
            "lossAtStopPctOfMargin": round(loss_at_stop, 1) if loss_at_stop is not None else None,
            "liquidationDistancePct": round(liq_dist, 3) if liq_dist is not None else None,
            "stopBeyondLiquidation": stop_beyond_liq,
            "alignedTimeframes": aligned, "oppositeTimeframes": opposite}


def register_trade_analyzer(app, state, lock, symbols):

    @app.get("/trade")
    def trade_page():
        return Response(HTML, mimetype="text/html")

    @app.get("/api/trade-analyzer")
    def trade_analyzer():
        symbol = request.args.get("symbol", "").strip().upper().replace("/", "").replace("-", "")
        side = request.args.get("side", "LONG").upper()
        try:
            entry = _num("entry", True)
            leverage = _num("leverage", True)
            margin, notional = _num("margin"), _num("notional")
            stop, target, liq = _num("stop"), _num("target"), _num("liquidation")
        except ValueError:
            return jsonify(error="Ongeldige getallen (alle bedragen moeten > 0 zijn)"), 400
        if symbol not in symbols or side not in ("LONG", "SHORT") or not 1 <= leverage <= 200 or not (margin or notional):
            return jsonify(error="Controleer pair, richting, leverage en margin of positiewaarde"), 400
        with lock:
            market = dict(state.get("symbols", {}).get(symbol) or {})
            last_scan = state.get("lastScan")
        quote = market.get("quote") or {}
        price = quote.get("markPrice")
        ts = quote.get("quoteTimestamp")
        q_age = time.time() - ts / 1000 if ts else None   # age NOW, not at scan time (v1.0 bug)
        scan_age = time.time() - last_scan if last_scan else None
        if not (market.get("status") == "OK" and scan_age is not None and scan_age < 180
                and q_age is not None and q_age < 120 and isinstance(price, (int, float)) and price > 0):
            return jsonify(status="INSUFFICIENT DATA",
                           error="Geen actuele WEEX mark price voor dit pair. Geen analyse mogelijk.",
                           scannerAgeSec=round(scan_age, 1) if scan_age is not None else None), 503
        tfs = market.get("timeframes") or {}
        res = analyze_position(side, entry, price, leverage, margin, notional, stop, target, liq,
                               tfs, (tfs.get("5m") or {}).get("atr"))
        h1 = tfs.get("1h") or {}
        res.update(symbol=symbol, side=side, entry=entry, markPrice=price, leverage=leverage,
                   quoteAgeSec=round(q_age, 1), scannerAgeSec=round(scan_age, 1),
                   levels={"h1High": h1.get("rangeHigh"), "h1Low": h1.get("rangeLow"),
                           "m15High": (tfs.get("15m") or {}).get("rangeHigh"),
                           "m15Low": (tfs.get("15m") or {}).get("rangeLow")},
                   uncertainty=("Mark price is een momentopname (max ~1 min oud). PNL is geschat: "
                                "exchange-PNL kan afwijken door contractspecificaties, funding en gedeeltelijke fills."),
                   warning=("Regelgebaseerde risicocheck, geen persoonlijk advies. Liquidatieprijs alleen als jij de "
                            "WEEX-waarde invult; bij Cross Combined telt je hele accountsaldo mee."))
        return jsonify(res)
