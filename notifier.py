"""Shared Telegram sending + message formatting (used by app.py and telegram_bot.py)."""
import logging
import time

import requests

HTTP = requests.Session()


def telegram(token, method, data, timeout=(5, 15)):
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ontbreekt")
    r = HTTP.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=timeout)
    if r.status_code == 429:  # respect Telegram flood control once
        try:
            wait = int(r.json().get("parameters", {}).get("retry_after", 3))
        except Exception:
            wait = 3
        time.sleep(min(wait, 30))
        r = HTTP.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=timeout)
    r.raise_for_status()
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram API response not OK")
    return result


def send(token, chat, message):
    text = str(message)
    while text:
        if len(text) <= 3500:
            part, text = text, ""
        else:
            cut = text.rfind("\n\n", 0, 3500)
            cut = 3500 if cut < 500 else cut
            part, text = text[:cut], text[cut:].lstrip()
        try:
            telegram(token, "sendMessage", {"chat_id": chat, "text": part, "disable_web_page_preview": True})
        except Exception as exc:  # never log the token-bearing URL
            logging.warning("Telegram send failed: %s", type(exc).__name__)
            return False
    return True


def chat_ids(raw):
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


def fmt(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v:.8f}".rstrip("0").rstrip(".")


def signal_text(s, fresh=None):
    """Alert text. `fresh` = signal_engine.freshness(...) result at send time."""
    side_icon = "🟢" if s.get("side") == "LONG" else "🔴"
    htf = s.get("htfTrend") or {}
    htf_txt = " · ".join(f"{k} {'↑' if v == 'UP' else '↓'}" for k, v in htf.items()) or "—"
    lines = [
        f"{side_icon} {s.get('mode', '?')} · {s.get('symbol', '?')} {s.get('side', '?')}",
        f"Kwaliteit {s.get('quality', '?')} · score {s.get('score', '—')}/100 (geen winstkans)",
        f"Trigger: {s.get('triggerTf', '?')}-close · trend {htf_txt}"
        + (f" · BTC {s['btcTrend']}" if s.get("btcTrend") else ""),
        "",
        f"Entry-referentie: {fmt(s.get('entry'))}",
        f"Invalidatie (stop): {fmt(s.get('stop'))}  ({fmt(s.get('riskPct'))}%)",
        f"TP1 (2R): {fmt(s.get('tp1'))} · TP2 (3R): {fmt(s.get('tp2'))}",
    ]
    if fresh:
        drift = fresh.get("driftR")
        lines.append(f"Leeftijd: {int(fresh['ageSec'])}s sinds candle-close"
                     + (f" · prijs nu {drift:+.2f}R t.o.v. entry" if drift is not None else ""))
        if not fresh.get("actionable"):
            lines.append("⚠️ NIET MEER ACTUEEL — niet instappen op dit signaal")
    lines.append(f"Herbeoordelen: {s.get('reassess', '—')} · indicatie {s.get('holdHint', '—')}")
    lines.append(f"Waarom: {s.get('reason', '—')}")
    if s.get("warnings"):
        lines.append("Let op: " + "; ".join(s["warnings"]))
    if s.get("notes"):
        lines.append("Info: " + "; ".join(s["notes"]))
    if s.get("leverage"):
        lines.append(s["leverage"]["text"])
    lines.append("Paper-signaal, geen order. Zet altijd zelf een stop.")
    return "\n".join(lines)
