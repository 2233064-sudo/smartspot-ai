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
            print("No setup passed V6 filters. No alert sent.")
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
eed. Check price and liquidity before acting.',
              '📍 Manual execution only | No orders placed','🕯 Closed candle: '+s['candle_time'],
              '━━━━━━━━━━━━━━━━━━━━','⚠️ Setup score is rule-based, not calibrated win probability.']
    return '\n'.join(lines)


def monitor_active(state):
    active = state.setdefault('active_signals', {})
    closed = state.setdefault('closed_signals', [])
    for sym, sig in list(active.items()):
        try:
            candles = exchange.fetch_ohlcv(sym, '15m', limit=3)
            if not candles: continue
            # Inspect completed candles only; last row may be forming.
            for row in candles[:-1]:
                t, o, h, l, c, v = row
                if int(t) <= int(sig.get('last_checked_ms', sig.get('created_ms',0))): continue
                hit_sl, hit_tp1 = l <= sig['sl'], h >= sig['tp1']
                # Conservative handling of same-candle collision: count stop first.
                if hit_sl and hit_tp1: outcome='SL (same-candle ambiguity; conservative)'
                elif hit_sl: outcome='SL'
                elif hit_tp1: outcome='TP1'
                else: outcome=None
                sig['last_checked_ms']=int(t)
                if outcome:
                    sig['status']=outcome; sig['closed_at']=utcnow().isoformat(); closed.append(sig)
                    telegram('━━━━━━━━━━━━━━━━━━━━\n📋 SMARTSPOT V6 UPDATE\n'+sym+'\nOutcome: '+outcome+'\nReference tracking only, not actual fills.\n━━━━━━━━━━━━━━━━━━━━')
                    del active[sym]; break
            # expiry
            age = (utcnow()-datetime.fromisoformat(sig['created'].replace('Z','+00:00'))).total_seconds()/3600
            if sym in active and age >= SIGNAL_EXPIRY_HOURS:
                sig['status']='EXPIRED'; sig['closed_at']=utcnow().isoformat(); closed.append(sig)
                telegram('📋 SMARTSPOT V6 UPDATE\n'+sym+'\nOutcome: EXPIRED (48h tracking window)\nReference tracking only.')
                del active[sym]
        except Exception as e:
            print('Monitor error',sym,e)
    # Keep bounded history
    state['closed_signals']=closed[-300:]


def scan(state):
    symbols=get_symbols(); print('Markets:',len(symbols))
    results=[]
    for i,sym in enumerate(symbols,1):
        print(f'[{i}/{len(symbols)}] {sym}',end=' ')
        try:
            s=analyze(sym)
            if s: results.append(s); print('QUALIFIED',s['score'])
            else: print('No setup')
        except Exception as e: print('Analysis exception',sym,e)
        time.sleep(.12)
    results.sort(key=lambda x:x['score'],reverse=True)
    if not results: print('No qualified setups'); return
    active=state.setdefault('active_signals',{})
    candidates=[s for s in results if s['symbol'] not in active]
    if not candidates: print('All candidates already tracked'); return
    best=candidates[0]
    if len(active)>=MAX_ACTIVE_SIGNALS: print('Active-signal cap reached'); return
    now=time.time(); last=state.setdefault('last_alert',{}).get(best['symbol'],0)
    if now-float(last)<COOLDOWN_SECONDS: print('Cooldown active'); return
    if telegram(signal_message(best)):
        best['created_ms']=int(now*1000)
        best['last_checked_ms']=best['created_ms']
        state['last_alert'][best['symbol']]=now
        active[best['symbol']]=best
        save_state(state); print('Telegram sent',best['symbol'])


def main():
    print('SMARTSPOT AI V6 STARTED | KuCoin Spot | no trading')
    state=load_state()
    monitor_active(state)
    scan(state)
    save_state(state)

if __name__=='__main__': main()

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
          
