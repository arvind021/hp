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

def calc_stoch_rsi(closes, period=14, smooth=3):
    """Stochastic RSI — RSI se zyada sensitive"""
    if len(closes) < period * 2: return 50.0, 50.0
    rsi_values = []
    for i in range(period, len(closes)):
        rsi_values.append(calc_rsi(closes[:i+1], period))
    if len(rsi_values) < period: return 50.0, 50.0
    recent = rsi_values[-period:]
    min_rsi = min(recent); max_rsi = max(recent)
    if max_rsi == min_rsi: return 50.0, 50.0
    k = ((rsi_values[-1] - min_rsi) / (max_rsi - min_rsi)) * 100
    d = sum([(rsi_values[-i] - min_rsi)/(max_rsi - min_rsi)*100
             for i in range(1, smooth+1)]) / smooth
    return k, d

def calc_macd(closes):
    if len(closes) < 35: return 0, 0
    macd_values = []
    for i in range(26, len(closes)):
        e12 = calc_ema(closes[:i+1], 12)
        e26 = calc_ema(closes[:i+1], 26)
        macd_values.append(e12 - e26)
    if len(macd_values) < 9: return 0, 0
    return macd_values[-1], calc_ema(macd_values, 9)

def calc_bollinger(closes, period=10):
    """2min ke liye BB period=10"""
    if len(closes) < period: return None, None, None
    r = closes[-period:]; m = sum(r)/period
    std = (sum((x-m)**2 for x in r)/period)**0.5
    return m + 2*std, m, m - 2*std

def calc_cci(closes, period=14):
    """CCI — Commodity Channel Index"""
    if len(closes) < period: return 0
    tp = closes[-period:]  # typical price (using close only)
    ma = sum(tp)/period
    md = sum(abs(x - ma) for x in tp)/period
    if md == 0: return 0
    return (closes[-1] - ma) / (0.015 * md)

def calc_candle_pattern(closes):
    """Bullish/Bearish candle pattern detect"""
    if len(closes) < 3: return 0
    c1, c2, c3 = closes[-3], closes[-2], closes[-1]
    # Bullish: 3 consecutive up candles
    if c3 > c2 > c1: return 1
    # Bearish: 3 consecutive down candles
    if c3 < c2 < c1: return -1
    # Bullish engulfing: big up after down
    if c3 > c1 and c2 < c1: return 1
    # Bearish engulfing: big down after up
    if c3 < c1 and c2 > c1: return -1
    return 0

def get_candles(symbol):
    try:
        import yfinance as yf
        df = yf.Ticker(YF_MAP[symbol]).history(period="1d", interval="2m")
        if df.empty: return None
        closes = list(df["Close"].dropna())
        print(f"  📊 {symbol}: {len(closes)} candles")
        return closes
    except Exception as e:
        print(f"  ❌ {symbol}: {e}")
        return None

def get_current_price(symbol):
    try:
        import yfinance as yf
        df = yf.Ticker(YF_MAP[symbol]).history(period="1d", interval="1m")
        if not df.empty: return float(df["Close"].iloc[-1])
    except: pass
    return None

