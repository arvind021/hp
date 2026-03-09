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
        df = yf.Ticker(YF_MAP[symbol]).history(period="2d", interval="5m")
        if df.empty: return None
        closes = list(df["Close"].dropna())
        print(f"  📊 {symbol}: {len(closes)} candles")
        return closes
    except Exception as e:
        print(f"  ❌ {symbol}: {e}")
        return None

def fetch_prices():
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
#  SIGNAL LOGIC — Voting System (3/4 wins)
#  Koi bhi 3 indicators agree karein = Signal!
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

    print(f"  RSI={rsi:.1f} MACD_line={ml:.6f} MACD_sig={ms:.6f}")

    # CALL votes
    call_votes = 0
    if rsi < 45:                          call_votes += 1  # RSI oversold
    if ml > ms:                           call_votes += 1  # MACD bullish
    if price > ema9 > ema21:              call_votes += 1  # EMA bullish
    if bl is not None and price < bl:     call_votes += 1  # BB lower

    # PUT votes
    put_votes = 0
    if rsi > 55:                          put_votes += 1   # RSI overbought
    if ml < ms:                           put_votes += 1   # MACD bearish
    if price < ema9 < ema21:              put_votes += 1   # EMA bearish
    if bu is not None and price > bu:     put_votes += 1   # BB upper

    print(f"  CALL votes: {call_votes}/4 | PUT votes: {put_votes}/4")

    # 4/4 = STRONG
    if call_votes == 4:
        return "CALL", 10, "STRONG 🔥"
    if put_votes == 4:
        return "PUT",  10, "STRONG 🔥"

    # 3/4 = GOOD
    if call_votes == 3 and call_votes > put_votes:
        return "CALL", 7, "GOOD ✅"
    if put_votes == 3 and put_votes > call_votes:
        return "PUT",  7, "GOOD ✅"

    return None, 0, ""

# ============================================================
#  SCAN LOOP
# ============================================================
def scan_loop():
    while True:
        try:
            ist = datetime.now(pytz.timezone("Asia/Kolkata"))
            stats["last_scan"] = ist.strftime("%I:%M %p")

            if not is_good_session():
                print(f"[{stats['last_scan']}] Outside session — skipped")
                time.sleep(120); continue

            print(f"\n[{stats['last_scan']}] Scanning {len(PAIRS)} pairs...")
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
                    print(f"✅ SIGNAL: {pair} {direction} {strength}")
                    break
                else:
                    print(f"⚪ {pair}: No signal")

            threading.Thread(target=fetch_prices, daemon=True).start()

        except Exception as e:
            print(f"Scan error: {e}")

        time.sleep(120)

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
    return jsonify({"status": "ok", "api": "yfinance"})

if __name__ == "__main__":
    print("🚀 Quotex Signals API — Voting System")
    print("📊 RSI + MACD + EMA + BB (3/4 = Signal)")
    print(f"📡 Pairs: {', '.join(PAIRS)}")
    print("⏱  Scan: Every 2 minutes")
    threading.Thread(target=scan_loop, daemon=True).start()
    print("🔗 http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001)
