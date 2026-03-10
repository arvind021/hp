from flask import Flask, jsonify
from flask_cors import CORS
from collections import deque
from datetime import datetime
import pytz, threading, time, os, pickle, json
import numpy as np
import requests

app = Flask(__name__)
CORS(app)

# ============================================================
#  CONFIG
# ============================================================
BOT_TOKEN  = "8535736097:AAF_h-PGiYEOtPLZ7rYIsCXMh6R5tiTtbmI"
SIGNAL_ID  = 8314837762    # Private — Signals yahan aayenge
LOG_ID     = -5269071865   # Channel — Logs + Files yahan

MODEL_DIR  = os.path.expanduser("~/hp/models")
DATA_FILE  = os.path.expanduser("~/hp/trade_data.json")
os.makedirs(MODEL_DIR, exist_ok=True)

PAIRS  = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "EUR/GBP", "USD/CHF"]
YF_MAP = {
    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X", "AUD/USD": "AUDUSD=X",
    "EUR/GBP": "EURGBP=X", "USD/CHF": "USDCHF=X",
}

pending_results = []
live_prices     = {p: 0.0 for p in PAIRS}
models          = {}

# ============================================================
#  PERSISTENT DATA
# ============================================================
def load_data():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                d = json.load(f)
            print(f"📂 Data loaded: W={d['stats']['wins']} L={d['stats']['losses']}")
            return d["stats"], d.get("history", [])
    except Exception as e:
        print(f"❌ Load error: {e}")
    return {"total": 0, "wins": 0, "losses": 0, "last_scan": "Never"}, []

def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump({"stats": stats, "history": list(signal_history)}, f, indent=2)
    except Exception as e:
        print(f"❌ Save error: {e}")

_saved_stats, _saved_history = load_data()
stats          = _saved_stats
signal_history = _saved_history

# ============================================================
#  TELEGRAM
# ============================================================
def tg_send(chat_id, msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": msg},
            timeout=10
        )
    except:
        pass

def tg_log(msg):
    tg_send(LOG_ID, msg)

def tg_signal(msg):
    tg_send(SIGNAL_ID, msg)

def tg_send_file(filepath, caption=""):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        with open(filepath, "rb") as f:
            r = requests.post(url,
                data={"chat_id": LOG_ID, "caption": caption},
                files={"document": f},
                timeout=30
            )
        resp = r.json()
        if r.status_code == 200 and resp.get("ok"):
            print(f"  📤 Uploaded: {os.path.basename(filepath)}")
            return resp["result"]["document"]["file_id"]
        else:
            print(f"  ❌ Upload failed: {resp.get('description', 'Unknown error')}")
            # Text message bhejo instead
            tg_log(f"⚠️ File upload failed: {os.path.basename(filepath)}\n{caption}")
    except Exception as e:
        print(f"  ❌ Upload error: {e}")
        tg_log(f"⚠️ Upload error: {e}")
    return None

def tg_download_model(pair):
    try:
        filename = pair.replace("/", "") + "_model.pkl"
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"limit": 100}, timeout=15
        )
        if r.status_code != 200:
            return False
        messages = r.json().get("result", [])
        file_id = None
        for msg in reversed(messages):
            doc = msg.get("channel_post", {}).get("document", {})
            if doc.get("file_name") == filename:
                file_id = doc["file_id"]
                break
        if not file_id:
            return False
        r2 = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=15
        )
        fp = r2.json()["result"]["file_path"]
        r3 = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}",
            timeout=30
        )
        save_path = os.path.join(MODEL_DIR, filename)
        with open(save_path, "wb") as f:
            f.write(r3.content)
        print(f"  📥 Downloaded: {filename}")
        return True
    except Exception as e:
        print(f"  ❌ TG download error: {e}")
        return False

def backup_to_telegram():
    if os.path.exists(DATA_FILE):
        tg_send_file(DATA_FILE,
            f"💾 Backup | W:{stats['wins']} L:{stats['losses']}")

