"""WEEX Moon scanner v6.0.

REST polling aligned to candle closes, confirmed candles only, entry-quality
checks, candle-based paper tracking, optional direct Telegram alerts and
position monitoring. Read-only: never places orders.
"""
import hmac
import json
import logging
import math
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

import requests
from flask import Flask, Response, jsonify, request

import notifier
import signal_engine as eng
from signal_engine import MODES, TIMEFRAMES

VERSION = "v6.0"
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner")

BASE = os.getenv("WEEX_BASE", "https://api-contract.weex.com/capi/v3/market")
SYMBOLS = list(dict.fromkeys(s.strip().upper() for s in os.getenv(
    "SCAN_SYMBOLS",
    "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,HBARUSDT,INJUSDT,UNIUSDT,"
    "QNTUSDT,RENDERUSDT,RNDRUSDT,XAUUSDT,BNBUSDT,ADAUSDT,AVAXUSDT,"
    "LINKUSDT,SUIUSDT,NEARUSDT,LTCUSDT,APTUSDT,ARBUSDT,OPUSDT,TRXUSDT,PEPEUSDT"
).split(",") if s.strip()))
INTERVAL = max(30, int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
SCAN_OFFSET = float(os.getenv("SCAN_OFFSET_SECONDS", "3"))   # wait after candle close so WEEX has it
MAX_QUOTE_AGE_SEC = 45
DB_PATH = os.getenv("SCANNER_DB_PATH", "/tmp/weex_moon_signals.sqlite3")
ACTIVE_TTL = {"SCALP": 5 * 60, "INTRADAY": 30 * 60}           # how long a setup stays listed
ALERT_COOLDOWN = {"SCALP": 10 * 60, "INTRADAY": 60 * 60}      # per symbol+mode+side
PAPER_LATENCY = int(os.getenv("PAPER_LATENCY_SECONDS", "20"))
PAPER_FEE_PCT = float(os.getenv("PAPER_FEE_PCT_ROUNDTRIP", "0.12"))   # assumption, check your WEEX fee tier
PAPER_SLIP_PCT = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.03"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHATS = notifier.chat_ids(os.getenv("ALERT_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", ""))
SCANNER_ALERTS = bool(TG_TOKEN and TG_CHATS and os.getenv("ALERTS_FROM_SCANNER", "1") != "0")
ALERT_MIN_QUALITY = os.getenv("ALERT_MIN_QUALITY", "A").upper()
ACCESS_TOKEN = os.getenv("APP_ACCESS_TOKEN", "").strip()

state = {"lastScan": None, "lastSuccessfulScan": None, "symbols": {}, "setups": [], "errors": [],
         "quotesUpdated": None, "scanDuration": None, "timeframes": list(TIMEFRAMES),
         "rejects": {}, "apiCalls": 0, "alertsSent": 0, "lastAlertError": None}
lock = threading.RLock()
session = requests.Session()
active = {}
kline_cache = {}
cached_tickers, cached_books, last_quotes = {}, {}, 0


# ------------------------------------------------------------------ storage
@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, symbol TEXT, mode TEXT, side TEXT,"
                  " quality TEXT, score INTEGER, created_at INTEGER, data TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS paper (id TEXT PRIMARY KEY, symbol TEXT, status TEXT,"
                  " created_at INTEGER, data TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS alerts_sent (signal_id TEXT PRIMARY KEY, key TEXT, sent_at REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY AUTOINCREMENT, active INTEGER,"
                  " created_at INTEGER, data TEXT)")


def load_seen():
    try:
        with db() as c:
            return {r[0] for r in c.execute("SELECT id FROM signals WHERE created_at > ?", (int(time.time()) - 86400,))}
    except Exception:
        log.exception("Could not restore signal deduplication")
        return set()


init_db()
seen = load_seen()


def save_signal(s):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?)",
                  (s["id"], s["symbol"], s["mode"], s["side"], s["quality"], s["score"], s["createdAt"],
                   json.dumps({k: v for k, v in s.items() if k != "quote"})))
        rec = eng.new_paper(s, PAPER_LATENCY, PAPER_FEE_PCT, PAPER_SLIP_PCT)
        c.execute("INSERT OR IGNORE INTO paper VALUES (?,?,?,?,?)",
                  (rec["id"], rec["symbol"], rec["status"], rec["createdAt"], json.dumps(rec)))


