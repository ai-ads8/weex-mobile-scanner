"""WEEX Moon backtest — replays the LIVE signal engine on historical candles.

Usage
  python backtest.py fetch --symbols XRPUSDT,OPUSDT,BTCUSDT --days 30 --out data
  python backtest.py run --data data --symbols XRPUSDT,OPUSDT --latency 20 --fee 0.12 --slip 0.03

Data format: data/<SYMBOL>_<tf>.csv with header t,o,h,l,c,v (t = open time, ms).

Design rules (no look-ahead):
- At time T only candles whose CLOSE <= T are visible.
- The quote is synthetic (last = mark = last 1m close, spread = --spread), so
  quote/spread filters are optimistic; the report says so.
- Fill = open of the first 1m candle starting >= signal + latency, plus slippage.
- Stop wins if stop and target fall inside the same 1m candle.
- Results are split chronologically: first 60% in-sample, last 40% out-of-sample.
"""
import argparse
import bisect
import csv
import json
import os
import sys
import time

import signal_engine as eng
from signal_engine import MODES, TIMEFRAMES

TFS = ("1m", "5m", "15m", "1h", "4h", "1d")


def load(data_dir, symbol):
    out = {}
    for tf in TFS:
        path = os.path.join(data_dir, f"{symbol}_{tf}.csv")
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        with open(path) as f:
            rows = [{"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
                     "c": float(r["c"]), "v": float(r["v"])} for r in csv.DictReader(f)]
        rows = sorted({r["t"]: r for r in rows}.values(), key=lambda r: r["t"])
        out[tf] = rows
    return out


def visible(series, closes, T, n=120):
    """Last n candles whose close time <= T (closes = precomputed close times in s)."""
    i = bisect.bisect_right(closes, T)
    return series[max(0, i - n):i]


def quick_breakout(trigger):
    if len(trigger) < 55:
        return False
    prior = trigger[-21:-1]
    c = trigger[-1]["c"]
    return c > max(x["h"] for x in prior) or c < min(x["l"] for x in prior)


def run_symbol(symbol, d, btc=None, latency=20, fee=0.12, slip=0.03, spread=0.02, offset=3):
    closes = {tf: [x["t"] / 1000 + TIMEFRAMES[tf] for x in d[tf]] for tf in TFS}
    bcloses = {tf: [x["t"] / 1000 + TIMEFRAMES[tf] for x in btc[tf]] for tf in TFS} if btc else None
    one = d["1m"]
    setups, seen = [], set()
    start_i = 300  # warm-up so higher timeframes have history
    for i in range(start_i, len(one)):
        T = closes["1m"][i]
        now = T + offset
        for mode, m in MODES.items():
            tf = m["trigger"]
            if tf != "1m" and int(T) % TIMEFRAMES[tf] != 0:
                continue                      # only evaluate right after the trigger candle closes
            trig = visible(d[tf], closes[tf], T)
            if not quick_breakout(trig):
                continue
            need = (m["trigger"], m["direction"]) + m["confirm"]
            data = {x: visible(d[x], closes[x], T) for x in need}
            if any(len(v) < 55 for v in data.values()):
                continue                      # not enough history yet (1d needs ~55 days)
            info = {x: eng.timeframe_info(v) for x, v in data.items()}
            p = one[i]["c"]
            q = {"quoteFresh": True, "lastPrice": p, "markPrice": p, "spreadPct": spread,
                 "bidPrice": p * (1 - spread / 200), "askPrice": p * (1 + spread / 200), "quoteAgeSec": 1}
            binfo = None
            if btc and symbol != "BTCUSDT":
                bdata = {x: visible(btc[x], bcloses[x], T) for x in (m["direction"],)}
                if len(bdata[m["direction"]]) >= 55:
                    binfo = {m["direction"]: eng.timeframe_info(bdata[m["direction"]])}
            s, _ = eng.evaluate(symbol, mode, data, info, now, q, binfo)
            if s and s["id"] not in seen:
                seen.add(s["id"])
                rec = eng.new_paper(s, latency, fee, slip)
                # step_paper itself skips candles that open before fillAfter (no look-ahead)
                future = one[i + 1:i + 3 + int((rec["maxHold"] + latency) / 60)]
                eng.step_paper(rec, future)
                rec.update(score=s["score"], quality=s["quality"], warnings=s["warnings"],
                           diagnostics=s["diagnostics"], scoreComponents=s["scoreComponents"])
                setups.append(rec)
    return setups


