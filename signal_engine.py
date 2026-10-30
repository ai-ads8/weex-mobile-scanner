"""WEEX Moon signal engine v6.0 — pure functions, no network, no clock reads.

Everything here takes its inputs explicitly (candles, quote, `now`) so the exact
same logic runs live (app.py), in the backtest (backtest.py) and in tests.

IMPORTANT: the 0-100 score and the A/B/C quality are heuristics. Neither is a
probability of winning. Thresholds below are starting assumptions that must be
validated with the paper tracker and backtest before anyone trusts them.
"""
import math
import os

TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

# Same timeframe roles as v5.1 (trigger, direction, two confirmations), plus the
# per-mode timing rules that v5.1 lacked.
MODES = {
    "SCALP": {
        "trigger": "1m", "direction": "5m", "confirm": ("15m", "1h"),
        "max_signal_age": 90,          # sec after trigger-candle close; a 1m setup older than 1.5 bars is stale
        "max_hold": 60 * 60,           # paper-trade time stop
        "reassess": "bij elke 1m-close (1-5 min)",
        "hold_hint": "ca. 1-15 min, soms tot 30 min",
    },
    "INTRADAY": {
        "trigger": "15m", "direction": "1h", "confirm": ("4h", "1d"),
        "max_signal_age": 420,
        "max_hold": 8 * 3600,
        "reassess": "bij elke 15m-close",
        "hold_hint": "ca. 30 min - 8 uur",
    },
}


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


# Starting assumptions (configurable, unvalidated).
CFG = {
    "min_volume_ratio": _f("MIN_VOLUME_RATIO", 1.1),        # unchanged from v5.1
    "max_spread_pct": _f("MAX_SPREAD_PCT", 0.3),            # unchanged hard reject
    "warn_spread_pct": _f("WARN_SPREAD_PCT", 0.08),
    "max_extension_atr": _f("MAX_EXTENSION_ATR", 1.0),      # close too far beyond breakout level = chasing
    "min_close_location": _f("MIN_CLOSE_LOCATION", 0.6),    # close in the outer 40% of the bar
    "climax_range_atr": _f("CLIMAX_RANGE_ATR", 3.0),        # bar range vs ATR -> exhaustion warning
    "climax_volume_ratio": _f("CLIMAX_VOLUME_RATIO", 4.0),
    "max_risk_atr": _f("MAX_RISK_ATR", 4.0),                # stop farther than 4 ATR = poor R for this timeframe
    "max_drift_r": _f("MAX_DRIFT_R", 0.5),                  # live price beyond entry by >0.5R = too late
}


# ---------------------------------------------------------------- indicators
def ema(values, n):
    z = sum(values[:n]) / n
    k = 2 / (n + 1)
    for x in values[n:]:
        z = k * x + (1 - k) * z
    return z


def atr(candles, n=14):
    tr = [max(candles[i]["h"] - candles[i]["l"], abs(candles[i]["h"] - candles[i - 1]["c"]),
              abs(candles[i]["l"] - candles[i - 1]["c"])) for i in range(1, len(candles))]
    return sum(tr[-n:]) / n


def timeframe_info(candles):
    closes = [x["c"] for x in candles]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    prior = candles[-21:-1]
    a = atr(candles)
    return {"trend": "UP" if e20 > e50 else "DOWN",
            "lastClosedPrice": closes[-1], "lastClosedAt": candles[-1]["t"], "candleCount": len(candles),
            "ema20": round(e20, 9), "ema50": round(e50, 9),
            "atr": a, "atrPct": round(100 * a / closes[-1], 4) if closes[-1] else None,
            "rangeHigh": max(x["h"] for x in prior), "rangeLow": min(x["l"] for x in prior)}


# ---------------------------------------------------------------- scoring
def legacy_score(ratio, risk_pct, spread_pct, candle_age, quote_age, alignment=3):
    """Exact v5.1 formula, kept for continuity and for comparison in validation.
    Heuristic quality indicator, NOT a probability of profit."""
    parts = legacy_components(ratio, risk_pct, spread_pct, candle_age, quote_age, alignment)
    return max(0, min(100, round(sum(parts.values()))))


def legacy_components(ratio, risk_pct, spread_pct, candle_age, quote_age, alignment=3):
    return {"volume": min(25, max(0, (ratio - 1) * 12)),
            "structure": 25 if alignment == 3 else 10,
            "freshness": max(0, 20 - candle_age / 12 - max(0, quote_age) / 5),
            "execution": max(0, 15 - (spread_pct or 0) * 45),
            "risk": max(0, 15 - abs(risk_pct - 1.0) * 6)}