def paper_track(data_by_symbol, now):
    """Advance open paper trades over newly confirmed 1m candles (no look-ahead:
    only candles that opened after the simulated fill time are used)."""
    with db() as c:
        rows = c.execute("SELECT id,data FROM paper WHERE status IN ('PENDING','OPEN')").fetchall()
        for ident, raw in rows:
            rec = json.loads(raw)
            one_min = (data_by_symbol.get(rec["symbol"]) or {}).get("1m")
            if one_min:
                eng.step_paper(rec, one_min)
            if rec["status"] in ("PENDING", "OPEN") and now - rec["createdAt"] > rec["maxHold"] + 3600:
                rec.update(status="UNRESOLVED", exitReason="geen candledata (scanner sliep of herstartte)")
            c.execute("UPDATE paper SET status=?, data=? WHERE id=?", (rec["status"], json.dumps(rec), ident))


def paper_records(limit=2000):
    with db() as c:
        return [json.loads(r[0]) for r in c.execute(
            "SELECT data FROM paper ORDER BY created_at DESC LIMIT ?", (limit,))]


def paper_stats():
    recs = paper_records()
    closed = [r for r in recs if r["status"] == "CLOSED"]
    counts = {}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    m = eng.summarize(list(reversed(closed)))
    # Backward-compatible keys for older clients
    m.update(tracked=len(recs), open=counts.get("OPEN", 0) + counts.get("PENDING", 0),
             tp1=sum(r["exitReason"] == "TP1" for r in closed), sl=sum(r["exitReason"] == "SL" for r in closed),
             expired=sum(r["exitReason"] == "TIME" for r in closed), closed=len(closed),
             unresolved=counts.get("UNRESOLVED", 0), statusCounts=counts)
    return m, recs[:80]


# ------------------------------------------------------------------ WEEX data
def get_json(path, params=None, timeout=10):
    with lock:
        state["apiCalls"] += 1
    r = session.get(BASE + path, params=params, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict) and "code" in j and str(j["code"]) not in ("0", "00000", "200"):
        raise ValueError(str(j.get("msg") or j["code"]))
    return j.get("data", j) if isinstance(j, dict) else j


def as_rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        return value["data"]
    return []


def refresh_quotes():
    global cached_tickers, cached_books, last_quotes
    now = time.time()
    if now - last_quotes < 20:
        return
    tickers = as_rows(get_json("/ticker/24hr", timeout=18))
    books = as_rows(get_json("/ticker/bookTicker", timeout=18))
    if not tickers:
        raise ValueError("WEEX ticker-feed leeg")
    cached_tickers = {x["symbol"]: x for x in tickers if isinstance(x, dict) and x.get("symbol")}
    cached_books = {x["symbol"]: x for x in books if isinstance(x, dict) and x.get("symbol")}
    last_quotes = now


def number(x):
    try:
        f = float(x)
        return f if math.isfinite(f) and f > 0 else None
    except (TypeError, ValueError):
        return None


def quote(symbol, now=None):
    now = now or time.time()
    t = cached_tickers.get(symbol, {})
    b = cached_books.get(symbol, {})
    bid, ask = number(b.get("bidPrice")), number(b.get("askPrice"))
    last, mark = number(t.get("lastPrice")), number(t.get("markPrice"))
    ts = int(b.get("time") or t.get("closeTime") or 0)
    if ts and ts < 10_000_000_000:
        ts *= 1000
    age = (now * 1000 - ts) / 1000 if ts else None
    spread = round((ask - bid) / ((ask + bid) / 2) * 100, 4) if bid and ask and ask >= bid else None
    return {"lastPrice": last, "markPrice": mark, "indexPrice": number(t.get("indexPrice")),
            "bidPrice": bid, "askPrice": ask, "spreadPct": spread, "quoteTimestamp": ts or None,
            "quoteAgeSec": round(age, 1) if age is not None else None,
            "quoteFresh": bool(age is not None and -15 <= age <= MAX_QUOTE_AGE_SEC),
            "source": "WEEX futures REST; polled, not streaming"}


