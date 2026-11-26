#!/usr/bin/env python3
# SMARTSPOT AI V6 - KuCoin SPOT scanner only; never places orders.

import os
import time
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import ccxt

VERSION = "V6"
WATCHLIST = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "BNB/USDT", "ADA/USDT", "DOGE/USDT", "LINK/USDT",
    "AVAX/USDT", "LTC/USDT", "DOT/USDT", "ASTER/USDT",
]
CANDLE_LIMIT = 150
MIN_SCORE = 72
MIN_NET_RR_TP1 = 1.20
MAX_SIGNALS_PER_RUN = 1
PAUSE = 0.20
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(len(values)-period, len(values))]
    gain = sum(max(x, 0) for x in changes) / period
    loss = sum(max(-x, 0) for x in changes) / period
    return 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    tr = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i][2], candles[i][3], candles[i-1][4]
        tr.append(max(high-low, abs(high-prev_close), abs(low-prev_close)))
    return sum(tr[-period:]) / period


def get_candles(exchange, symbol, timeframe):
    rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=CANDLE_LIMIT)
    if len(rows) < 60:
        raise ValueError(f"Not enough {timeframe} candles: {len(rows)}")
    return rows[:-1]  # Never analyze the still-forming candle.


def rising_structure(candles):
    sample = candles[-40:]
    old, new = sample[:20], sample[20:]
    return (
        max(c[2] for c in new) > max(c[2] for c in old)
        and min(c[3] for c in new) > min(c[3] for c in old)
    )


def stats(candles):
    closes = [c[4] for c in candles]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    return {
        "close": closes[-1], "ema20": e20, "ema50": e50,
        "bull": bool(e20 and e50 and closes[-1] > e20 > e50),
        "rsi": rsi(closes), "atr": atr(candles),
        "structure": rising_structure(candles),
    }


def analyze(symbol, c4, c1, c15, btc_ok):
    a4, a1, a15 = stats(c4), stats(c1), stats(c15)
    closes = [c[4] for c in c15]
    price, e20, e50 = closes[-1], ema(closes, 20), ema(closes, 50)
    vol_atr, current_rsi = a15["atr"], a15["rsi"]
    if not e20 or not e50 or not vol_atr or current_rsi is None or price <= 0:
        return None

    atr_pct = 100 * vol_atr / price
    if not 0.15 <= atr_pct <= 4.0:
        return None

    # Pullback watch zone around the 15m EMA20.
    zone_low = e20 - 0.25 * vol_atr
    zone_high = e20 + 0.35 * vol_atr
    in_zone = zone_low <= price <= zone_high
    trend15 = price > e50 and e20 > e50

    cur, prev = c15[-1], c15[-2]
    bullish = cur[4] > cur[1]
    engulfing = (
        bullish and prev[4] < prev[1]
        and cur[4] >= prev[1] and cur[1] <= prev[4]
    )
    candle_ok = bullish and (engulfing or abs(cur[4]-cur[1]) >= 0.35*vol_atr)

    vols = [c[5] for c in c15[-21:-1]]
    avg_vol = sum(vols)/len(vols) if vols else 0
    rel_vol = cur[5]/avg_vol if avg_vol else 0

    # Transparent rule score, not a probability of winning.
    score, reasons = 0, []
    checks = [
        (a4["bull"], 18, "4H trend bullish"),
        (a1["bull"], 18, "1H trend bullish"),
        (a4["structure"] or a1["structure"], 10, "rising swing structure"),
        (trend15, 14, "15m trend aligned"),
        (in_zone, 10, "price in pullback watch zone"),
        (rel_vol >= 1.20, 12, f"relative volume {rel_vol:.2f}x"),
        (candle_ok, 10, "bullish candle confirmation"),
        (45 <= current_rsi <= 68, 5, f"RSI {current_rsi:.1f}"),
        (btc_ok, 3, "BTC filter passed"),
    ]
    for passed, points, label in checks:
        if passed:
            score += points
            reasons.append(label)

    if not (a4["bull"] and a1["bull"] and trend15 and in_zone and candle_ok):
        return None
    if not btc_ok or score < MIN_SCORE:
        return None

    swing_low = min(c[3] for c in c15[-12:])
    stop = min(swing_low - 0.25*vol_atr, price - 1.10*vol_atr)
    risk = price - stop
    if risk <= 0 or risk/price > 0.06:
        return None

    # Approximate round-trip fee allowance. Adjust for your actual fee tier.
    fee = 0.002
    tp1, tp2, tp3 = price+1.5*risk, price+2.5*risk, price+3.5*risk
    net_reward = (tp1-price) - (price+tp1)*fee
    net_risk = risk + (price+stop)*fee
    net_rr = net_reward/net_risk if net_risk > 0 else 0
    if net_rr < MIN_NET_RR_TP1:
        return None

    return {
        "symbol": symbol, "price": price, "zone_low": zone_low,
        "zone_high": zone_high, "stop": stop, "tp1": tp1,
        "tp2": tp2, "tp3": tp3, "score": score,
        "rsi": current_rsi, "atr_pct": atr_pct,
        "rel_vol": rel_vol, "net_rr": net_rr,
        "reasons": reasons, "candle_time": cur[0],
    }