def tier_for(score):
    return "STRONG" if score >= 80 else "GOOD" if score >= 60 else "EARLY"


def leverage_note(risk_pct):
    """Plain arithmetic: loss at the technical stop as % of position margin is
    roughly risk% x leverage (before fees). Liquidation in isolated margin comes
    a bit before -100%; cross margin depends on the whole account."""
    if not risk_pct or risk_pct <= 0:
        return None
    max_lev = max(1, int(90 / risk_pct))  # stop reached before ~-90% of margin
    return {"lossAtStopPctOfMarginPer10x": round(risk_pct * 10, 1),
            "maxLeverageStopBeforeMarginGone": max_lev,
            "text": (f"Stopafstand {risk_pct:.2f}%: bij 10x ≈ -{risk_pct*10:.0f}% van je margin, "
                     f"bij 100x ≈ -{risk_pct*100:.0f}%. Boven ~{max_lev}x is je margin (isolated) "
                     f"vóór de stop op. Cross margin trekt je hele account mee.")}


# ---------------------------------------------------------------- setup evaluation
def evaluate(symbol, mode, data, info, now, quote, btc_info=None):
    """Return (setup | None, reject_reason | None). `now` in unix seconds."""
    m = MODES[mode]
    tf = m["trigger"]
    a = data[tf]
    direction = info[m["direction"]]["trend"]
    aligned = sum(info[x]["trend"] == direction for x in (m["direction"],) + m["confirm"])
    if aligned < 2:
        return None, "trend niet uitgelijnd"
    last = a[-1]
    p = last["c"]
    prior = a[-21:-1]
    avg = sum(x["v"] for x in prior) / len(prior)
    ratio = last["v"] / avg if avg > 0 else 0
    high = max(x["h"] for x in prior)
    low = min(x["l"] for x in prior)
    side = "LONG" if direction == "UP" and p > high else "SHORT" if direction == "DOWN" and p < low else None
    if not side:
        return None, "geen breakout"
    if ratio < CFG["min_volume_ratio"]:
        return None, "volume te laag"
    candle_age = now - (last["t"] / 1000 + TIMEFRAMES[tf])
    if candle_age < 0 or candle_age > m["max_signal_age"]:
        return None, "signaal te oud"
    q = quote or {}
    if (not q.get("quoteFresh") or not q.get("lastPrice") or not q.get("markPrice")
            or q.get("spreadPct") is None or q["spreadPct"] > CFG["max_spread_pct"]
            or q.get("bidPrice") is None or q.get("askPrice") is None):
        return None, "quote/spread ongeschikt"
    distance = atr(a)
    if distance <= 0:
        return None, "ATR nul"
    stop = min(low, p - 1.5 * distance) if side == "LONG" else max(high, p + 1.5 * distance)
    risk = abs(p - stop)
    if not .001 <= risk / p <= .045:
        return None, "stopafstand buiten bereik"
    if abs(q["lastPrice"] - p) / p > .008:
        return None, "prijs al >0.8% weg van candle-close"
    sign = 1 if side == "LONG" else -1
    risk_pct = 100 * risk / p

    # ---- entry-quality diagnostics (new in v6.0)
    level = high if side == "LONG" else low
    extension_atr = sign * (p - level) / distance
    bar = last["h"] - last["l"]
    close_loc = ((p - last["l"]) / bar if side == "LONG" else (last["h"] - p) / bar) if bar > 0 else 1.0
    range_atr = bar / distance
    risk_atr = risk / distance
    drift_r = sign * (q["lastPrice"] - p) / risk          # + = moved our way (late), - = back toward stop
    mark_gap_pct = 100 * (q["markPrice"] - q["lastPrice"]) / q["lastPrice"]

    warnings = []
    if aligned < 3:
        warnings.append(f"slechts {aligned}/3 timeframes in lijn")
    if extension_atr > CFG["max_extension_atr"]:
        warnings.append(f"achterna-jagen: close {extension_atr:.1f} ATR voorbij breakout-niveau")
    if close_loc < CFG["min_close_location"]:
        warnings.append("afwijzingswick: candle sloot ver van zijn extreme")
    if range_atr >= CFG["climax_range_atr"] or ratio >= CFG["climax_volume_ratio"]:
        warnings.append(f"climax-risico: candle {range_atr:.1f} ATR groot, volume {ratio:.1f}x")
    notes = []   # informative, does not lower quality (needs data before it may)
    if risk_atr > CFG["max_risk_atr"]:
        notes.append(f"stop ver weg ({risk_atr:.1f} ATR): TP1 ligt {2 * risk_atr:.0f} ATR verder")
    if q["spreadPct"] > CFG["warn_spread_pct"]:
        warnings.append(f"spread {q['spreadPct']:.3f}%")
    if drift_r > CFG["max_drift_r"]:
        warnings.append(f"te laat: prijs al {drift_r:.2f}R voorbij entry")
    if drift_r < -CFG["max_drift_r"]:
        warnings.append(f"breakout faalt: prijs {abs(drift_r):.2f}R terug richting stop")
    btc_trend = None
    if btc_info and symbol != "BTCUSDT":
        btc_trend = (btc_info.get(m["direction"]) or {}).get("trend")
        if btc_trend and btc_trend != direction:
            warnings.append(f"tegen BTC in ({m['direction']} BTC {btc_trend})")

    quality = "A" if not warnings else "B" if len(warnings) <= 2 else "C"
    comps = legacy_components(ratio, risk_pct, q["spreadPct"], candle_age, q.get("quoteAgeSec") or 0, aligned)
    score = max(0, min(100, round(sum(comps.values()))))
    created = int(now)
    return {
        "id": f"{symbol}:{mode}:{side}:{last['t']}",
        "symbol": symbol, "mode": mode, "side": side, "triggerTf": tf,
        "directionTf": m["direction"], "htfTrend": {x: info[x]["trend"] for x in (m["direction"],) + m["confirm"]},
        "btcTrend": btc_trend,
        "entry": p, "stop": stop, "tp1": p + sign * 2 * risk, "tp2": p + sign * 3 * risk,
        "breakoutLevel": level,
        "riskPct": round(risk_pct, 3), "volumeRatio": round(ratio, 2),
        "score": score, "scoreComponents": {k: round(v, 1) for k, v in comps.items()},
        "scoreLabel": "Heuristische setupkwaliteit; geen winstkans",
        "tier": tier_for(score), "quality": quality, "warnings": warnings, "notes": notes,
        "diagnostics": {"extensionAtr": round(extension_atr, 2), "closeLocation": round(close_loc, 2),
                        "rangeAtr": round(range_atr, 2), "riskAtr": round(risk_atr, 2),
                        "driftR": round(drift_r, 2), "markGapPct": round(mark_gap_pct, 4),
                        "candleAgeSec": round(candle_age, 1)},
        "alignment": aligned,
        "leverage": leverage_note(risk_pct),
        "reassess": m["reassess"], "holdHint": m["hold_hint"], "maxSignalAgeSec": m["max_signal_age"],
        "createdAt": created, "candleAt": last["t"], "candleCloseAt": int(last["t"] / 1000 + TIMEFRAMES[tf]),
        "status": "PAPER_OPEN",
        "reason": (f"{tf}-candle sloot {'boven' if side == 'LONG' else 'onder'} de {20}-candle "
                   f"{'high' if side == 'LONG' else 'low'}; {aligned}/3 trend; volume {ratio:.1f}x"),
        "source": "WEEX gesloten candles; entry is referentie, geen uitvoerbare prijs",
        "quote": q,
    }, None