# ============================================================
#  INDICATORS
# ============================================================
def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-(i+1)]
        (gains if diff > 0 else losses).append(abs(diff))
    ag = sum(gains)/period if gains else 0
    al = sum(losses)/period if losses else 1e-10
    return 100 - (100 / (1 + ag/al))

def calc_stoch_rsi(closes, period=14, smooth=3):
    if len(closes) < period * 2:
        return 50.0, 50.0
    rsi_vals = [calc_rsi(closes[:i+1], period) for i in range(period, len(closes))]
    if len(rsi_vals) < period:
        return 50.0, 50.0
    recent = rsi_vals[-period:]
    mn, mx = min(recent), max(recent)
    if mx == mn:
        return 50.0, 50.0
    k = ((rsi_vals[-1] - mn) / (mx - mn)) * 100
    d = sum([(rsi_vals[-i] - mn)/(mx - mn)*100 for i in range(1, smooth+1)]) / smooth
    return k, d

def calc_macd(closes):
    if len(closes) < 35:
        return 0, 0
    macd_vals = []
    for i in range(26, len(closes)):
        macd_vals.append(calc_ema(closes[:i+1], 12) - calc_ema(closes[:i+1], 26))
    if len(macd_vals) < 9:
        return 0, 0
    return macd_vals[-1], calc_ema(macd_vals, 9)

def calc_bollinger(closes, period=10):
    if len(closes) < period:
        return None, None, None
    r = closes[-period:]
    m = sum(r) / period
    std = (sum((x-m)**2 for x in r) / period) ** 0.5
    return m + 2*std, m, m - 2*std

def calc_cci(closes, period=14):
    if len(closes) < period:
        return 0
    tp = closes[-period:]
    ma = sum(tp) / period
    md = sum(abs(x - ma) for x in tp) / period
    return 0 if md == 0 else (closes[-1] - ma) / (0.015 * md)

def calc_atr(closes, period=14):
    if len(closes) < period + 1:
        return 0
    return sum(abs(closes[-i] - closes[-(i+1)]) for i in range(1, period+1)) / period

def calc_momentum(closes, period=10):
    if len(closes) < period + 1:
        return 0
    return closes[-1] - closes[-(period+1)]

def calc_williams_r(closes, period=14):
    if len(closes) < period:
        return -50
    high = max(closes[-period:])
    low  = min(closes[-period:])
    if high == low:
        return -50
    return ((high - closes[-1]) / (high - low)) * -100

def extract_features(closes):
    if len(closes) < 50:
        return None
    try:
        price   = closes[-1]
        rsi     = calc_rsi(closes)
        sk, sd  = calc_stoch_rsi(closes)
        ml, ms  = calc_macd(closes)
        bu, bm, bl = calc_bollinger(closes)
        cci     = calc_cci(closes)
        atr     = calc_atr(closes)
        mom     = calc_momentum(closes)
        wpr     = calc_williams_r(closes)
        e5      = calc_ema(closes, 5)
        e13     = calc_ema(closes, 13)
        bb_pos  = (price - bl) / (bu - bl) * 100 if bu and bu != bl else 50
        return [
            rsi, sk, sd,
            ml * 10000, ms * 10000,
            (price - e5)  / price * 100,
            (price - e13) / price * 100,
            bb_pos, cci / 100,
            atr * 10000, mom * 10000, wpr,
            1 if closes[-1] > closes[-2] else -1,
            (closes[-1] - closes[-3]) / closes[-3] * 100,
            (closes[-1] - closes[-5]) / closes[-5] * 100,
        ]
    except:
        return None

# ============================================================
#  MODEL SAVE / LOAD
# ============================================================
def model_filename(pair):
    return os.path.join(MODEL_DIR, pair.replace("/", "") + "_model.pkl")

def save_model_local(pair, model, scaler, acc):
    try:
        path = model_filename(pair)
        with open(path, "wb") as f:
            pickle.dump({"model": model, "scaler": scaler, "acc": acc}, f)
        print(f"  💾 Saved: {pair}")
        return path
    except Exception as e:
        print(f"  ❌ Save error: {e}")
        return None

