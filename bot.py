import os
import time
import json
import requests
from datetime import datetime
import pytz

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")

SCAN_INTERVAL     = 60
CONFIDENCE_MIN    = 80
SPIKE_PTS         = 3.0
IST               = pytz.timezone("Asia/Kolkata")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=" + GEMINI_API_KEY if GEMINI_API_KEY else ""

last_direction  = None
last_price      = None
prev_kz         = False

PROMPT = """You are an elite XAUUSD trader using SMC and ICT methodology.
FOREX.com price scale — all prices must be around 4700-4730 range.

Analyse XAUUSD and respond ONLY in this exact JSON with no markdown:
{
  "direction": "BUY or SELL or WAIT",
  "confidence_pct": 84,
  "entry": "4712.00",
  "entry_reason": "Bullish OB retest",
  "sl": "4706.00",
  "sl_reason": "Below OB",
  "tp1": "4722.00",
  "tp1_reason": "Buyside liquidity",
  "tp2": "4730.00",
  "tp2_reason": "Session high",
  "rr": "1:2.4",
  "setup": "Bullish OB + FVG",
  "structure": "Market structure summary",
  "confluences": ["OB", "FVG", "Discount"],
  "session": "London Kill Zone",
  "reasoning": "Short rationale"
}"""

def ist_now():
    return datetime.now(IST).strftime("%I:%M %p IST")

def utc_hour():
    return datetime.now(pytz.utc).hour

def in_kz():
    h = utc_hour()
    return (0<=h<3) or (7<=h<10) or (12<=h<17)

def kz_name():
    h = utc_hour()
    if 0<=h<3:  return "Asia Kill Zone"
    if 7<=h<10: return "London Kill Zone"
    if 12<=h<15: return "NY AM Kill Zone"
    if 15<=h<17: return "NY PM Kill Zone"
    return "Off-hours"

def is_weekend():
    return datetime.now(pytz.utc).weekday() >= 5

def tg(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        if r.json().get("ok"):
            print("[TG] Sent")
        else:
            print(f"[TG] Error: {r.json().get('description')}")
    except Exception as e:
        print(f"[TG] Failed: {e}")

def analyse(trigger="SCHEDULED"):
    global last_direction
    print(f"[SCAN] {trigger} — {ist_now()}")
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        h = utc_hour()
        sess = "London KZ" if 7<=h<10 else "NY AM KZ" if 12<=h<15 else "NY PM KZ" if 15<=h<17 else "Off-hours"
        body = {
            "contents": [{"parts": [{"text": PROMPT + f"\n\nIST: {ist_now()}\nSession: {sess}\nUTC hour: {h}\nTrigger: {trigger}"}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 800}
        }
        r = requests.post(url, json=body, timeout=30)
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = raw.replace("```json","").replace("```","").strip()
        s = json.loads(raw)

        d = s.get("direction","WAIT")
        c = int(s.get("confidence_pct", 0))
        print(f"[RESULT] {d} | {c}% | {s.get('setup','')}")

        if d == "WAIT":
            print(f"[WAIT] {s.get('structure','')}")
            return
        if c < CONFIDENCE_MIN:
            print(f"[SKIP] {c}% < {CONFIDENCE_MIN}%")
            return
        if d == last_direction:
            print(f"[SKIP] Duplicate {d}")
            return

        last_direction = d
        em = "🟢" if d=="BUY" else "🔴"
        tl = "⚡ Kill Zone Alert" if trigger=="KZ_ALERT" else "⚡ Spike Alert" if trigger=="SPIKE_ALERT" else "🔄 Scheduled"
        cf = " · ".join(s.get("confluences",[]))
        msg = (
            f"{em} <b>XAUUSD {d}</b> · {tl}\n"
            f"{ist_now()} · {s.get('session','')}\n\n"
            f"📍 {s.get('setup','')}\n"
            f"🎯 Confidence: {c}%\n\n"
            f"💰 <b>Entry:</b> {s.get('entry','')}\n"
            f"   <i>{s.get('entry_reason','')}</i>\n"
            f"🛑 <b>SL:</b> {s.get('sl','')}\n"
            f"   <i>{s.get('sl_reason','')}</i>\n"
            f"🎯 <b>TP1:</b> {s.get('tp1','')}\n"
            f"   <i>{s.get('tp1_reason','')}</i>\n"
            f"🎯 <b>TP2:</b> {s.get('tp2','')}\n"
            f"   <i>{s.get('tp2_reason','')}</i>\n"
            f"⚖️ <b>RR:</b> {s.get('rr','')}\n\n"
            f"📊 {s.get('structure','')}\n"
            f"✅ {cf}\n\n"
            f"💡 {s.get('reasoning','')}\n\n"
            f"🤖 XAUUSD Agent · FOREX.com"
        )
        tg(msg)
        print(f"[SIGNAL] {d} sent!")

    except Exception as e:
        print(f"[ERROR] {e}")

def check_kz():
    global prev_kz
    now = in_kz()
    if now and not prev_kz:
        name = kz_name()
        print(f"[KZ] {name} opened!")
        tg(f"⚡ <b>{name} Opened!</b>\nRunning instant analysis...\n🕐 {ist_now()}")
        analyse("KZ_ALERT")
    prev_kz = now

def fetch_price():
    try:
        r = requests.get("https://api.metals.live/v1/spot/gold", timeout=5)
        return round(float(r.json().get("price", 0)) * 2, 2)
    except:
        return 0.0

def check_spike(price):
    global last_price
    if last_price and price > 0:
        move = abs(price - last_price)
        if move >= SPIKE_PTS:
            d = "UP ⬆️" if price > last_price else "DOWN ⬇️"
            print(f"[SPIKE] {move:.2f} pts {d}")
            tg(f"⚡ <b>Price Spike!</b>\nXAUUSD moved <b>{move:.2f} pts {d}</b>\n💰 {price}\n🕐 {ist_now()}")
            analyse("SPIKE_ALERT")
    last_price = price

def main():
    print("=" * 45)
    print("  XAUUSD Bot — Gemini FREE — Railway")
    print(f"  Started: {ist_now()}")
    print("=" * 45)
    tg(
        "✅ <b>XAUUSD Agent LIVE!</b>\n\n"
        "⚡ Every 1 min scan\n"
        "🔔 Kill zone + spike alerts\n"
        f"🎯 Min confidence: {CONFIDENCE_MIN}%\n"
        "📊 FOREX.com scale\n"
        f"🕐 {ist_now()}"
    )
    n = 0
    while True:
        try:
            if is_weekend():
                print("[WEEKEND] Sleeping 1hr")
                time.sleep(3600)
                continue
            check_kz()
            p = fetch_price()
            if p: check_spike(p)
            n += 1
            print(f"[TICK #{n}] {ist_now()} | {p}")
            analyse("SCHEDULED")
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            tg("⏸ Agent stopped.")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
