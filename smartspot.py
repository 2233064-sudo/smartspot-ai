# ==============================================================
# SMARTSPOT AI V4
# PRICE ACTION + FIBONACCI + LIMITED MARTINGALE
# KUCOIN SPOT | GITHUB ACTIONS | TELEGRAM
# ==============================================================

import os
import json
import ccxt
import pandas as pd
import requests
import time
import traceback
from datetime import datetime, timezone

# ==============================================================
# SETTINGS
# ==============================================================

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SIGNAL_COOLDOWN = 6 * 60 * 60

TIMEFRAME_HTF = "1h"
TIMEFRAME_ENTRY = "15m"

MIN_SCORE = 80

MARTINGALE_LEVELS = [0, 2, 4, 6]
MARTINGALE_MULTIPLIERS = [1.0, 1.5, 2.0, 2.5]

STATE_FILE = "signal_state.json"

# ============================================================== 
# WATCHLIST
# ==============================================================

WATCHLIST_BASE = [
    "ETHFI", "DASH", "ZEC", "RENDER", "TAO",
    "NEAR", "UNI", "STX", "BOME", "ENA",
    "ZRO", "QNT", "XMR", "TUT", "PUMP",
    "PYTH", "SUI", "APT", "ONDO", "GMX",
    "TIA", "WLD", "GIGGLE", "ARIA", "OP",
    "HYPE", "VIRTUAL", "ARB", "BCH", "AVNT",
    "AVAX", "ADA", "JUP", "KAS", "SOL",
    "DOGE", "CAKE", "ETC", "GRAM", "ASTER",
    "ETH", "SHIB", "DRIFT", "BTC", "LDO",
    "FIL", "HEI", "HEMI", "WIF", "ALLO",
    "SEI", "HBAR", "BONK", "CLANKER", "ACE",
    "PEPE", "FET", "PAXG", "XAUT", "ATOM",
    "TRX", "ORDER", "CC", "WLFI", "ICP",
    "HMSTR", "AIO", "Q", "LUNC", "AIN"
]

# ============================================================== 
# EXCHANGE
# ==============================================================

exchange = ccxt.kucoin({
    "enableRateLimit": True,
    "timeout": 20000
})

# ============================================================== 
# STATE
# ==============================================================

def load_state():

    try:

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    except (FileNotFoundError, json.JSONDecodeError):

        return {"last_alert": {}}


def save_state(state):

    with open(STATE_FILE, "w", encoding="utf-8") as f:

        json.dump(
            state,
            f,
            indent=2
        )


# ============================================================== 
# TELEGRAM
# ==============================================================

def send_telegram(message):

    if not TOKEN or not CHAT_ID:

        print("Telegram credentials are missing.")

        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:

        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=20
        )

        response.raise_for_status()

        result = response.json()

        if result.get("ok"):

            return True

        print("Telegram error:", result)

        return False

    except Exception as e:

        print("Telegram error:", e)

        return False


# ============================================================== 
# MARKET LIST
# ==============================================================

def get_symbols():

    markets = exchange.load_markets()

    symbols = []

    for base in WATCHLIST_BASE:

        symbol = base + "/USDT"

        market = markets.get(symbol)

        if market is None:
            continue

        if not market.get("active", True):
            continue

        if market.get("spot") is False:
            continue

        symbols.append(symbol)

    return list(dict.fromkeys(symbols))


# ============================================================== 
# OHLCV
# ==============================================================