def load_model_local(pair):
    try:
        path = model_filename(pair)
        if os.path.exists(path):
            with open(path, "rb") as f:
                d = pickle.load(f)
            print(f"  📂 Loaded: {pair} acc={d['acc']:.1f}%")
            return d["model"], d["scaler"], d["acc"]
    except Exception as e:
        print(f"  ❌ Load error: {e}")
    return None, None, 0

# ============================================================
#  TRAIN MODEL
# ============================================================
def train_model(pair, closes):
    try:
        from xgboost import XGBClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.preprocessing import StandardScaler

        print(f"  🤖 Training {pair}...")
        X, y = [], []
        for i in range(60, len(closes) - 2):
            feat = extract_features(closes[i-60:i])
            if feat is None:
                continue
            y.append(1 if closes[i+2] > closes[i] else 0)
            X.append(feat)

        if len(X) < 100:
            print(f"  ❌ Not enough data: {len(X)}")
            return None, None, 0

        X, y = np.array(X), np.array(y)
        Xtr, Xte, ytr, yte = train_test_split(
            X, y, test_size=0.2, random_state=42, shuffle=False
        )
        sc = StandardScaler()
        Xtr = sc.fit_transform(Xtr)
        Xte = sc.transform(Xte)

        model = XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            eval_metric="logloss", random_state=42, verbosity=0
        )
        model.fit(Xtr, ytr)
        acc = model.score(Xte, yte) * 100
        print(f"  ✅ {pair} acc={acc:.1f}%")
        return model, sc, acc

    except ImportError:
        print("  ❌ pip install xgboost scikit-learn")
        return None, None, 0
    except Exception as e:
        print(f"  ❌ Train error {pair}: {e}")
        return None, None, 0

# ============================================================
#  DATA FETCH
# ============================================================
def _get_close_series(df, sym):
    """yfinance new version MultiIndex fix"""
    import pandas as pd
    if isinstance(df.columns, pd.MultiIndex):
        return df[("Close", sym)].dropna()
    return df["Close"].dropna()

def get_candles(symbol):
    import yfinance as yf
    sym = YF_MAP[symbol]
    for attempt in range(3):
        try:
            df = yf.download(sym, period="5d", interval="2m",
                             progress=False, timeout=20)
            if df is not None and len(df) >= 35:
                closes = [float(x) for x in _get_close_series(df, sym)]
                if len(closes) >= 35:
                    print(f"  ✅ {symbol}: {len(closes)} candles")
                    return closes
        except Exception as e:
            print(f"  ⚠️ {symbol} attempt {attempt+1}: {e}")
            time.sleep(1)
    print(f"  ❌ {symbol}: Failed")
    return None

def get_current_price(symbol):
    import yfinance as yf
    sym = YF_MAP[symbol]
    for attempt in range(3):
        try:
            df = yf.download(sym, period="1d", interval="2m",
                            progress=False, timeout=10)
            if df is not None and len(df) > 0:
                return float(_get_close_series(df, sym).iloc[-1])
        except Exception as e:
            print(f"  ⚠️ Price {symbol}: {e}")
            time.sleep(1)
    return None

def fetch_all_prices():
    import yfinance as yf
    for pair in PAIRS:
        try:
            sym = YF_MAP[pair]
            df = yf.download(sym, period="1d", interval="2m",
                            progress=False, timeout=10)
            if df is not None and len(df) > 0:
                live_prices[pair] = float(_get_close_series(df, sym).iloc[-1])
        except: pass

def is_good_session():
    hour = datetime.now(pytz.timezone("UTC")).hour
    return (7 <= hour < 16) or (12 <= hour < 21)

