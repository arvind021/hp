from flask import Flask, jsonify
from flask_cors import CORS
from collections import deque, defaultdict
from datetime import datetime
import pytz, requests, threading, time

app = Flask(__name__)
CORS(app)

TD_API_KEY = "6c0dd041654a47b692d3964cf86ecfec"
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "EUR/GBP", "USD/CHF"]

signal_history = deque(maxlen=20)
live_prices    = {p: 0.0 for p in PAIRS}
winrate        = defaultdict(lambda: {"win": 0, "loss": 0})
stats          = {"total": 0, "users": 0, "last_scan": "Never"}

# ============================================================
#  INDICATORS
# ============================================================
def calc_ema(closes, period):
    if len(closes) < period: return closes[-1]
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]: ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-(i+1)]
        (gains if diff > 0 else losses).append(abs(diff))
    ag = sum(gains)/period if gains else 0
    al = sum(losses)/period if losses else 1e-10
    return 100 - (100 / (1 + ag/al))

def calc_macd(closes):
    if len(closes) < 26: return 0, 0
    ml = calc_ema(closes, 12) - calc_ema(closes, 26)
    return ml, ml * 0.85

def calc_bollinger(closes, period=20):
    if len(closes) < period: return None, None, None
    r = closes[-period:]; m = sum(r)/period
    std = (sum((x-m)**2 for x in r)/period)**0.5
    return m + 2*std, m, m - 2*std

def get_candles(symbol, interval="5min", size=60):
    clean = symbol.replace("/", "")
    url = f"https://api.twelvedata.com/time_series?symbol={clean}&interval={interval}&outputsize={size}&apikey={TD_API_KEY}"
    try:
        r = requests.get(url, timeout=10); d = r.json()
        if d.get("status") != "ok": return None
        return [float(c["close"]) for c in reversed(d["values"])]
    except: return None

def fetch_prices():
    for pair in PAIRS:
        clean = pair.replace("/", "")
        url = f"https://api.twelvedata.com/price?symbol={clean}&apikey={TD_API_KEY}"
        try:
            r = requests.get(url, timeout=5)
            live_prices[pair] = float(r.json().get("price", 0))
        except: pass

def is_good_session():
    hour = datetime.now(pytz.timezone("UTC")).hour
    return (7 <= hour < 16) or (12 <= hour < 21)

# ============================================================
#  BEST SIGNAL LOGIC
#
#  STRONG signal (score 10) — Sabse reliable:
#    RSI + MACD + EMA + BB — sab 4 agree
#
#  NORMAL signal (score 7) — Acha signal:
#    RSI + MACD + EMA — 3 agree
#
#  WEAK signal (score 5) — Skip karo:
#    Sirf 2 agree — signal nahi dega
# ============================================================
def generate_signal(symbol):
    closes = get_candles(symbol)
    if not closes or len(closes) < 30: return None, 0, ""

    price  = closes[-1]
    rsi    = calc_rsi(closes)
    ml, ms = calc_macd(closes)
    ema9   = calc_ema(closes, 9)
    ema21  = calc_ema(closes, 21)
    bu, _, bl = calc_bollinger(closes)

    # --- Individual checks ---
    # CALL
    c_rsi  = rsi < 50
    c_macd = ml > ms
    c_ema  = price > ema9 > ema21
    c_bb   = bl is not None and price < bl

    # PUT
    p_rsi  = rsi > 50
    p_macd = ml < ms
    p_ema  = price < ema9 < ema21
    p_bb   = bu is not None and price > bu

    call_count = sum([c_rsi, c_macd, c_ema, c_bb])
    put_count  = sum([p_rsi, p_macd, p_ema, p_bb])

    # STRONG — sab 4 agree
    if call_count == 4:
        return "CALL", 10, "STRONG 🔥"
    if put_count == 4:
        return "PUT", 10, "STRONG 🔥"

    # NORMAL — RSI + MACD + EMA (3 core)
    if c_rsi and c_macd and c_ema:
        return "CALL", 7, "GOOD ✅"
    if p_rsi and p_macd and p_ema:
        return "PUT", 7, "GOOD ✅"

    # NORMAL — RSI + MACD + BB
    if c_rsi and c_macd and c_bb:
        return "CALL", 7, "GOOD ✅"
    if p_rsi and p_macd and p_bb:
        return "PUT", 7, "GOOD ✅"

    return None, 0, ""

# ============================================================
#  SCAN LOOP — Har 1 min
# ============================================================
def scan_loop():
    while True:
        try:
            fetch_prices()
            ist = datetime.now(pytz.timezone("Asia/Kolkata"))
            stats["last_scan"] = ist.strftime("%I:%M %p")

            if not is_good_session():
                print(f"[{stats['last_scan']}] Outside session — skipped")
                time.sleep(60); continue

            print(f"[{stats['last_scan']}] Scanning {len(PAIRS)} pairs...")
            for pair in PAIRS:
                direction, score, strength = generate_signal(pair)
                if direction:
                    signal_history.appendleft({
                        "time":      ist.strftime("%I:%M %p"),
                        "pair":      pair,
                        "direction": direction,
                        "score":     score,
                        "strength":  strength,
                        "price":     live_prices.get(pair, 0),
                    })
                    stats["total"] += 1
                    print(f"✅ {pair} {direction} {strength}")
                    break
                else:
                    print(f"⚪ {pair}: No signal")

        except Exception as e:
            print(f"Scan error: {e}")

        time.sleep(60)

# ============================================================
#  API ROUTES
# ============================================================
@app.route("/signals")
def get_signals():
    return jsonify({
        "signals": list(signal_history),
        "prices":  live_prices,
        "stats":   stats,
        "winrate": {k: v for k, v in winrate.items()},
    })

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "scan": "1min"})

if __name__ == "__main__":
    print("🚀 Quotex Signals API — Best System")
    print("🔥 STRONG: RSI+MACD+EMA+BB (score 10)")
    print("✅ GOOD:   RSI+MACD+EMA or BB (score 7)")
    print(f"📡 Pairs: {', '.join(PAIRS)}")
    print("⏱  Scan: Every 1 minute")
    threading.Thread(target=scan_loop, daemon=True).start()
    print("🔗 http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001)