def parse_candles(raw, tf, now_ms):
    out = []
    for x in raw:
        try:
            ts = int(x[0])
            if ts < 10_000_000_000:
                ts *= 1000
            o, h, l, c, v = map(float, x[1:6])
            if all(map(math.isfinite, (o, h, l, c, v))) and ts + TIMEFRAMES[tf] * 1000 <= now_ms:
                out.append({"t": ts, "o": o, "h": h, "l": l, "c": c, "v": v})
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    out = sorted({x["t"]: x for x in out}.values(), key=lambda x: x["t"])
    if len(out) < 55:
        raise ValueError(f"{tf}: te weinig gesloten candles ({len(out)})")
    step = TIMEFRAMES[tf] * 1000
    gaps = [out[i]["t"] - out[i - 1]["t"] for i in range(max(1, len(out) - 20), len(out))]
    if sum(g == step for g in gaps) < max(10, int(len(gaps) * .8)):
        raise ValueError(f"{tf}: onjuiste candle-intervallen")
    if any(x["h"] < max(x["o"], x["c"]) or x["l"] > min(x["o"], x["c"]) or x["v"] < 0 for x in out[-25:]):
        raise ValueError(f"{tf}: OHLCV inconsistent")
    age = (now_ms - (out[-1]["t"] + step)) / 1000
    if age > TIMEFRAMES[tf] * 2.5:
        raise ValueError(f"{tf}: oude candles ({int(age)}s)")
    return out


def candles(symbol, tf):
    """Cached: a timeframe is only re-downloaded once a newer candle can have closed.
    Cuts WEEX calls from ~170/min (v5.1) to roughly 30/min for 24 symbols."""
    now = time.time()
    hit = kline_cache.get((symbol, tf))
    if hit and now < hit[-1]["t"] / 1000 + 2 * TIMEFRAMES[tf] + SCAN_OFFSET:
        return hit
    raw = as_rows(get_json("/klines", {"symbol": symbol, "interval": tf, "limit": 120}, timeout=12))
    out = parse_candles(raw, tf, now * 1000)
    kline_cache[(symbol, tf)] = out
    return out


def fetch_symbol(symbol):
    now = time.time()
    q = quote(symbol, now)
    if symbol not in cached_tickers:
        return symbol, {"status": "UNAVAILABLE", "reason": "Contract niet gevonden in WEEX futures tickers",
                        "quote": q}, {}, {}, []
    data, errors = {}, []
    for tf in TIMEFRAMES:
        try:
            data[tf] = candles(symbol, tf)
        except Exception as e:
            kline_cache.pop((symbol, tf), None)
            errors.append(f"{tf}: {str(e)[:100]}")
    info = {tf: eng.timeframe_info(a) for tf, a in data.items()}
    usable = all(tf in data for m in MODES.values() for tf in (m["trigger"], m["direction"]) + m["confirm"])
    entry = {"status": "OK" if usable and q["quoteFresh"] else "PARTIAL", "quote": q, "timeframes": info,
             "closed1mPrice": info.get("1m", {}).get("lastClosedPrice"),
             "lastClosed": info.get("1m", {}).get("lastClosedAt"),
             "updatedAt": int(now), "reason": "; ".join(errors[:4]) if errors else None}
    return symbol, entry, data, info, errors


# ------------------------------------------------------------------ alerts & positions
def alert_key(s):
    return f"{s['symbol']}:{s['mode']}:{s['side']}"


def maybe_alert(setups, now):
    if not SCANNER_ALERTS:
        return
    for s in sorted(setups, key=lambda x: (-eng.QUALITY_RANK.get(x["quality"], 0), -x["score"])):
        if not eng.passes_alert(s, ALERT_MIN_QUALITY):
            continue
        fresh = eng.freshness(s, now, (s.get("quote") or {}).get("lastPrice"))
        if not fresh["actionable"]:
            continue
        key = alert_key(s)
        with db() as c:
            if c.execute("SELECT 1 FROM alerts_sent WHERE signal_id=? OR (key=? AND sent_at>?)",
                         (s["id"], key, now - ALERT_COOLDOWN[s["mode"]])).fetchone():
                continue
        ok = all(notifier.send(TG_TOKEN, chat, notifier.signal_text(s, fresh)) for chat in TG_CHATS)
        with lock:
            state["lastAlertError"] = None if ok else f"verzenden mislukt {int(now)}"
        if ok:
            with db() as c:
                c.execute("INSERT OR IGNORE INTO alerts_sent VALUES (?,?,?)", (s["id"], key, now))
            with lock:
                state["alertsSent"] += 1