# ============================================================
#  SIGNAL GENERATION
# ============================================================
def generate_signal_ml(symbol):
    closes = get_candles(symbol)
    if not closes or len(closes) < 60:
        return None, 0, ""

    if symbol not in models or models[symbol][0] is None:
        model, scaler, acc = train_model(symbol, closes)
        if model:
            models[symbol] = (model, scaler, acc)
            path = save_model_local(symbol, model, scaler, acc)
            if path:
                threading.Thread(
                    target=tg_send_file,
                    args=(path, f"🤖 {symbol} | Acc: {acc:.1f}%"),
                    daemon=True
                ).start()
        else:
            models[symbol] = (None, None, 0)

    model, scaler, acc = models.get(symbol, (None, None, 0))
    if model is None:
        return None, 0, ""

    feat = extract_features(closes)
    if feat is None:
        return None, 0, ""

    try:
        prob = model.predict_proba(scaler.transform([feat]))[0]
        cp, pp = prob[1] * 100, prob[0] * 100
        print(f"  {symbol}: CALL={cp:.1f}% PUT={pp:.1f}% acc={acc:.1f}%")

        if cp >= 65 and cp > pp:
            s = "STRONG 🔥" if cp >= 75 else "GOOD ✅"
            return "CALL", min(10, int(cp/10)), f"{s} ({cp:.0f}%)"
        if pp >= 65 and pp > cp:
            s = "STRONG 🔥" if pp >= 75 else "GOOD ✅"
            return "PUT", min(10, int(pp/10)), f"{s} ({pp:.0f}%)"
    except Exception as e:
        print(f"  ❌ Predict error: {e}")

    return None, 0, ""

# ============================================================
#  WIN/LOSS CHECKER
# ============================================================
def check_results():
    while True:
        time.sleep(30)
        now = time.time()
        still = []
        for e in pending_results:
            if now - e["timestamp"] >= 120:
                cur = get_current_price(e["pair"])
                if cur is None:
                    still.append(e)
                    continue
                won = (e["direction"] == "CALL" and cur > e["price"]) or \
                      (e["direction"] == "PUT"  and cur < e["price"])
                print(f"📊 {e['pair']} {e['direction']} -> {'WIN' if won else 'LOSS'}")
                if won:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                for sig in signal_history:
                    if sig["pair"] == e["pair"] and sig["time"] == e["time"]:
                        sig["result"] = "✅ WIN" if won else "❌ LOSS"
                        break
                save_data()
                total = stats["wins"] + stats["losses"]
                acc   = round(stats["wins"] / total * 100, 1) if total > 0 else 0
                tg_log(
                    f"{'✅ WIN' if won else '❌ LOSS'} | {e['pair']} {e['direction']}\n"
                    f"W:{stats['wins']} L:{stats['losses']} Acc:{acc}%"
                )
            else:
                still.append(e)
        pending_results.clear()
        pending_results.extend(still)

# ============================================================
#  RETRAIN EVERY 2 HOURS
# ============================================================
def retrain_loop():
    while True:
        time.sleep(7200)
        print("\n🔄 Retraining...")
        tg_log("🔄 Retraining models...")
        for pair in PAIRS:
            closes = get_candles(pair)
            if closes and len(closes) >= 100:
                model, scaler, acc = train_model(pair, closes)
                if model:
                    models[pair] = (model, scaler, acc)
                    path = save_model_local(pair, model, scaler, acc)
                    if path:
                        tg_send_file(path, f"🔄 {pair} retrained | Acc:{acc:.1f}%")
        print("✅ Retrain done!")
        tg_log("✅ Retrain complete!")
        backup_to_telegram()