def get_data(symbol, timeframe, limit=150):

    try:

        data = exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit
        )

        df = pd.DataFrame(
            data,
            columns=[
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        )

        if len(df) < 60:
            return None

        # REMOVE UNFINISHED CANDLE
        df = df.iloc[:-1].copy()

        df["time"] = pd.to_datetime(
            df["time"],
            unit="ms",
            utc=True
        )

        return df.reset_index(drop=True)

    except Exception as e:

        print("Data error:", symbol, e)

        return None


# ============================================================== 
# PRICE ACTION
# ==============================================================

def bullish_engulfing(df):

    if len(df) < 3:
        return False

    p = df.iloc[-2]
    c = df.iloc[-1]

    return (
        p["close"] < p["open"]
        and c["close"] > c["open"]
        and c["open"] <= p["close"]
        and c["close"] >= p["open"]
    )


def bullish_pinbar(df):

    c = df.iloc[-1]

    body = abs(c["close"] - c["open"])

    candle_range = c["high"] - c["low"]

    if candle_range <= 0:
        return False

    lower_wick = min(c["open"], c["close"]) - c["low"]

    upper_wick = c["high"] - max(c["open"], c["close"])

    return (
        lower_wick >= body * 2
        and lower_wick > upper_wick
        and c["close"] > c["open"]
    )


def bullish_structure(df):

    if len(df) < 20:
        return False

    recent = df.iloc[-10:]

    older = df.iloc[-20:-10]

    return (
        recent["high"].max() > older["high"].max()
        and recent["low"].min() >= older["low"].min()
    )


# ============================================================== 
# FIBONACCI
# ==============================================================

def fibonacci_analysis(df):

    data = df.iloc[-80:-1].copy()

    if len(data) < 40:
        return None

    swing_low = float(data["low"].min())

    swing_high = float(data["high"].max())

    if swing_high <= swing_low:
        return None

    swing_range = swing_high - swing_low

    fib_382 = swing_high - swing_range * 0.382

    fib_50 = swing_high - swing_range * 0.50

    fib_618 = swing_high - swing_range * 0.618

    fib_786 = swing_high - swing_range * 0.786

    return {
        "low": swing_low,
        "high": swing_high,
        "fib_382": fib_382,
        "fib_50": fib_50,
        "fib_618": fib_618,
        "fib_786": fib_786
    }


# ============================================================== 
# ANALYSIS
# ==============================================================

def analyze_symbol(symbol):

    try:

        h1 = get_data(symbol, TIMEFRAME_HTF, 150)

        m15 = get_data(symbol, TIMEFRAME_ENTRY, 150)

        if h1 is None or m15 is None:
            return None

        fib = fibonacci_analysis(h1)

        if fib is None:
            return None

        structure = bullish_structure(h1)

        if not structure:
            return None

        price = float(m15.iloc[-1]["close"])

        zone_low = min(fib["fib_618"], fib["fib_50"])

        zone_high = max(fib["fib_618"], fib["fib_50"])

        zone_width = abs(fib["high"] - fib["low"]) * 0.03

        in_zone = (
            zone_low - zone_width
            <= price
            <= zone_high + zone_width
        )

        if not in_zone:
            return None

        engulfing = bullish_engulfing(m15)

        pinbar = bullish_pinbar(m15)

        candle_confirmed = engulfing or pinbar

        if not candle_confirmed:
            return None

        score = 0

        reasons = []

        score += 30

        reasons.append("Bullish price structure")

        score += 30

        reasons.append("Fibonacci 50%-61.8% retracement")

        if engulfing:

            score += 25

            reasons.append("Bullish engulfing confirmation")

        elif pinbar:

            score += 25

            reasons.append("Bullish pinbar confirmation")

        distance = abs(price - fib["fib_618"])

        fib_distance = abs(fib["high"] - fib["low"])

        if fib_distance > 0:

            proximity = max(
                0,
                1 - distance / fib_distance
            )

            score += int(proximity * 15)

        score = min(score, 100)

        if score < MIN_SCORE:
            return None

        entry = price

        mg_entries = []

        mg_sizes = []

        for drop, multiplier in zip(
            MARTINGALE_LEVELS,
            MARTINGALE_MULTIPLIERS
        ):

            level_price = entry * (1 - drop / 100)

            mg_entries.append(level_price)

            mg_sizes.append(multiplier)

        total_weight = sum(mg_sizes)

        avg_entry = sum(
            p * w
            for p, w in zip(mg_entries, mg_sizes)
        ) / total_weight

        sl = fib["low"] * 0.995

        if sl >= avg_entry:
            return None

        risk = avg_entry - sl

        if risk <= 0:
            return None

        tp1 = max(
            fib["high"],
            avg_entry + risk * 1.5
        )

        tp2 = avg_entry + risk * 2.5

        tp3 = avg_entry + risk * 4.0

        return {
            "symbol": symbol,
            "direction": "BUY",
            "score": score,
            "entry": entry,
            "avg_entry": avg_entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "mg_entries": mg_entries,
            "mg_sizes": mg_sizes,
            "fib": fib,
            "reasons": reasons,
            "candle_time": m15.iloc[-1]["time"]
        }

    except Exception as e:

        print("Analysis error:", symbol, e)

        return None


# ============================================================== 
# FORMAT SIGNAL
# ==============================================================

def format_signal(s):

    msg = f"""
━━━━━━━━━━━━━━━━━━━━
🟢 SMARTSPOT AI V4
━━━━━━━━━━━━━━━━━━━━

📌 Coin: {s['symbol']}

📈 Direction: BUY SPOT

⭐ Score: {s['score']}/100

━━━━━━━━━━━━━━━━━━━━

💰 Initial Entry:

{s['entry']:.8g}

━━━━━━━━━━━━━━━━━━━━

📐 FIBONACCI

38.2%:
{s['fib']['fib_382']:.8g}

50%:
{s['fib']['fib_50']:.8g}

61.8%:
{s['fib']['fib_618']:.8g}

78.6%:
{s['fib']['fib_786']:.8g}

━━━━━━━━━━━━━━━━━━━━

🧮 MARTINGALE

"""

    for i, (price, size) in enumerate(
        zip(s["mg_entries"], s["mg_sizes"])
    ):

        msg += (
            f"\nLevel {i+1}: "
            f"{price:.8g}"
            f"\nMultiplier: {size}x\n"
        )

    msg += f"""

━━━━━━━━━━━━━━━━━━━━

📊 Weighted Average Entry:

{s['avg_entry']:.8g}

🛑 Stop Loss:

{s['sl']:.8g}

━━━━━━━━━━━━━━━━━━━━

🎯 TP1:

{s['tp1']:.8g}

🎯 TP2:

{s['tp2']:.8g}

🎯 TP3:

{s['tp3']:.8g}

━━━━━━━━━━━━━━━━━━━━

🔎 PRICE ACTION
"""

    for reason in s["reasons"]:

        msg += "\n• " + reason

    msg += f"""

🕯 Candle:

{s['candle_time']}

━━━━━━━━━━━━━━━━━━━━

⚠️ SPOT SCANNER

No orders executed.
"""

    return msg


# ============================================================== 
# SCAN MARKET
# ==============================================================

def scan_market(state):

    print("\n" + "=" * 50)

    print("SCAN")

    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

    print("=" * 50)

    symbols = get_symbols()

    print("Available markets:", len(symbols))

    results = []

    for i, symbol in enumerate(symbols, 1):

        print(f"[{i}/{len(symbols)}]", symbol, end=" ")

        result = analyze_symbol(symbol)

        if result:

            results.append(result)

            print("QUALIFIED", result["score"])

        else:

            print("No signal")

        time.sleep(0.15)

    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    if not results:

        print("No qualified signal.")

        return

    best = results[0]

    print("\nBEST SIGNAL:", best["symbol"], best["score"])

    now = time.time()

    last_alert = state.get("last_alert", {})

    key = best["symbol"]

    previous = float(last_alert.get(key, 0))

    if now - previous < SIGNAL_COOLDOWN:

        print("Cooldown active.")

        return

    message = format_signal(best)

    success = send_telegram(message)

    if success:

        last_alert[key] = now

        state["last_alert"] = last_alert

        save_state(state)

        print("Telegram sent:", best["symbol"])


# ============================================================== 
# MAIN
# ==============================================================

def main():

    print("SMARTSPOT AI V4 STARTED")

    print("PRICE ACTION + FIBONACCI + MARTINGALE")

    print("KUCOIN SPOT")

    print("Scanning watchlist only")

    state = load_state()

    scan_market(state)


if __name__ == "__main__":

    main()
          