def load_positions(active_only=True):
    with db() as c:
        q = "SELECT id,data FROM positions" + (" WHERE active=1" if active_only else "") + " ORDER BY id DESC"
        return [dict(json.loads(d), id=i) for i, d in c.execute(q)]


def position_events(p, price, atr5):
    """Rule-based events; each fires once per position."""
    sign = 1 if p["side"] == "LONG" else -1
    ev = []
    if p.get("stop"):
        risk = abs(p["entry"] - p["stop"])
        if sign * (price - p["stop"]) <= 0:
            ev.append(("STOP", "Invalidatie/stop gekruist: setup ongeldig"))
        elif risk and sign * (p["entry"] - price) >= 0.5 * risk:
            ev.append(("HALF_STOP", "Koers is >50% van de stopafstand tegen je in"))
        if risk and sign * (price - p["entry"]) >= risk:
            ev.append(("PROFIT_1R", "+1R bereikt: overweeg winst te beschermen (stop naar entry)"))
    if p.get("target") and sign * (price - p["target"]) >= 0:
        ev.append(("TARGET", "Doel bereikt: gedeeltelijke-exitvoorwaarde voldaan"))
    if p.get("liquidation"):
        dist = abs(price - p["liquidation"])
        if dist / price < 0.01 or (atr5 and dist < 1.5 * atr5):
            ev.append(("LIQ_NEAR", f"Liquidatieprijs (door jou opgegeven) dichtbij: {dist / price * 100:.2f}%"))
    return ev


def monitor_positions(rows, now):
    try:
        positions = load_positions()
    except Exception:
        log.exception("positions load failed")
        return
    for p in positions:
        row = rows.get(p["symbol"]) or {}
        q = row.get("quote") or {}
        price = q.get("markPrice")
        if not (q.get("quoteFresh") and price):
            continue
        atr5 = ((row.get("timeframes") or {}).get("5m") or {}).get("atr")
        sent = set(p.get("sent", []))
        new = [(k, msg) for k, msg in position_events(p, price, atr5) if k not in sent]
        if not new:
            continue
        text = (f"📍 Positie #{p['id']} {p['symbol']} {p['side']} · mark {notifier.fmt(price)}\n"
                + "\n".join(f"• {m}" for _, m in new)
                + "\nRegelgebaseerde melding, geen advies. Controleer je positie in WEEX.")
        chats = [p["chat_id"]] if p.get("chat_id") else TG_CHATS
        if TG_TOKEN and chats and all(notifier.send(TG_TOKEN, ch, text) for ch in chats):
            p["sent"] = sorted(sent | {k for k, _ in new})
            pid = p.pop("id")
            with db() as c:
                c.execute("UPDATE positions SET data=?, active=? WHERE id=?",
                          (json.dumps(p), 0 if "STOP" in p["sent"] else 1, pid))


