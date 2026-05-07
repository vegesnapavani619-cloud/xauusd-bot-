import os
import time
import json
import requests
from datetime import datetime
import pytz

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")

SCAN_INTERVAL_SECONDS = 60        # every 1 minute
CONFIDENCE_THRESHOLD  = 80        # only signals >= 80%
SPIKE_THRESHOLD_PTS   = 3.0       # price move to trigger spike alert

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent?key={key}"
)

IST = pytz.timezone("Asia/Kolkata")

# ─── STATE ─────────────────────────────────────────────────────────────────────
last_direction  = None
last_price      = None
prev_kz_active  = False
signal_count    = 0
alert_count     = 0

# ─── SMC/ICT PROMPT ────────────────────────────────────────────────────────────
PROMPT = """You are an elite XAUUSD trader using Smart Money Concepts (SMC) and ICT methodology.

CRITICAL: FOREX.com broker price scale — XAUUSD trades around 4,700–4,730 on this broker.
ALL price outputs MUST use this FOREX.com scale (entry, SL, TP1, TP2 all in ~4,700 range).

Analyse current XAUUSD market and generate a high-accuracy trading signal using:
- Order Blocks (OB): most recent bullish/bearish OB
- Fair Value Gaps (FVG): open imbalances price will revisit
- Market Structure: BOS, CHOCH, MSS confirmation
- Liquidity: BSL/SSL pools, sweep confirmation before entry
- Premium/Discount: 50% equilibrium — buy discount, sell premium
- OTE Zone: 62-79% fibonacci for optimal entry
- Kill Zone weighting: London (07-10 UTC), NY AM (12-15 UTC)
- HTF Bias: daily/4H directional bias

CONFLUENCE RULES — only signal BUY/SELL with 3+ confluences, else output WAIT:
1. OB alignment with HTF bias
2. FVG present at entry zone
3. Liquidity swept before entry
4. Price in correct premium/discount zone
5. Kill zone active (bonus confluence)
6. MSS/CHOCH confirming direction

You MUST respond ONLY in this exact JSON format.
Do NOT include any markdown, backticks, or extra text — pure JSON only:
{
  "direction": "BUY or SELL or WAIT",
  "confidence_pct": 84,
  "current_price": "4718.50",
  "entry": "4712.00",
  "entry_reason": "Retest of bullish OB + open FVG at 4712",
  "sl": "4706.50",
  "sl_reason": "Below OB invalidation level",
  "tp1": "4724.00",
  "tp1_reason": "Buyside liquidity + session high",
  "tp2": "4731.00",
  "tp2_reason": "Daily FVG / NY AM high",
  "rr": "1:2.6",
  "setup": "Bullish OB + FVG + BSL sweep",
  "structure": "One sentence market structure summary",
  "confluences": ["Bullish OB", "Open FVG", "SSL swept", "Discount zone", "London KZ"],
  "htf_bias": "BULLISH",
  "session": "London Kill Zone",
  "reasoning": "2 sentence trade rationale"
}"""

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def ist_now():
    return datetime.now(IST).strftime("%I:%M %p IST")

def utc_hour():
    return datetime.utcnow().hour

def in_kill_zone():
    h = utc_hour()
    return (0 <= h < 3) or (7 <= h < 10) or (12 <= h < 17)

def kill_zone_name():
    h = utc_hour()
    if 0  <= h < 3:  return "Asia Kill Zone"
    if 7  <= h < 10: return "London Kill Zone"
    if 12 <= h < 15: return "NY AM Kill Zone"
    if 15 <= h < 17: return "NY PM Kill Zone"
    return "Off-hours"

def session_context():
    h = utc_hour()
    if 7  <= h < 10: return "London Kill Zone active"
    if 12 <= h < 15: return "NY AM Kill Zone active"
    if 15 <= h < 17: return "NY PM Kill Zone active"
    if 0  <= h < 3:  return "Asia Kill Zone active"
    return "Off-hours — no kill zone"

