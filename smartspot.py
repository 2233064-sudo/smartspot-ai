
import ccxt
import requests
import time
from datetime import datetime, timezone

# =========================================================
# گروه تخصصی سیگنال فیوچرز مهندس عدنانی
# ADNANI FUTURES V7
# Signals only. NO automatic orders.
# =========================================================

TELEGRAM_TOKEN = ""
TELEGRAM_CHAT_ID = ""

WATCHLIST = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "BNB/USDT", "ADA/USDT", "DOGE/USDT", "LINK/USDT",
    "AVAX/USDT", "LTC/USDT", "DOT/USDT", "ASTER/USDT"
]

MIN_SCORE = 88
RISK_PERCENT = 0.5
LEVERAGE_REFERENCE = 2

exchange = ccxt.binanceusdm({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})


def ema(values, period):
    if len(values) < period:
        return None

    alpha = 2 / (period + 1)
    result = values[0]

    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    trs = []

    for i in range(1, len(candles)):
        high = candles[i][2]
        low = candles[i][3]
        prev_close = candles[i - 1][4]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        trs.append(tr)

    return sum(trs[-period:]) / period


def fetch_data(symbol, timeframe):
    return exchange.fetch_ohlcv(
        symbol, timeframe, limit=120
    )


def trend(candles):
    closes = [c[4] for c in candles]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    if e20 is None or e50 is None:
        return "NEUTRAL"

    last = closes[-1]

    if last > e20 > e50:
        return "BULL"

    if last < e20 < e50:
        return "BEAR"

    return "NEUTRAL"


def btc_filter():
    h4 = fetch_data("BTC/USDT", "4h")
    h1 = fetch_data("BTC/USDT", "1h")

    t4 = trend(h4)
    t1 = trend(h1)

    if t4 == "BULL" and t1 == "BULL":
        return "BULL"

    if t4 == "BEAR" and t1 == "BEAR":
        return "BEAR"

    return "NEUTRAL"


def analyze(symbol, btc_state):
    h4 = fetch_data(symbol, "4h")
    h1 = fetch_data(symbol, "1h")
    m15 = fetch_data(symbol, "15m")

    t4 = trend(h4)
    t1 = trend(h1)

    closes = [c[4] for c in m15]
    volumes = [c[5] for c in m15]

    price = closes[-1]
    current_rsi = rsi(closes)
    volatility = atr(m15)
    e20 = ema(closes, 20)

    if current_rsi is None or volatility is None:
        return None

    if volatility <= 0 or e20 is None:
        return None

    avg_volume = sum(volumes[-21:-1]) / 20

    if avg_volume <= 0:
        return None

    volume_ratio = volumes[-1] / avg_volume

    candle = m15[-1]
    candle_range = candle[2] - candle[3]

    if candle_range <= 0:
        return None

    body = abs(candle[4] - candle[1])
    body_ratio = body / candle_range

    direction = None

    if t4 == "BULL" and t1 == "BULL":
        if price > e20 and 50 <= current_rsi <= 68:
            if candle[4] > candle[1] and body_ratio >= 0.45:
                direction = "LONG"

    if t4 == "BEAR" and t1 == "BEAR":
        if price < e20 and 32 <= current_rsi <= 50:
            if candle[4] < candle[1] and body_ratio >= 0.45:
                direction = "SHORT"

    if direction is None:
        return None

    score = 0
    reasons = []

    score += 25
    reasons.append("4H and 1H trend alignment")

    score += 20
    reasons.append("15M candle confirmation")

    score += 15
    reasons.append("RSI confirmation")

    if volume_ratio >= 1.5:
        score += 15
        reasons.append("Strong volume")

    elif volume_ratio >= 1.2:
        score += 10
        reasons.append("Above average volume")

    else:
        return None

    if direction == "LONG":

        if btc_state == "BULL":
            score += 15
            reasons.append("BTC bullish alignment")

        elif btc_state == "NEUTRAL":
            score += 8
            reasons.append("BTC neutral")

        else:
            return None

    elif direction == "SHORT":

        if btc_state == "BEAR":
            score += 15
            reasons.append("BTC bearish alignment")

        elif btc_state == "NEUTRAL":
            score += 8
            reasons.append("BTC neutral")

        else:
            return None

    if direction == "LONG" and price > e20:
        score += 10

    elif direction == "SHORT" and price < e20:
        score += 10

    sl_distance = volatility * 1.5

    if direction == "LONG":
        sl = price - sl_distance
        tp1 = price + (sl_distance * 2)
        tp2 = price + (sl_distance * 3)

    else:
        sl = price + sl_distance
        tp1 = price - (sl_distance * 2)
        tp2 = price - (sl_distance * 3)

    if score < MIN_SCORE:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "entry": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rsi": current_rsi,
        "volume_ratio": volume_ratio,
        "btc": btc_state,
        "reasons": reasons
    }


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials are missing.")
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        response = requests.post(
            url, json=payload, timeout=20
        )
        response.raise_for_status()

    except Exception as exc:
        print("Telegram error:", exc)