# ------------------------------------------------------------------ worker
def next_run(now):
    """Next scan moment: just after a candle boundary, so a closed 1m candle is
    seen within ~SCAN_OFFSET seconds instead of up to 60 s later (v5.1)."""
    period = INTERVAL if INTERVAL < 60 else max(60, INTERVAL // 60 * 60)
    return (math.floor(now / period) + 1) * period + SCAN_OFFSET


def scan_once():
    start = time.time()
    refresh_quotes()
    rows, data_all, info_all, errors = {}, {}, {}, []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_symbol, s): s for s in SYMBOLS}
        for f in as_completed(futures):
            sym = futures[f]
            try:
                symbol, entry, data, info, problems = f.result()
                rows[symbol], data_all[symbol], info_all[symbol] = entry, data, info
                if problems:
                    errors.append(f"{sym}: {'; '.join(problems[:2])}")
            except Exception as e:
                rows[sym] = {"status": "ERROR", "reason": str(e)[:200]}
                errors.append(f"{sym}: {str(e)[:140]}")
    now = time.time()
    btc = info_all.get("BTCUSDT")
    setups, rejects = [], {}
    for sym, entry in rows.items():
        if entry.get("status") != "OK":
            continue
        for mode in MODES:
            s, why = eng.evaluate(sym, mode, data_all[sym], info_all[sym], now, entry["quote"], btc)
            if s:
                setups.append(s)
            else:
                rejects[why] = rejects.get(why, 0) + 1
    with lock:
        for s in setups:
            if s["id"] not in seen:
                seen.add(s["id"])
                active[s["id"]] = s
                try:
                    save_signal(s)
                except Exception:
                    log.exception("Signal journal write failed")
        for k in [k for k, s in active.items() if now - s["createdAt"] > ACTIVE_TTL[s["mode"]]]:
            del active[k]
        listed = sorted(active.values(), key=lambda s: (-eng.QUALITY_RANK.get(s["quality"], 0), -s["score"]))
        state.update(symbols=rows, setups=listed, lastScan=int(now), quotesUpdated=last_quotes,
                     errors=errors[:30], scanDuration=round(time.time() - start, 1), rejects=rejects)
        if any(x.get("status") == "OK" for x in rows.values()):
            state["lastSuccessfulScan"] = int(now)
    try:
        paper_track(data_all, now)
    except Exception:
        log.exception("Paper tracker failed")
    try:
        maybe_alert([s for s in setups if s["id"] in active], now)
    except Exception:
        log.exception("Alerting failed")
    monitor_positions(rows, now)
    log.info("scan %.1fs ok=%d setups=%d rejects=%s", time.time() - start,
             sum(r.get("status") == "OK" for r in rows.values()), len(setups), rejects)


def worker():
    while True:
        try:
            scan_once()
        except Exception as e:
            log.exception("Scan failure")
            with lock:
                state["errors"] = [f"Scanner: {str(e)[:200]}"]
        time.sleep(max(1.0, next_run(time.time()) - time.time()))


# ------------------------------------------------------------------ API
def with_live_flags(setups, now):
    out = []
    for s in setups:
        s = dict(s)
        s["live"] = eng.freshness(s, now, (s.get("quote") or {}).get("lastPrice"))
        out.append(s)
    return out


@app.get("/api/status")
def status():
    now = time.time()
    with lock:
        result = dict(state)
        result["symbols"] = dict(state["symbols"])
        result["setups"] = with_live_flags(state["setups"], now)
        result["errors"] = list(state["errors"])
    try:
        result["metrics"], result["paper"] = paper_stats()
    except Exception:
        log.exception("Paper stats failed")
        result["metrics"], result["paper"] = {"error": "tracker unavailable"}, []
    result["scannerVersion"] = VERSION
    result["scannerAlerts"] = SCANNER_ALERTS
    result["alertMinQuality"] = ALERT_MIN_QUALITY
    result["monitor"] = {"stale": not bool(result.get("lastScan") and now - result["lastScan"] < 180),
                         "marketsOK": sum(v.get("status") == "OK" for v in result["symbols"].values()),
                         "lastSuccessfulScan": result.get("lastSuccessfulScan")}
    result["persistence"] = "ephemeral" if DB_PATH.startswith("/tmp") else "disk"
    return jsonify(result)


@app.get("/api/paper")
def paper_results():
    try:
        metrics, results = paper_stats()
        return jsonify({"metrics": metrics, "results": results,
                        "method": (f"Fill op open van eerste 1m-candle >= {PAPER_LATENCY}s na signaal, "
                                   f"slippage {PAPER_SLIP_PCT}%, fees {PAPER_FEE_PCT}% roundtrip (aanname). "
                                   "Stop wint als stop en doel in dezelfde candle vallen. Exit volledig op TP1."),
                        "persistence": "ephemeral" if DB_PATH.startswith("/tmp") else "disk"})
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 503


