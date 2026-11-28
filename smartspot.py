
import ccxt
import time
import requests
from datetime import datetime, timezone

# ==================================================
# GROUP: گروه تخصصی سیگنال فیوچرز مهندس عدنانی
# ADNANI FUTURES V7
# Signals only. NO automatic orders.
# ==================================================

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

    result = values[0]
    alpha = 2 / (period + 1)

    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

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
        previous_close = candles[i - 1][4]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
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
    r = rsi(closes)
    volatility = atr(m15)

    if r is None or volatility is None or volatility <= 0:
        return None

    e20 = ema(closes, 20)
    avg_volume = sum(volumes[-21:-1]) / 20

    if avg_volume <= 0:
        return None

    volume_ratio = volumes[-1] / avg_volume

    candle = m15[-1]
    previous = m15[-2]

    candle_range = candle[2] - candle[3]

    if candle_range <= 0:
        return None

    body = abs(candle[4] - candle[1])
    body_ratio = body / candle_range

    direction = None

    if t4 == "BULL" and t1 == "BULL":
        if price > e20 and 50 <= r <= 68:
            if candle[4] > candle[1] and body_ratio >= 0.45:
                direction = "LONG"

    if t4 == "BEAR" and t1 == "BEAR":
        if price < e20 and 32 <= r <= 50:
            if candle[4] < candle[1] and body_ratio >= 0.45:
                direction = "SHORT"

    if direction is None:
        return None

    score = 0
    reasons = []

    # Multi-timeframe trend
    score += 25
    reasons.append("4H and 1H trend alignment")

    # 15-minute momentum confirmation
    score += 20
    reasons.append("15M candle confirmation")

    # RSI confirmation
    score += 15
    reasons.append("RSI confirmation")

    # Volume confirmation
    if volume_ratio >= 1.5:
        score += 15
        reasons.append("Strong volume")

    elif volume_ratio >= 1.2:
        score += 10
        reasons.append("Above-average volume")

    else:
        return None

    # BTC market filter
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

    # Price position
    if direction == "LONG" and price > e20:
        score += 10

    elif direction == "SHORT" and price < e20:
        score += 10

    # Risk calculations
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
        "rsi": r,
        "volume_ratio": volume_ratio,
        "btc": btc_state,
        "reasons": reasons
    }


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials are missing.")
        return

    url = (
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
        f"/sendMessage"
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
        "🏆 گروه تخصصی سیگنال فیوچرز مهندس عدنانی\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🚨 {s['symbol']} | {s['direction']}\n"
        f"⭐ Signal score: {s['score']}/100\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Entry: {s['entry']:.8f}\n"
        f"Stop Loss: {s['sl']:.8f}\n"
        f"TP1: {s['tp1']:.8f}\n"
        f"TP2: {s['tp2']:.8f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"RSI: {s['rsi']:.2f}\n"
        f"Volume: {s['volume_ratio']:.2f}x\n"
        f"BTC filter: {s['btc']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Reasons:\n"
        + "\n".join(f"• {x}" for x in s["reasons"])
        + "\n━━━━━━━━━━━━━━━━━━\n"
        f"Risk reference: {RISK_PERCENT}%\n"
        f"Leverage reference: {LEVERAGE_REFERENCE}x\n"
        f"Time: {now}\n"
        "⚠️ Signal only. No order was placed."
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

    valid.sort(key=lambda x: x["score"], reverse=True)

    if valid:
        for s in valid:
            send_telegram(format_signal(s))

        report = (
            "📊 گروه تخصصی سیگنال فیوچرز مهندس عدنانی\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Scan completed.\n"
            f"BTC filter: {btc_state}\n"
            f"Pairs checked: {len(WATCHLIST)}\n"
            f"Valid setups: {len(valid)}\n"
            f"Errors: {errors}\n"
            "Signals sent.\n"
            "No orders were placed."
        )

    else:
        report = (
            "📊 گروه تخصصی سیگنال فیوچرز مهندس عدنانی\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Scan completed successfully.\n"
            f"BTC filter: {btc_state}\n"
            f"Pairs checked: {len(WATCHLIST)}\n"
            "Valid setups: 0\n"
            f"Errors: {errors}\n"
            "No qualified futures signal.\n"
            "Filters were not relaxed.\n"
            "No order was placed."
        )

    send_telegram(report)


if __name__ == "__main__":
    main()
Manage risk independently.",
                f"Candle time (UTC): {candle_utc}",
            ]
            send_telegram("\n".join(lines))
    except Exception as exc:
        print(f"FATAL SCANNER ERROR: {type(exc).__name__}: {exc}")
        raise
    finally:
        try:
            exchange.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