def format_signal(s):
    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    return (
        "GROUP: گروه تخصصی سیگنال فیوچرز مهندس عدنانی\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ADNANI FUTURES V7\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + s["symbol"] + " | " + s["direction"] + "\n"
        + "Signal score: " + str(s["score"]) + "/100\n"
        + "━━━━━━━━━━━━━━━━━━\n"
        + "Entry: " + format(s["entry"], ".8f") + "\n"
        + "Stop Loss: " + format(s["sl"], ".8f") + "\n"
        + "TP1: " + format(s["tp1"], ".8f") + "\n"
        + "TP2: " + format(s["tp2"], ".8f") + "\n"
        + "━━━━━━━━━━━━━━━━━━\n"
        + "RSI: " + format(s["rsi"], ".2f") + "\n"
        + "Volume: " + format(s["volume_ratio"], ".2f") + "x\n"
        + "BTC filter: " + s["btc"] + "\n"
        + "━━━━━━━━━━━━━━━━━━\n"
        + "Reasons:\n"
        + "\n".join("- " + x for x in s["reasons"])
        + "\n━━━━━━━━━━━━━━━━━━\n"
        + "Risk reference: " + str(RISK_PERCENT) + "%\n"
        + "Leverage reference: " + str(LEVERAGE_REFERENCE) + "x\n"
        + "Time: " + now + "\n"
        + "Signal only. No order was placed."
    )


def main():
    print("Starting Adnani Futures V7 scan...")

    valid = []
    errors = 0

    try:
        btc_state = btc_filter()

    except Exception as exc:
        print("BTC filter error:", exc)

        send_telegram(
            "گروه تخصصی سیگنال فیوچرز مهندس عدنانی\n"
            "BTC data unavailable.\n"
            "Scan aborted for safety."
        )
        return

    print("BTC state:", btc_state)

    for symbol in WATCHLIST:

        try:
            result = analyze(symbol, btc_state)

            if result:
                valid.append(result)
                print("SETUP:", symbol, result["direction"])

            else:
                print("No setup:", symbol)

        except Exception as exc:
            errors += 1
            print("Error:", symbol, exc)

        time.sleep(0.5)

    valid.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    if valid:

        for s in valid:
            send_telegram(format_signal(s))

        report = (
            "GROUP: گروه تخصصی سیگنال فیوچرز مهندس عدنانی\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Scan completed.\n"
            "BTC filter: " + btc_state + "\n"
            "Pairs checked: " + str(len(WATCHLIST)) + "\n"
            "Valid setups: " + str(len(valid)) + "\n"
            "Errors: " + str(errors) + "\n"
            "Signals sent.\n"
            "No orders were placed."
        )

    else:

        report = (
            "GROUP: گروه تخصصی سیگنال فیوچرز مهندس عدنانی\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Scan completed successfully.\n"
            "BTC filter: " + btc_state + "\n"
            "Pairs checked: " + str(len(WATCHLIST)) + "\n"
            "Valid setups: 0\n"
            "Errors: " + str(errors) + "\n"
            "No qualified futures signal.\n"
            "Filters were not relaxed.\n"
            "No order was placed."
        )

    send_telegram(report)


if __name__ == "__main__":
    main()