def fetch_all_prices():
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
#  POWERFUL SIGNAL LOGIC — 6 Indicators
#  STRONG: 5-6/6 agree
#  GOOD:   4/6 agree
#  Min: 4/6 required
# ============================================================
def generate_signal(symbol):
    closes = get_candles(symbol)
    if not closes or len(closes) < 35: return None, 0, ""

    price      = closes[-1]
    stoch_k, stoch_d = calc_stoch_rsi(closes)
    ml, ms     = calc_macd(closes)
    ema5       = calc_ema(closes, 5)
    ema13      = calc_ema(closes, 13)
    bu, bm, bl = calc_bollinger(closes, 10)
    cci        = calc_cci(closes)
    candle     = calc_candle_pattern(closes)

    print(f"  {symbol}: StochRSI={stoch_k:.1f}/{stoch_d:.1f} MACD={ml:.6f}/{ms:.6f} EMA={ema5:.5f}/{ema13:.5f} CCI={cci:.1f} Candle={candle}")

    # ---- CALL votes ----
    votes_call = 0
    # 1. Stochastic RSI — oversold + K crossing D upward
    if stoch_k < 30 and stoch_k > stoch_d:       votes_call += 1
    # 2. MACD — bullish crossover
    if ml > ms and ml < 0:                         votes_call += 1  # Fresh crossover
    elif ml > ms:                                  votes_call += 0.5
    # 3. EMA 5/13 — fast cross bullish
    if price > ema5 > ema13:                       votes_call += 1
    # 4. Bollinger — price bouncing from lower band
    if bl is not None and price < bl * 1.001:      votes_call += 1
    # 5. CCI — oversold
    if cci < -80:                                  votes_call += 1
    elif cci < 0:                                  votes_call += 0.5
    # 6. Candle pattern — bullish
    if candle == 1:                                votes_call += 1

    # ---- PUT votes ----
    votes_put = 0
    # 1. Stochastic RSI — overbought + K crossing D downward
    if stoch_k > 70 and stoch_k < stoch_d:        votes_put += 1
    # 2. MACD — bearish crossover
    if ml < ms and ml > 0:                         votes_put += 1  # Fresh crossover
    elif ml < ms:                                  votes_put += 0.5
    # 3. EMA 5/13 — fast cross bearish
    if price < ema5 < ema13:                       votes_put += 1
    # 4. Bollinger — price hitting upper band
    if bu is not None and price > bu * 0.999:      votes_put += 1
    # 5. CCI — overbought
    if cci > 80:                                   votes_put += 1
    elif cci > 0:                                  votes_put += 0.5
    # 6. Candle pattern — bearish
    if candle == -1:                               votes_put += 1

    print(f"  CALL votes={votes_call:.1f} | PUT votes={votes_put:.1f}")

    max_votes = 6

    # STRONG — 5+ votes
    if votes_call >= 5 and votes_call > votes_put:
        score = min(10, int(votes_call/max_votes*10)+2)
        return "CALL", score, "STRONG 🔥"
    if votes_put >= 5 and votes_put > votes_call:
        score = min(10, int(votes_put/max_votes*10)+2)
        return "PUT", score, "STRONG 🔥"

    # GOOD — 4+ votes
    if votes_call >= 4 and votes_call > votes_put:
        score = int(votes_call/max_votes*10)+1
        return "CALL", score, "GOOD ✅"
    if votes_put >= 4 and votes_put > votes_call:
        score = int(votes_put/max_votes*10)+1
        return "PUT", score, "GOOD ✅"

    return None, 0, ""

# ============================================================
#  WIN/LOSS CHECKER — 2 min baad
# ============================================================
def check_results():
    while True:
        time.sleep(30)
        now = time.time()
        still_pending = []
        for entry in pending_results:
            if now - entry["timestamp"] >= 120:
                current = get_current_price(entry["pair"])
                if current is None:
                    still_pending.append(entry); continue
                won = (entry["direction"] == "CALL" and current > entry["price"]) or \
                      (entry["direction"] == "PUT"  and current < entry["price"])
                result = "WIN 🏆" if won else "LOSS ❌"
                print(f"📊 Result: {entry['pair']} {entry['direction']} → {result}")
                if won:
                    winrate[entry["pair"]]["win"]  += 1; stats["wins"]   += 1
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
#  SCAN LOOP — Har 2 min
# ============================================================
def scan_loop():
    while True:
        try:
            fetch_all_prices()
            ist = datetime.now(pytz.timezone("Asia/Kolkata"))
            stats["last_scan"] = ist.strftime("%I:%M %p")

            if not is_good_session():
                print(f"[{stats['last_scan']}] Outside session — skipped")
                time.sleep(120); continue

            print(f"\n[{stats['last_scan']}] Scanning all {len(PAIRS)} pairs...")
            found_any = False

            for pair in PAIRS:
                direction, score, strength = generate_signal(pair)
                if direction:
                    price = live_prices.get(pair, 0) or (get_current_price(pair) or 0)
                    sig = {
                        "time":      ist.strftime("%I:%M %p"),
                        "pair":      pair,
                        "direction": direction,
                        "score":     score,
                        "strength":  strength,
                        "price":     price,
                        "result":    "⏳ Pending",
                    }
                    signal_history.appendleft(sig)
                    stats["total"] += 1
                    pending_results.append({
                        "pair": pair, "direction": direction,
                        "price": price, "time": ist.strftime("%I:%M %p"),
                        "timestamp": time.time(),
                    })
                    print(f"✅ SIGNAL: {pair} {direction} {strength} (score={score})")
                    found_any = True
                else:
                    print(f"⚪ {pair}: No signal")

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
    print("🚀 Quotex Signals — POWERFUL 6 Indicator Strategy!")
    print("📊 StochRSI + EMA5/13 + MACD + BB + CCI + Candle")
    print("🎯 Min 4/6 votes required | 2 MIN expiry")
    print(f"📡 Pairs: {', '.join(PAIRS)}")
    print("⏱  Scan: Every 2 minutes")
    threading.Thread(target=scan_loop,     daemon=True).start()
    threading.Thread(target=check_results, daemon=True).start()
    print("🔗 http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001)
        