def freshness(setup, now, last_price=None):
    """Live check at display/alert time. Returns dict with fresh/actionable flags."""
    age = now - setup.get("candleCloseAt", setup.get("createdAt", 0))
    max_age = setup.get("maxSignalAgeSec") or MODES.get(setup.get("mode"), MODES["INTRADAY"])["max_signal_age"]
    out = {"ageSec": round(age, 1), "fresh": 0 <= age <= max_age, "driftR": None, "actionable": False}
    risk = abs(setup["entry"] - setup["stop"]) or None
    if last_price and risk:
        sign = 1 if setup["side"] == "LONG" else -1
        out["driftR"] = round(sign * (last_price - setup["entry"]) / risk, 2)
    drift_ok = out["driftR"] is None or -CFG["max_drift_r"] <= out["driftR"] <= CFG["max_drift_r"]
    out["actionable"] = out["fresh"] and drift_ok
    return out


QUALITY_RANK = {"A": 3, "B": 2, "C": 1}


def passes_alert(setup, min_quality="A"):
    return QUALITY_RANK.get(setup.get("quality"), 0) >= QUALITY_RANK.get(min_quality, 3)


# ---------------------------------------------------------------- paper simulation
def new_paper(setup, latency_sec=20, fee_pct=0.12, slip_pct=0.03):
    """Paper record. Entry is filled at the open of the first 1m candle that starts
    at least `latency_sec` after the signal (models alert + human delay)."""
    return {"id": setup["id"], "symbol": setup["symbol"], "side": setup["side"], "mode": setup["mode"],
            "entry": setup["entry"], "stop": setup["stop"], "tp1": setup["tp1"], "tp2": setup["tp2"],
            "score": setup.get("score"), "quality": setup.get("quality"),
            "createdAt": setup["createdAt"], "fillAfter": setup["createdAt"] + latency_sec,
            "feePct": fee_pct, "slipPct": slip_pct,
            "maxHold": MODES[setup["mode"]]["max_hold"],
            "status": "PENDING", "fill": None, "fillAt": None, "lastT": 0,
            "mfeR": 0.0, "maeR": 0.0, "exitPrice": None, "exitAt": None, "exitReason": None,
            "grossR": None, "netR": None}