def is_weekend():
    return datetime.utcnow().weekday() >= 5

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram credentials missing")
        return False
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
        data = resp.json()
        if data.get("ok"):
            print("[TG] Message sent successfully")
            return True
        else:
            print(f"[WARN] Telegram error: {data.get('description')}")
            return False
    except Exception as e:
        print(f"[ERROR] Telegram failed: {e}")
        return False

def build_signal_message(sig: dict, trigger: str) -> str:
    direction = sig.get("direction", "WAIT")
    emoji     = "🟢" if direction == "BUY" else "🔴" if direction == "SELL" else "🟡"
    trigger_label = {
        "SCHEDULED":   "🔄 Scheduled Scan",
        "KZ_ALERT":    "⚡ Kill Zone Alert",
        "SPIKE_ALERT": "⚡ Price Spike Alert"
    }.get(trigger, "🔄 Scan")
    confluences = " · ".join(sig.get("confluences", []))
    return (
        f"{emoji} <b>XAUUSD {direction}</b> · {trigger_label}\n"
        f"{ist_now()} · {sig.get('session', '')}\n\n"
        f"📍 {sig.get('setup', '')}\n"
        f"🎯 Confidence: {sig.get('confidence_pct', 0)}%\n\n"
        f"💰 <b>Entry:</b>  {sig.get('entry', '')}\n"
        f"   <i>{sig.get('entry_reason', '')}</i>\n"
        f"🛑 <b>SL:</b>     {sig.get('sl', '')}\n"
        f"   <i>{sig.get('sl_reason', '')}</i>\n"
        f"🎯 <b>TP1:</b>    {sig.get('tp1', '')}\n"
        f"   <i>{sig.get('tp1_reason', '')}</i>\n"
        f"🎯 <b>TP2:</b>    {sig.get('tp2', '')}\n"
        f"   <i>{sig.get('tp2_reason', '')}</i>\n"
        f"⚖️ <b>RR:</b>     {sig.get('rr', '')}\n\n"
        f"📊 {sig.get('structure', '')}\n"
        f"✅ {confluences}\n"
        f"📈 HTF Bias: {sig.get('htf_bias', '')}\n\n"
        f"💡 {sig.get('reasoning', '')}\n\n"
        f"🤖 XAUUSD Agent · FOREX.com scale"
    )

# ─── GEMINI AI ─────────────────────────────────────────────────────────────────
def run_analysis(trigger: str = "SCHEDULED"):
    global last_direction, signal_count

    print(f"[SCAN] Trigger: {trigger} — {ist_now()}")

    try:
        now_ist    = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        full_prompt = (
            PROMPT
            + f"\n\nCurrent IST: {now_ist}"
            + f"\nSession: {session_context()}"
            + f"\nUTC hour: {utc_hour()}"
            + f"\nTrigger: {trigger}"
        )

        url  = GEMINI_URL.format(key=GEMINI_API_KEY)
        body = {
            "contents": [{
                "parts": [{"text": full_prompt}]
            }],
            "generationConfig": {
                "temperature":     0.3,
                "maxOutputTokens": 900
            }
        }

        resp = requests.post(url, json=body, timeout=30)
        data = resp.json()

        # Extract text from Gemini response
        raw = (
            data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
        )
        raw = raw.replace("```json", "").replace("```", "").strip()
        sig = json.loads(raw)

        direction  = sig.get("direction", "WAIT")
        confidence = int(sig.get("confidence_pct", 0))

        print(f"[RESULT] {direction} | {confidence}% | {sig.get('setup', '')}")

        if direction == "WAIT":
            print(f"[WAIT] {sig.get('structure', 'No confluence found')}")
            return

        if confidence < CONFIDENCE_THRESHOLD:
            print(f"[SKIP] Confidence {confidence}% < {CONFIDENCE_THRESHOLD}%")
            return

        if direction == last_direction:
            print(f"[SKIP] Duplicate {direction} — same as last signal")
            return

        # ✅ Valid signal — send to Telegram
        last_direction = direction
        signal_count  += 1
        send_telegram(build_signal_message(sig, trigger))
        print(f"[SIGNAL #{signal_count}] {direction} sent to Telegram ✅")

    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parse error: {e} | Raw: {raw[:200]}")
    except Exception as e:
        print(f"[ERROR] Analysis failed: {e}")

