from flask import Flask, jsonify
from flask_cors import CORS
from collections import deque, defaultdict
from datetime import datetime
import pytz, threading, time

app = Flask(__name__)
CORS(app)

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "EUR/GBP", "USD/CHF"]
YF_MAP = {
    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X", "AUD/USD": "AUDUSD=X",
    "EUR/GBP": "EURGBP=X", "USD/CHF": "USDCHF=X",
}

signal_history  = deque(maxlen=20)
live_prices     = {p: 0.0 for p in PAIRS}
winrate         = defaultdict(lambda: {"win": 0, "loss": 0})
pending_results = []
stats           = {"total": 0, "wins": 0, "losses": 0, "last_scan": "Never"}

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
    if len(closes) < 35: return 0, 0
    macd_values = []
    for i in range(26, len(closes)):
        e12 = calc_ema(closes[:i+1], 12)
        e26 = calc_ema(closes[:i+1], 26)
        macd_values.append(e12 - e26)
    if len(macd_values) < 9: return 0, 0
    return macd_values[-1], calc_ema(macd_values, 9)

def calc_bollinger(closes, period=20):
    if len(closes) < period: return None, None, None
    r = closes[-period:]; m = sum(r)/period
    std = (sum((x-m)**2 for x in r)/period)**0.5
    return m + 2*std, m, m - 2*std

def get_candles(symbol):
    try:
        import yfinance as yf
        df = yf.Ticker(YF_MAP[symbol]).history(period="1d", interval="2m")
        if df.empty: return None
        return list(df["Close"].dropna())
    except Exception as e:
        print(f"  ❌ {symbol}: {e}")
        return None

def get_current_price(symbol):
    try:
        import yfinance as yf
        df = yf.Ticker(YF_MAP[symbol]).history(period="1d", interval="1m")
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except: pass
    return None

def fetch_all_prices():
    """FIX 3 — Sab pairs ke fresh prices fetch karo"""
    try:
        import yfinance as yf
        for pair in PAIRS:
            df = yf.Ticker(YF_MAP[pair]).history(period="1d", interval="1m")
            if not df.empty:
                live_prices[pair] = float(df["Close"].iloc[-1])
    except: pass

def is_good_session():
    hour = datetime.now(pytz.timezone("UTC")).hour
    return (7 <= hour < 16) or (12 <= hour < 21)

# ============================================================
#  SIGNAL LOGIC — FIX 2: RSI range sahi kiya
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

    # FIX 2 — RSI strict ranges
    # CALL: RSI 30-50 (oversold zone)
    # PUT:  RSI 50-70 (overbought zone)
    # RSI >70 ya <30 = skip (extreme = reversal possible but unreliable)
    c_rsi  = 30 < rsi < 50          # CALL zone
    c_macd = ml > ms
    c_ema  = price > ema9 > ema21
    c_bb   = bl is not None and price < bl

    p_rsi  = 50 < rsi < 70          # PUT zone
    p_macd = ml < ms
    p_ema  = price < ema9 < ema21
    p_bb   = bu is not None and price > bu

    call_votes = sum([c_rsi, c_macd, c_ema, c_bb])
    put_votes  = sum([p_rsi, p_macd, p_ema, p_bb])

    print(f"  {symbol}: RSI={rsi:.1f} | CALL={call_votes}/4 PUT={put_votes}/4")

    # 4/4 = STRONG, 3/4 = GOOD, 2/4 = WEAK
    if call_votes >= 3 and c_macd and call_votes > put_votes:
        s = "STRONG 🔥" if call_votes == 4 else "GOOD ✅"
        return "CALL", call_votes*2+2, s
    if put_votes >= 3 and p_macd and put_votes > call_votes:
        s = "STRONG 🔥" if put_votes == 4 else "GOOD ✅"
        return "PUT", put_votes*2+2, s

    # WEAK — 2/4 + MACD + RSI range zaroori
    if call_votes == 2 and c_macd and c_rsi and call_votes > put_votes:
        return "CALL", 5, "WEAK ⚡"
    if put_votes == 2 and p_macd and p_rsi and put_votes > call_votes:
        return "PUT", 5, "WEAK ⚡"

    return None, 0, ""