def step_paper(rec, candles_1m, target="tp1"):
    """Advance a paper record over confirmed 1m candles (dicts t,o,h,l,c in ms).
    Conservative: if stop and target fall inside the same candle, the stop wins.
    Returns the (mutated) record."""
    if rec["status"] not in ("PENDING", "OPEN"):
        return rec
    sign = 1 if rec["side"] == "LONG" else -1
    plan_risk = abs(rec["entry"] - rec["stop"])
    if plan_risk <= 0:
        rec["status"], rec["exitReason"] = "INVALID", "risk nul"
        return rec
    tgt = rec[target]
    slip = rec["slipPct"] / 100
    for c in candles_1m:
        t = c["t"] / 1000
        if c["t"] <= rec["lastT"]:
            continue
        rec["lastT"] = c["t"]
        if rec["status"] == "PENDING":
            if t < rec["fillAfter"]:
                continue
            fill = c["o"] * (1 + sign * slip)
            if sign * (fill - rec["stop"]) <= 0:
                rec.update(status="SKIPPED", exitReason="prijs al voorbij stop bij fill", fill=fill, fillAt=t)
                return rec
            rec.update(status="OPEN", fill=fill, fillAt=t)
        hi_r = sign * ((c["h"] if sign > 0 else c["l"]) - rec["fill"]) / plan_risk
        lo_r = sign * ((c["l"] if sign > 0 else c["h"]) - rec["fill"]) / plan_risk
        rec["mfeR"] = round(max(rec["mfeR"], hi_r), 3)
        rec["maeR"] = round(min(rec["maeR"], lo_r), 3)
        hit_stop = (c["l"] <= rec["stop"]) if sign > 0 else (c["h"] >= rec["stop"])
        hit_tgt = (c["h"] >= tgt) if sign > 0 else (c["l"] <= tgt)
        exit_price = reason = None
        if hit_stop:
            exit_price, reason = rec["stop"] * (1 - sign * slip), "SL"
        elif hit_tgt:
            exit_price, reason = tgt, target.upper()
        elif t + 60 - rec["fillAt"] >= rec["maxHold"]:
            exit_price, reason = c["c"] * (1 - sign * slip), "TIME"
        if exit_price is not None:
            gross = sign * (exit_price - rec["fill"]) / plan_risk
            fee_r = (rec["feePct"] / 100) * rec["fill"] / plan_risk
            rec.update(status="CLOSED", exitPrice=exit_price, exitAt=t + 60, exitReason=reason,
                       grossR=round(gross, 3), netR=round(gross - fee_r, 3))
            return rec
    return rec


# ---------------------------------------------------------------- statistics
def summarize(results):
    """results: iterable of closed records with netR. Returns metrics dict."""
    rs = [r["netR"] for r in results if r.get("netR") is not None]
    n = len(rs)
    if not n:
        return {"n": 0}
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    eq = peak = dd = 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    half = 1.96 * sd / math.sqrt(n) if n > 1 else None
    return {"n": n, "winRate": round(100 * len(wins) / n, 1),
            "avgWinR": round(sum(wins) / len(wins), 3) if wins else None,
            "avgLossR": round(sum(losses) / len(losses), 3) if losses else None,
            "expectancyR": round(mean, 3),
            "expectancyCI95": [round(mean - half, 3), round(mean + half, 3)] if half is not None else None,
            "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "netR": round(sum(rs), 2), "maxDrawdownR": round(dd, 2),
            "reliable": n >= 100,
            "note": "Minder dan 100 trades: niet genoeg om conclusies te trekken" if n < 100 else None}


def bracket(score):
    return "80-100" if score >= 80 else "60-79" if score >= 60 else "0-59"


def grouped(results, key):
    groups = {}
    for r in results:
        groups.setdefault(key(r), []).append(r)
    return {k: summarize(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}