def report(recs):
    closed = sorted([r for r in recs if r["status"] == "CLOSED"], key=lambda r: r["createdAt"])
    cut = int(len(closed) * 0.6)
    ins, oos = closed[:cut], closed[cut:]
    out = {"signals": len(recs), "closed": len(closed),
           "notClosed": {k: sum(r["status"] == k for r in recs) for k in ("OPEN", "PENDING", "SKIPPED")}}
    for name, part in (("inSample", ins), ("outOfSample", oos)):
        out[name] = {
            "baseline_allSetups": eng.summarize(part),
            "legacyScore80plus": eng.summarize([r for r in part if (r["score"] or 0) >= 80]),
            "qualityA": eng.summarize([r for r in part if r["quality"] == "A"]),
            "byScore": eng.grouped(part, lambda r: eng.bracket(r["score"] or 0)),
            "byQuality": eng.grouped(part, lambda r: r["quality"]),
            "byMode": eng.grouped(part, lambda r: r["mode"]),
        }
    out["caveats"] = ["Quote/spread is synthetisch (optimistisch).",
                      "Alleen geldig voor de geteste periode en paren; geen garantie voor de toekomst.",
                      "Beslis alleen op out-of-sample resultaten met n >= 100 per groep."]
    return out


def fetch(symbols, days, out_dir):
    """Paginated download via WEEX /klines endTime. NOTE: whether WEEX honours
    startTime/endTime and how far back it serves history is NOT verified; the
    script stops and warns if pagination does not move backwards."""
    import requests
    base = os.getenv("WEEX_BASE", "https://api-contract.weex.com/capi/v3/market")
    os.makedirs(out_dir, exist_ok=True)
    for sym in symbols:
        for tf in TFS:
            end = int(time.time() * 1000)
            stop_at = end - days * 86400 * 1000
            rows = {}
            while end > stop_at:
                r = requests.get(base + "/klines", params={"symbol": sym, "interval": tf, "limit": 1000,
                                                           "endTime": end}, timeout=20)
                r.raise_for_status()
                j = r.json()
                raw = j.get("data", j) if isinstance(j, dict) else j
                batch = [x for x in raw if isinstance(x, (list, tuple))]
                if not batch:
                    break
                oldest = min(int(x[0]) for x in batch)
                for x in batch:
                    rows[int(x[0])] = x[:6]
                if oldest >= end:
                    print(f"WAARSCHUWING {sym} {tf}: WEEX negeert endTime; alleen recente candles beschikbaar.")
                    break
                end = oldest - 1
                time.sleep(0.25)
            with open(os.path.join(out_dir, f"{sym}_{tf}.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["t", "o", "h", "l", "c", "v"])
                for t in sorted(rows):
                    w.writerow(rows[t])
            print(sym, tf, len(rows), "candles")


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--symbols", required=True)
    f.add_argument("--days", type=int, default=30)
    f.add_argument("--out", default="data")
    r = sub.add_parser("run")
    r.add_argument("--data", default="data")
    r.add_argument("--symbols", required=True)
    r.add_argument("--latency", type=int, default=20)
    r.add_argument("--fee", type=float, default=0.12)
    r.add_argument("--slip", type=float, default=0.03)
    r.add_argument("--spread", type=float, default=0.02)
    r.add_argument("--json", help="write full trade list to this file")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        fetch([s.strip().upper() for s in a.symbols.split(",")], a.days, a.out)
        return
    syms = [s.strip().upper() for s in a.symbols.split(",")]
    btc = load(a.data, "BTCUSDT") if os.path.exists(os.path.join(a.data, "BTCUSDT_1m.csv")) else None
    recs = []
    for s in syms:
        recs += run_symbol(s, load(a.data, s), btc, a.latency, a.fee, a.slip, a.spread)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(recs, fh)
    json.dump(report(recs), sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