@app.get("/api/stats")
def stats():
    recs = [r for r in paper_records() if r["status"] == "CLOSED"]
    recs.reverse()
    return jsonify({"all": eng.summarize(recs),
                    "byMode": eng.grouped(recs, lambda r: r["mode"]),
                    "byQuality": eng.grouped(recs, lambda r: r.get("quality")),
                    "byScore": eng.grouped(recs, lambda r: eng.bracket(r.get("score") or 0)),
                    "bySide": eng.grouped(recs, lambda r: r["side"]),
                    "bySymbol": eng.grouped(recs, lambda r: r["symbol"]),
                    "warning": "Heuristiek; geen bewijs van winstgevendheid zolang n < 100 per groep."})


@app.get("/api/history")
def signal_history():
    try:
        with db() as c:
            rows = [json.loads(r[0]) for r in c.execute(
                "SELECT data FROM signals ORDER BY created_at DESC LIMIT 100")]
        return jsonify({"signals": rows,
                        "persistence": "ephemeral" if DB_PATH.startswith("/tmp") else "disk"})
    except Exception as e:
        return jsonify({"signals": [], "error": str(e)[:150]}), 503


@app.get("/api/health")
def health():
    with lock:
        age = time.time() - state["lastScan"] if state["lastScan"] else None
        ok = sum(1 for v in state["symbols"].values() if v.get("status") == "OK")
        unavailable = [s for s, v in state["symbols"].items() if v.get("status") == "UNAVAILABLE"]
    return jsonify({"version": VERSION, "scannerCurrent": bool(age is not None and 0 <= age < 180 and ok > 0),
                    "lastScanAgeSec": round(age, 1) if age is not None else None, "marketsOK": ok,
                    "unavailableSymbols": unavailable, "scannerAlerts": SCANNER_ALERTS,
                    "persistence": "ephemeral" if DB_PATH.startswith("/tmp") else "disk"})


@app.get("/api/quotes")
def quotes():
    with lock:
        return jsonify({"updated": state["quotesUpdated"],
                        "quotes": {s: v.get("quote") for s, v in state["symbols"].items()}})


def authorized():
    if not ACCESS_TOKEN:
        return False
    given = request.headers.get("X-Access-Token") or request.args.get("key") or ""
    return hmac.compare_digest(given, ACCESS_TOKEN)


@app.route("/api/positions", methods=["GET", "POST"])
def positions():
    if not authorized():
        return jsonify(error="Positie-tracking uit of toegangscode onjuist (APP_ACCESS_TOKEN)"), 403
    if request.method == "GET":
        return jsonify(positions=load_positions(False)[:50])
    j = request.get_json(silent=True) or {}
    try:
        p = {"symbol": str(j["symbol"]).upper().replace("/", "").replace("-", ""),
             "side": str(j["side"]).upper(), "entry": float(j["entry"]),
             "stop": float(j["stop"]) if j.get("stop") else None,
             "target": float(j["target"]) if j.get("target") else None,
             "liquidation": float(j["liquidation"]) if j.get("liquidation") else None,
             "leverage": float(j.get("leverage") or 1), "margin": float(j.get("margin") or 0) or None,
             "chat_id": int(j["chat_id"]) if str(j.get("chat_id") or "").lstrip("-").isdigit() else None,
             "createdAt": int(time.time()), "sent": []}
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Ongeldige invoer"), 400
    if p["symbol"] not in SYMBOLS or p["side"] not in ("LONG", "SHORT") or p["entry"] <= 0:
        return jsonify(error="Onbekend pair, richting of entry"), 400
    with db() as c:
        cur = c.execute("INSERT INTO positions(active,created_at,data) VALUES (1,?,?)", (p["createdAt"], json.dumps(p)))
        pid = cur.lastrowid
    warn = None if p["stop"] else "Geen stop opgegeven: bij hoge leverage is dit het grootste risico."
    return jsonify(id=pid, warning=warn)


@app.post("/api/positions/<int:pid>/close")
def close_position(pid):
    if not authorized():
        return jsonify(error="Toegangscode onjuist"), 403
    with db() as c:
        c.execute("UPDATE positions SET active=0 WHERE id=?", (pid,))
    return jsonify(ok=True)


