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

# --- Indicators (same as bot) ---
def calc_ema(c, p):
    if len(c) < p: return c[-1]
    k = 2/(p+1); e = sum(c[:p])/p
    for x in c[p:]: e = x*k + e*(1-k)
    return e

def calc_rsi(c, p=14):
    if len(c) < p+1: return 50.0
    g=[]; l=[]
    for i in range(1,p+1):
        d=c[-i]-c[-(i+1)]
        (g if d>0 else l).append(abs(d))
    ag=sum(g)/p if g else 0; al=sum(l)/p if l else 1e-10
    return 100-(100/(1+ag/al))

def calc_macd(c):
    if len(c)<26: return 0,0
    ml=calc_ema(c,12)-calc_ema(c,26)
    return ml, ml*0.85

def calc_bb(c, p=20):
    if len(c)<p: return None,None,None
    r=c[-p:]; m=sum(r)/p
    std=(sum((x-m)**2 for x in r)/p)**0.5
    return m+2*std, m, m-2*std

def get_candles(symbol, interval="5min", size=60):
    clean=symbol.replace("/","")
    url=f"https://api.twelvedata.com/time_series?symbol={clean}&interval={interval}&outputsize={size}&apikey={TD_API_KEY}"
    try:
        r=requests.get(url,timeout=10); d=r.json()
        if d.get("status")!="ok": return None
        cv=d["values"]
        return {
            "close": [float(c["close"]) for c in reversed(cv)],
            "high":  [float(c["high"])  for c in reversed(cv)],
            "low":   [float(c["low"])   for c in reversed(cv)],
            "open":  [float(c["open"])  for c in reversed(cv)],
        }
    except: return None

def generate_signal(symbol):
    d=get_candles(symbol)
    if not d or len(d["close"])<30: return None,0
    c=d["close"]; h=d["high"]; l=d["low"]
    rsi=calc_rsi(c); ml,ms=calc_macd(c)
    e9=calc_ema(c,9); e21=calc_ema(c,21)
    bu,_,bl=calc_bb(c); price=c[-1]

    cs=ps=0
    if rsi<40: cs+=1
    if ml>ms: cs+=1
    if price>e9>e21: cs+=1
    if bl and price<bl: cs+=1

    if rsi>60: ps+=1
    if ml<ms: ps+=1
    if price<e9<e21: ps+=1
    if bu and price>bu: ps+=1

    if cs>=3 and cs>ps: return "CALL", cs*2+2
    if ps>=3 and ps>cs: return "PUT",  ps*2+2
    return None, 0

def fetch_prices():
    for pair in PAIRS:
        clean=pair.replace("/","")
        url=f"https://api.twelvedata.com/price?symbol={clean}&apikey={TD_API_KEY}"
        try:
            r=requests.get(url,timeout=5)
            live_prices[pair]=float(r.json().get("price",0))
        except: pass

def scan_loop():
    while True:
        try:
            fetch_prices()
            ist=datetime.now(pytz.timezone("Asia/Kolkata"))
            stats["last_scan"]=ist.strftime("%I:%M %p")
            for pair in PAIRS:
                direction, score = generate_signal(pair)
                if direction:
                    signal_history.appendleft({
                        "time": ist.strftime("%I:%M %p"),
                        "pair": pair,
                        "direction": direction,
                        "score": score,
                        "price": live_prices.get(pair, 0),
                    })
                    stats["total"] += 1
                    break
        except Exception as e:
            print(f"Scan error: {e}")
        time.sleep(300)

# --- API Routes ---
@app.route("/signals")
def get_signals():
    return jsonify({
        "signals": list(signal_history),
        "prices": live_prices,
        "stats": stats,
        "winrate": {k: v for k,v in winrate.items()},
    })

@app.route("/ping")
def ping():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    threading.Thread(target=scan_loop, daemon=True).start()
    print("🚀 Signals API running on http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001)