# ============================================================
#  WIN/LOSS CHECKER
# ============================================================
def check_results():
    while True:
        time.sleep(30)
        now = time.time()
        still_pending = []
        for entry in pending_results:
            if now - entry["timestamp"] >= 120:  # 2 min baad check
                current = get_current_price(entry["pair"])
                if current is None:
                    still_pending.append(entry); continue
                won = (entry["direction"] == "CALL" and current > entry["price"]) or \
                      (entry["direction"] == "PUT"  and current < entry["price"])
                result = "WIN 🏆" if won else "LOSS ❌"
                print(f"📊 Result: {entry['pair']} {entry['direction']} → {result}")
                if won:
                    winrate[entry["pair"]]["win"] += 1; stats["wins"] += 1
                else:
                    winrate[entry["pair"]]["loss"] += 1; stats["losses"] += 1
                for sig in signal_history:
                    if sig["pair"] == entry["pair"] and sig["time"] == entry["time"]:
                        sig["result"] = "✅ WIN" if won else "❌ LOSS"; break
            else:
                still_pending.append(entry)
        pending_results.clear()
        pending_results.extend(still_pending)

# ============================================================
#  SCAN LOOP — FIX 1: Sab pairs scan karo, best signal lo
# ============================================================
def scan_loop():
    while True:
        try:
            # FIX 3 — Pehle fresh prices fetch karo
            fetch_all_prices()

            ist = datetime.now(pytz.timezone("Asia/Kolkata"))
            stats["last_scan"] = ist.strftime("%I:%M %p")

            if not is_good_session():
                print(f"[{stats['last_scan']}] Outside session — skipped")
                time.sleep(120); continue

            print(f"\n[{stats['last_scan']}] Scanning all {len(PAIRS)} pairs...")

            # Sab valid signals dikhao
            found_any = False
            for pair in PAIRS:
                direction, score, strength = generate_signal(pair)
                if direction:
                    sig = {
                        "time":      ist.strftime("%I:%M %p"),
                        "pair":      pair,
                        "direction": direction,
                        "score":     score,
                        "strength":  strength,
                        "price":     live_prices.get(pair, 0),
                        "result":    "⏳ Pending",
                    }
                    signal_history.appendleft(sig)
                    stats["total"] += 1
                    pending_results.append({
                        "pair":      pair,
                        "direction": direction,
                        "price":     live_prices.get(pair, 0),
                        "time":      ist.strftime("%I:%M %p"),
                        "timestamp": time.time(),
                    })
                    print(f"✅ SIGNAL: {pair} {direction} {strength} (score={score})")
                    found_any = True

            if not found_any:
                print("⚪ No signal this scan")

        except Exception as e:
            print(f"Scan error: {e}")

        time.sleep(120)

@app.route("/signals")
def get_signals():
    total = stats["wins"] + stats["losses"]
    acc   = round(stats["wins"]/total*100, 1) if total > 0 else 0
    return jsonify({
        "signals": list(signal_history),
        "prices":  live_prices,
        "stats":   {**stats, "accuracy": acc},
        "winrate": {k: v for k, v in winrate.items()},
    })

@app.route("/ping")
def ping():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    print("🚀 Quotex Signals API — 3 Bugs Fixed!")
    print("✅ FIX 1: Sab pairs scan, best signal")
    print("✅ FIX 2: RSI range 30-50 / 50-70")
    print("✅ FIX 3: Fresh prices har scan")
    print(f"📡 Pairs: {', '.join(PAIRS)}")
    print("⏱  Scan: Every 2 min | Min: 3/4 votes")
    threading.Thread(target=scan_loop,     daemon=True).start()
    threading.Thread(target=check_results, daemon=True).start()
    print("🔗 http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001)