@app.get("/")
def home():
    return Response(HOME_HTML, mimetype="text/html")


HOME_HTML = r"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a0f1a"><title>WEEX Moon</title>
<style>
:root{color-scheme:dark;--bg:#0a0f1a;--card:#131c2c;--card2:#0e1625;--line:#243349;--muted:#8fa2bd;--text:#eef3fb;
--green:#5fe0a8;--red:#ff8597;--amber:#ffc46b;--accent:#ff7a5c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:760px;margin:auto;padding:max(16px,env(safe-area-inset-top)) 16px 60px}
header{display:flex;justify-content:space-between;align-items:center}
.brand{font-weight:800;font-size:20px;letter-spacing:-.5px}.brand em{font-style:normal;color:var(--accent)}
.pill{padding:6px 11px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:16px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:11px}
.stat small{display:block;color:var(--muted);font-size:11px;letter-spacing:.3px}.stat b{font-size:20px}
h2{font-size:15px;margin:26px 0 10px;display:flex;justify-content:space-between;align-items:baseline}
h2 small{color:var(--muted);font-weight:400;font-size:12px}
.setup{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:10px}
.setup.LONG{border-left:4px solid var(--green)}.setup.SHORT{border-left:4px solid var(--red)}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px}
.sym{font-weight:700;font-size:17px}.q{font-weight:800;border-radius:8px;padding:3px 8px;font-size:12px}
.qA{background:#163d30;color:var(--green)}.qB{background:#3d3218;color:var(--amber)}.qC{background:#3d1d24;color:var(--red)}
.lv{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:10px 0}
.lv div{background:var(--card2);border-radius:10px;padding:8px}.lv small{display:block;color:var(--muted);font-size:11px}
.sub{color:var(--muted);font-size:12.5px}.warn{color:var(--amber);font-size:12.5px;margin-top:6px}
.stale{opacity:.55}.empty{border:1px dashed var(--line);border-radius:14px;padding:16px;color:var(--muted);font-size:14px}
details{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-top:10px}
summary{cursor:pointer;font-weight:600}table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
td,th{padding:6px 2px;text-align:left;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:500}
a.btn{display:block;text-align:center;background:var(--accent);color:#1a0d08;font-weight:700;border-radius:12px;padding:13px;text-decoration:none;margin-top:18px}
footer{color:var(--muted);font-size:12px;margin-top:26px}
</style></head><body><main>
<header><div class="brand">WEEX <em>MOON</em></div><span class="pill" id="health">Verbinden…</span></header>
<div class="stats"><div class="stat"><small>MARKTEN OK</small><b id="ok">—</b></div>
<div class="stat"><small>LAATSTE SCAN</small><b id="age">—</b></div><div class="stat"><small>PAPER n</small><b id="pn">—</b></div></div>
<h2>⚡ Scalp <small>1m trigger · 5m/15m/1h context</small></h2><div id="SCALP"></div>
<h2>📈 Intraday <small>15m trigger · 1h/4h/1d context</small></h2><div id="INTRADAY"></div>
<details><summary>Niet-actueel of zwakkere setups (<span id="wn">0</span>)</summary><div id="watch"></div></details>
<details><summary>Paper-resultaten</summary><div id="paper"></div></details>
<details><summary>Waarom geen setups? (filters deze scan)</summary><div id="rej" class="sub"></div></details>
<details><summary>Markten</summary><div id="markets"></div></details>
<a class="btn" href="/trade">Trade Analyzer →</a>
<footer>Score en kwaliteit zijn heuristieken, geen winstkans. Entry is een referentie (gesloten candle), geen uitvoerbare prijs.
Geen automatische orders. Verlies bij stop ≈ stopafstand% × leverage van je margin.</footer>
</main><script>
const $=id=>document.getElementById(id),esc=x=>String(x??'—').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
num=x=>x==null?'—':Number(x).toLocaleString('en-US',{maximumFractionDigits:8});
function card(s,dim){let l=s.live||{};return '<div class="setup '+s.side+(dim?' stale':'')+'"><div class="row"><span class="sym">'+esc(s.symbol)+' '+esc(s.side)+'</span><span class="q q'+esc(s.quality)+'">'+esc(s.quality)+' · '+esc(s.score)+'</span></div>'+
'<div class="sub">'+(l.fresh?'⏱ '+Math.round(l.ageSec)+'s oud':'⏸ niet meer actueel')+(l.driftR!=null?' · prijs '+(l.driftR>0?'+':'')+l.driftR+'R t.o.v. entry':'')+'</div>'+
'<div class="lv"><div><small>Entry ref</small>'+num(s.entry)+'</div><div><small>Stop</small>'+num(s.stop)+'</div><div><small>TP1 · TP2</small>'+num(s.tp1)+'<br>'+num(s.tp2)+'</div></div>'+
'<div class="sub">'+esc(s.reason)+' · stop '+esc(s.riskPct)+'%</div>'+((s.warnings||[]).length?'<div class="warn">⚠ '+s.warnings.map(esc).join(' · ')+'</div>':'')+((s.notes||[]).length?'<div class="sub">ℹ '+s.notes.map(esc).join(' · ')+'</div>':'')+'</div>'}
function render(d){let all=Object.entries(d.symbols||{}),ok=all.filter(([s,v])=>v.status==='OK').length,age=d.lastScan?Math.max(0,Math.round(Date.now()/1000-d.lastScan)):null,live=age!=null&&age<180&&ok>0;
$('ok').textContent=ok+'/'+all.length;$('age').textContent=age==null?'—':age+'s';$('health').textContent=live?'● Actief':'● Niet actueel';$('health').style.color=live?'#5fe0a8':'#ffc46b';
let m=d.metrics||{};$('pn').textContent=m.n??0;
let top=s=>s.live&&s.live.actionable&&s.quality==='A',watch=(d.setups||[]).filter(s=>!top(s));
for(const mode of ['SCALP','INTRADAY']){let xs=(d.setups||[]).filter(s=>s.mode===mode&&top(s));$(mode).innerHTML=xs.length?xs.map(s=>card(s)).join(''):'<div class="empty">Geen actuele A-setup. Wachten is normaal.</div>'}
$('wn').textContent=watch.length;$('watch').innerHTML=watch.map(s=>card(s,true)).join('')||'<p class="sub">Geen.</p>';
$('paper').innerHTML='<p class="sub">'+(m.n?('n='+m.n+' · winrate '+m.winRate+'% · verwachting '+m.expectancyR+'R/trade · netto '+m.netR+'R · max DD '+m.maxDrawdownR+'R'+(m.note?'<br>'+esc(m.note):'')):'Nog geen afgesloten paper-trades.')+'</p><table><tr><th>Pair</th><th>Mode</th><th>Q</th><th>Status</th><th>R</th></tr>'+
(d.paper||[]).slice(0,20).map(p=>'<tr><td>'+esc(p.symbol)+' '+esc(p.side[0])+'</td><td>'+esc(p.mode)+'</td><td>'+esc(p.quality)+'</td><td>'+esc(p.exitReason||p.status)+'</td><td>'+(p.netR??'—')+'</td></tr>').join('')+'</table>';
$('rej').innerHTML=Object.entries(d.rejects||{}).map(([k,v])=>esc(k)+': '+v).join('<br>')||'—';
$('markets').innerHTML='<table><tr><th>Pair</th><th>Status</th><th>Mark</th><th>Spread</th></tr>'+all.sort((a,b)=>a[0].localeCompare(b[0])).map(([s,v])=>{let q=v.quote||{};return '<tr><td>'+esc(s)+'</td><td>'+esc(v.status)+'</td><td>'+num(q.markPrice)+'</td><td>'+num(q.spreadPct)+'%</td></tr>'}).join('')+'</table>'}
async function refresh(){try{let r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error();render(await r.json())}catch(e){$('health').textContent='● Geen verbinding';$('health').style.color='#ff8597'}}
refresh();setInterval(refresh,15000);
</script></body></html>"""


from trade_analyzer import register_trade_analyzer  # noqa: E402
register_trade_analyzer(app, state, lock, SYMBOLS)

if os.getenv("SCANNER_DISABLE_WORKER") != "1":
    threading.Thread(target=worker, daemon=True, name="scanner").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