def price_text(value):
    if value >= 100:
        return f"{value:,.3f}"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.5f}"
    return f"{value:.8f}"


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram secrets missing: message not sent.")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": message,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
        if not result.get("ok"):
            print("Telegram API error:", result)
            return False
        print("Telegram message sent.")
        return True
    except Exception as exc:
        print(f"Telegram send error: {type(exc).__name__}: {exc}")
        return False


def main():
    print(f"SMARTSPOT AI {VERSION} starting: {datetime.now(timezone.utc).isoformat()}")
    exchange = ccxt.kucoin({
        "enableRateLimit": True, "timeout": 30000,
        "options": {"defaultType": "spot"},
    })
    try:
        markets = exchange.load_markets()
        btc4 = stats(get_candles(exchange, "BTC/USDT", "4h"))
        time.sleep(PAUSE)
        btc1 = stats(get_candles(exchange, "BTC/USDT", "1h"))
        btc_ok = btc4["bull"] and btc1["bull"]
        print("BTC filter:", "PASS" if btc_ok else "BLOCK")

        candidates = []
        for symbol in WATCHLIST:
            try:
                market = markets.get(symbol)
                if not market or not market.get("spot") or not market.get("active", True):
                    print("Skipping unavailable spot pair:", symbol)
                    continue
                c4 = get_candles(exchange, symbol, "4h")
                time.sleep(PAUSE)
                c1 = get_candles(exchange, symbol, "1h")
                time.sleep(PAUSE)
                c15 = get_candles(exchange, symbol, "15m")
                time.sleep(PAUSE)
                result = analyze(symbol, c4, c1, c15, btc_ok)
                if result:
                    candidates.append(result)
                    print(f"Candidate {symbol}: score {result['score']}")
                else:
                    print("No valid setup:", symbol)
            except Exception as exc:
                print(f"Pair error {symbol}: {type(exc).__name__}: {exc}")

        candidates.sort(key=lambda x: (x["score"], x["net_rr"]), reverse=True)
        if not candidates:
            print("No setup passed V6 filters. Sending scan-status report to Telegram.")
            scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            btc_status = "BLOCK" if not btc_ok else "PASS"
            report = "\n".join([
                f"SMARTSPOT AI {VERSION} | SCAN REPORT",
                "━━━━━━━━━━━━━━━━━━",
                "Scan completed successfully.",
                f"Time: {scan_time}",
                f"BTC trend filter: {btc_status}",
                f"Pairs checked: {len(WATCHLIST)}",
                "Valid setups: 0",
                "No trade signal this run.",
                "No order was placed. Filters were not relaxed.",
                "━━━━━━━━━━━━━━━━━━",
                "This is a scan-status update, not a buy/sell recommendation.",
            ])
            send_telegram(report)
            return

        for s in candidates[:MAX_SIGNALS_PER_RUN]:
            reason_text = "\n".join("- " + x for x in s["reasons"])
            candle_utc = datetime.fromtimestamp(
                s["candle_time"]/1000, timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
            lines = [
                f"SMARTSPOT AI {VERSION} | SPOT BUY SETUP",
                "━━━━━━━━━━━━━━━━━━",
                f"Pair: {s['symbol']}",
                f"Setup quality: {s['score']}/100",
                f"Reference price: {price_text(s['price'])}",
                f"Entry watch zone: {price_text(s['zone_low'])} - {price_text(s['zone_high'])}",
                f"Stop loss: {price_text(s['stop'])}",
                f"TP1 (1.5R): {price_text(s['tp1'])}",
                f"TP2 (2.5R): {price_text(s['tp2'])}",
                f"TP3 (3.5R): {price_text(s['tp3'])}",
                f"Estimated net RR to TP1: {s['net_rr']:.2f}",
                f"RSI: {s['rsi']:.1f} | ATR: {s['atr_pct']:.2f}%",
                f"Relative volume: {s['rel_vol']:.2f}x",
                "Why it passed:", reason_text,
                "━━━━━━━━━━━━━━━━━━",
                "Closed 15m candle analysis. Not a guaranteed result.",
                "No order placed. No martingale. Manage risk independently.",
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
