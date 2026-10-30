

"""WEEX Moon Trade Analyzer v1.0"""

import time
from flask import jsonify, request, Response

HTML = """
<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1">
<title>WEEX Moon | Trade Analyzer</title>
<style>
* {box-sizing:border-box}
body {
  background:#0b101c;
  color:#f5f7ff;
  font:16px system-ui;
  margin:0;
  padding:24px 14px;
}
main {max-width:650px;margin:auto}
h1 {font-size:32px}
p,small {color:#aabbd1}
label {
  display:block;
  margin:15px 0 6px;
  font-size:13px;
  color:#c4d1e2;
}
input,select,button {
  width:100%;
  padding:14px;
  background:#1b2a40;
  border:1px solid #40506a;
  border-radius:10px;
  color:white;
  font:inherit;
}
button {
  background:#ff7358;
  border:0;
  font-weight:750;
  margin-top:22px;
  cursor:pointer;
}
.grid {
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:12px;
}
.panel {
  background:#19263a;
  border:1px solid #34445e;
  border-radius:16px;
  padding:20px;
  margin-top:20px;
  white-space:pre-wrap;
  line-height:1.7;
}
a {color:#ff9a83}
</style>
</head>
<body>
<main>
<a href="/">← Scanner</a>

<h1>Trade Analyzer</h1>
<p>
Analyseer je open positie met actuele WEEX-marktdata.
Geen automatische orders.
</p>

<label>Trading pair</label>
<input id="symbol" value="BTCUSDT">

<div class="grid">
  <div>
    <label>Richting</label>
    <select id="side">
      <option>LONG</option>
      <option>SHORT</option>
    </select>
  </div>

  <div>
    <label>Leverage</label>
    <input id="leverage"
           type="number"
           value="20"
           min="1"
           max="200">
  </div>

  <div>
    <label>Entry (USDT)</label>
    <input id="entry"
           type="number"
           step="any"
           placeholder="65000">
  </div>

  <div>
    <label>Margin (USDT)</label>
    <input id="margin"
           type="number"
           step="any"
           value="25">
  </div>
</div>

<label>Stopprijs (optioneel)</label>
<input id="stop"
       type="number"
       step="any">

<button onclick="analyze()">
  Analyseer trade
</button>

<div id="result" class="panel">
Vul je positie in.
De huidige prijs wordt automatisch opgehaald.
</div>

<small>
Geen automatische orders.
Liquidatie, cross margin, fees en funding
zijn niet volledig meegenomen.
</small>
</main>

<script>
async function analyze() {
  let el = document.getElementById('result');
  el.textContent = 'Live scannerdata ophalen...';

  let params = new URLSearchParams();

  for (let key of [
    'symbol',
    'side',
    'leverage',
    'entry',
    'margin',
    'stop'
  ]) {
    params.set(
      key,
      document.getElementById(key).value
    );
  }

  try {
    let response = await fetch(
      '/api/trade-analyzer?' + params,
      {cache:'no-store'}
    );

    let data = await response.json();

    if (!response.ok) {
      throw Error(data.error || 'Serverfout');
    }

    el.textContent =
      data.action + '\\n\\n' +
      data.reason + '\\n\\n' +
      'Mark price: ' + data.markPrice +
      ' USDT\\n' +
      'Geschatte PNL: ' +
      data.estimatedPnl + ' USDT (' +
      data.estimatedRoiPct + '%)\\n' +
      'Positiegrootte: ' +
      data.notional + ' USDT\\n' +
      'Trend mee: ' +
      data.alignedTimeframes +
      '/4 | tegen: ' +
      data.oppositeTimeframes + '/4\\n' +
      'Stop PNL: ' +
      (data.estimatedStopPnl ?? 'niet ingevuld') +
      ' USDT\\n' +
      'Koersquote: ' +
      data.quoteAgeSec +
      ' sec oud\\n\\n' +
      data.warning;

  } catch (error) {
    el.textContent =
      'Geen analyse mogelijk: ' + error.message;
  }
}
</script>
</body>
</html>
"""