# ─── KILL ZONE MONITOR ─────────────────────────────────────────────────────────
def check_kill_zone_open():
    global prev_kz_active, alert_count
    now_kz = in_kill_zone()
    if now_kz and not prev_kz_active:
        kz = kill_zone_name()
        alert_count += 1
        print(f"[KZ] {kz} just opened!")
        send_telegram(
            f"⚡ <b>INSTANT ALERT — {kz} Opened!</b>\n\n"
            f"Running SMC/ICT analysis now...\n"
            f"🕐 {ist_now()}\n"
            f"🤖 XAUUSD Agent"
        )
        run_analysis("KZ_ALERT")
    prev_kz_active = now_kz

# ─── PRICE SPIKE MONITOR ───────────────────────────────────────────────────────
def fetch_price() -> float:
    try:
        resp  = requests.get("https://api.metals.live/v1/spot/gold", timeout=5)
        price = float(resp.json().get("price", 0))
        return round(price * 2, 2)   # convert to FOREX.com scale
    except Exception:
        return 0.0

def check_price_spike(current_price: float):
    global last_price, alert_count
    if last_price is not None and current_price > 0:
        move = abs(current_price - last_price)
        if move >= SPIKE_THRESHOLD_PTS:
            direction = "UP ⬆️" if current_price > last_price else "DOWN ⬇️"
            alert_count += 1
            print(f"[SPIKE] {move:.2f} pts {direction}")
            send_telegram(
                f"⚡ <b>PRICE SPIKE ALERT!</b>\n\n"
                f"XAUUSD moved <b>{move:.2f} pts {direction}</b> suddenly!\n"
                f"💰 Current price: {current_price}\n"
                f"🕐 {ist_now()}\n"
                f"Running instant analysis...\n"
                f"🤖 XAUUSD Agent"
            )
            run_analysis("SPIKE_ALERT")
    last_price = current_price

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("  XAUUSD SMC/ICT Bot — Powered by Gemini (FREE)")
    print("  24/5 Autonomous · Telegram Alerts")
    print("=" * 50)
    print(f"  Scan every    : {SCAN_INTERVAL_SECONDS}s (1 min)")
    print(f"  Min confidence: {CONFIDENCE_THRESHOLD}%")
    print(f"  Spike trigger : {SPIKE_THRESHOLD_PTS} pts")
    print(f"  Started at    : {ist_now()}")
    print("=" * 50)

    send_telegram(
        "✅ <b>XAUUSD Agent is LIVE!</b>\n\n"
        "⚡ Scanning every <b>1 minute</b>\n"
        "🔔 Instant alerts:\n"
        "   • Kill zone opens (London / NY)\n"
        "   • Sudden price spikes\n"
        f"🎯 Min confidence: {CONFIDENCE_THRESHOLD}%\n"
        "📊 FOREX.com scale (~4,712)\n"
        "🆓 Powered by Google Gemini (FREE)\n\n"
        f"🕐 Started: {ist_now()}\n"
        "🤖 I'll alert you the moment I find a setup!"
    )

    scan_counter = 0

    while True:
        try:
            if is_weekend():
                print(f"[SLEEP] Weekend — markets closed. Sleeping 1 hour.")
                time.sleep(3600)
                continue

            scan_counter += 1

            # 1. Kill zone check
            check_kill_zone_open()

            # 2. Price spike check
            price = fetch_price()
            if price > 0:
                check_price_spike(price)
                print(f"[TICK #{scan_counter}] {ist_now()} | Price: {price}")

            # 3. Scheduled scan
            run_analysis("SCHEDULED")

            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\n[STOP] Agent stopped.")
            send_telegram("⏸ <b>XAUUSD Agent paused.</b>")
            break
        except Exception as e:
            print(f"[ERROR] Main loop: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