# ============================================================
#  INITIAL SETUP
# ============================================================
def initial_setup():
    print("\n📦 Installing dependencies...")
    os.system("pip install xgboost scikit-learn flask flask-cors yfinance pytz requests --quiet --break-system-packages")
    time.sleep(2)

    tg_log("🚀 QX Signals starting...\nChecking saved models...")

    for pair in PAIRS:
        print(f"\n📊 {pair}:")
        # 1. Disk
        model, scaler, acc = load_model_local(pair)
        if model:
            models[pair] = (model, scaler, acc)
            continue
        # 2. Telegram
        print(f"  📥 Trying Telegram...")
        if tg_download_model(pair):
            model, scaler, acc = load_model_local(pair)
            if model:
                models[pair] = (model, scaler, acc)
                continue
        # 3. Fresh train
        print(f"  🤖 Training...")
        closes = get_candles(pair)
        if closes and len(closes) >= 100:
            model, scaler, acc = train_model(pair, closes)
            if model:
                models[pair] = (model, scaler, acc)
                path = save_model_local(pair, model, scaler, acc)
                if path:
                    tg_send_file(path, f"🆕 {pair} | Acc:{acc:.1f}%")
        else:
            models[pair] = (None, None, 0)

    ready = sum(1 for v in models.values() if v[0] is not None)
    msg = f"✅ Models ready: {ready}/{len(PAIRS)}\n"
    for pair, (m, s, acc) in models.items():
        msg += f"  {'✅' if m else '❌'} {pair}: {acc:.1f}%\n"
    print("\n" + msg)
    tg_log(msg)

# ============================================================
#  SCAN LOOP
# ============================================================
def scan_loop():
    initial_setup()
    while True:
        try:
            fetch_all_prices()
            ist = datetime.now(pytz.timezone("Asia/Kolkata"))
            stats["last_scan"] = ist.strftime("%I:%M %p")

            if not is_good_session():
                print(f"[{stats['last_scan']}] Outside session")
                time.sleep(120)
                continue

            print(f"\n[{stats['last_scan']}] Scanning {len(PAIRS)} pairs...")
            found = False

            for pair in PAIRS:
                direction, score, strength = generate_signal_ml(pair)
                if direction:
                    price = live_prices.get(pair) or get_current_price(pair) or 0
                    sig = {
                        "time":      ist.strftime("%I:%M %p"),
                        "pair":      pair,
                        "direction": direction,
                        "score":     score,
                        "strength":  strength,
                        "price":     price,
                        "result":    "⏳ Pending",
                        "model_acc": f"{models.get(pair, (None,None,0))[2]:.1f}%",
                    }
                    signal_history.insert(0, sig)
                    if len(signal_history) > 30:
                        signal_history.pop()
                    stats["total"] += 1
                    pending_results.append({
                        "pair":      pair,
                        "direction": direction,
                        "price":     price,
                        "time":      ist.strftime("%I:%M %p"),
                        "timestamp": time.time(),
                    })
                    print(f"✅ {pair} {direction} {strength}")
                    tg_signal(
                        f"🚨 SIGNAL!\n"
                        f"💱 {pair}\n"
                        f"📊 {direction} {strength}\n"
                        f"💰 @ {price:.5f}\n"
                        f"⏱ 2 MIN expiry"
                    )
                    found = True

            if not found:
                print("⚪ No signal this scan")

        except Exception as e:
            print(f"Scan error: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(120)

# ============================================================
#  API ROUTES
# ============================================================
@app.route("/signals")
def get_signals():
    total = stats["wins"] + stats["losses"]
    acc   = round(stats["wins"] / total * 100, 1) if total > 0 else 0
    return jsonify({
        "signals":     signal_history,
        "prices":      live_prices,
        "stats":       {**stats, "accuracy": acc},
        "model_stats": {
            p: {"acc": f"{v[2]:.1f}%", "ready": v[0] is not None}
            for p, v in models.items()
        },
    })

@app.route("/ping")
def ping():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    print("🚀 QX Signals — XGBoost ML System!")
    print("🤖 Model: XGBoost | 15 Features")
    print("📊 Min confidence: 65% | 2 MIN expiry")
    print(f"📡 Pairs: {', '.join(PAIRS)}")
    print("💾 Data: Disk + Telegram backup")
    threading.Thread(target=scan_loop,     daemon=True).start()
    threading.Thread(target=check_results, daemon=True).start()
    threading.Thread(target=retrain_loop,  daemon=True).start()
    app.run(host="0.0.0.0", port=5001)