def register_trade_analyzer(app, state, lock, symbols):

    @app.get("/trade")
    def trade_page():
        return Response(
            HTML,
            mimetype="text/html"
        )

    @app.get("/api/trade-analyzer")
    def trade_analyzer():

        symbol = (
            request.args.get("symbol", "")
            .strip()
            .upper()
            .replace("/", "")
            .replace("-", "")
        )

        side = request.args.get(
            "side", "LONG"
        ).upper()

        try:
            entry = float(
                request.args.get("entry", "0")
            )

            margin = float(
                request.args.get("margin", "0")
            )

            leverage = float(
                request.args.get("leverage", "1")
            )

            stop_raw = request.args.get(
                "stop", ""
            ).strip()

            stop = (
                float(stop_raw)
                if stop_raw else None
            )

        except ValueError:
            return jsonify(
                error="Ongeldige getallen"
            ), 400

        if (
            symbol not in symbols
            or side not in ("LONG", "SHORT")
            or not (
                entry > 0
                and margin > 0
                and 1 <= leverage <= 200
            )
        ):
            return jsonify(
                error=(
                    "Controleer pair, richting, "
                    "entry, margin en leverage"
                )
            ), 400

        with lock:
            market = dict(
                state.get("symbols", {}).get(symbol)
                or {}
            )

            last_scan = state.get("lastScan")

        quote = market.get("quote") or {}
        price = quote.get("markPrice")

        age = (
            time.time() - last_scan
            if last_scan else None
        )

        if not (
            market.get("status") == "OK"
            and age is not None
            and 0 <= age < 180
            and quote.get("quoteFresh")
            and isinstance(price, (float, int))
            and price > 0
        ):
            return jsonify(
                error=(
                    "Geen actuele WEEX mark price "
                    "voor dit pair. "
                    "Geen analyse mogelijk."
                ),
                scannerAgeSec=(
                    round(age, 1)
                    if age is not None else None
                )
            ), 503

        direction = (
            1 if side == "LONG" else -1
        )

        notional = margin * leverage

        pnl = (
            notional
            * direction
            * (price / entry - 1)
        )

        roi = pnl / margin * 100

        stop_pnl = (
            notional
            * direction
            * (stop / entry - 1)
            if stop and stop > 0
            else None
        )

        trends = market.get(
            "timeframes"
        ) or {}

        timeframes = (
            "5m", "15m", "1h", "4h"
        )

        aligned = sum(
            1 for tf in timeframes
            if (
                trends.get(tf) or {}
            ).get("trend") == (
                "UP"
                if side == "LONG"
                else "DOWN"
            )
        )

        opposite = sum(
            1 for tf in timeframes
            if (
                trends.get(tf) or {}
            ).get("trend") == (
                "DOWN"
                if side == "LONG"
                else "UP"
            )
        )

        if leverage >= 50:
            action = "RISICO VERMINDEREN"
            reason = (
                "Zeer hoge leverage. "
                "Een kleine koersbeweging kan "
                "liquidatie veroorzaken."
            )

        elif opposite >= 3:
            action = "POSITIE HERBEOORDELEN"
            reason = (
                "De meeste beschikbare "
                "timeframes bewegen tegen "
                "je positie."
            )

        elif roi > 20 and aligned < 2:
            action = "WINST BESCHERMEN"
            reason = (
                "Je staat op winst, maar "
                "de trendbevestiging is beperkt."
            )

        elif aligned >= 3:
            action = "AFWACHTEN MET RISICOGRENS"
            reason = (
                "Meerdere timeframes steunen "
                "de richting. Controleer je "
                "vooraf bepaalde exitregels."
            )

        else:
            action = "AFWACHTEN / NIET BIJKOPEN"
            reason = (
                "Onvoldoende duidelijke "
                "trendbevestiging om extra "
                "risico te rechtvaardigen."
            )

        return jsonify(
            symbol=symbol,
            side=side,
            entry=entry,
            markPrice=price,
            quoteAgeSec=quote.get("quoteAgeSec"),
            margin=margin,
            leverage=leverage,
            notional=round(notional, 2),
            estimatedPnl=round(pnl, 2),
            estimatedRoiPct=round(roi, 2),
            estimatedStopPnl=(
                round(stop_pnl, 2)
                if stop_pnl is not None
                else None
            ),
            alignedTimeframes=aligned,
            oppositeTimeframes=opposite,
            action=action,
            reason=reason,
            warning=(
                "Risicocheck, geen persoonlijk "
                "handelsadvies. PNL exclusief "
                "fees, funding en slippage. "
                "Geen liquidatieberekening, "
                "vooral bij cross margin."
            ),
            scannerAgeSec=round(age, 1)
        )
