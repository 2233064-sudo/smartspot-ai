# ============================================================
# ADNANI FUTURES V7.3
# OKX USDT PERPETUAL | SIGNALS ONLY | NO ORDER EXECUTION
# GitHub Actions compatible
# ============================================================

import os
import sys
import time
import math
import json
import traceback
from datetime import datetime, timezone

import ccxt
import pandas as pd


# -------------------- CONFIGURATION --------------------

TOKEN = "8814058749:AAEZnslerRYWElLmZpHLbmYPf1f7CFfPtjU"  # نمونه آزمایشی اعلام‌شده توسط کاربر
CHAT_ID = "1729032800"  # نمونه آزمایشی اعلام‌شده توسط کاربر
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", TOKEN).strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", CHAT_ID).strip()

BOT_NAME = "گروه تخصصی سیگنال فیوچرز مهندس عدنانی"
TIMEFRAME_TREND = "4h"
TIMEFRAME_CONFIRM = "1h"
TIMEFRAME_ENTRY = "15m"

SCAN_LIMIT = 40
MIN_24H_QUOTE_VOLUME = 3_000_000
MIN_SCORE = 85

RISK_PERCENT = 0.5       # informational risk reference per trade
LEVERAGE_REFERENCE = 2   # informational only; bot never sets leverage
ATR_SL_MULTIPLIER = 1.6
MIN_RR = 2.0

COOLDOWN_HOURS = 6
STATE_FILE = "signal_state.txt"  # این فایل در cache موجود GitHub Actions نگهداری می‌شود
TRACK_TIMEFRAME = "1m"
TRACK_CANDLE_LIMIT = 100

# Public market data only. No API key and no order methods are used.
exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 25000,
    "options": {
        "defaultType": "swap",
    },
})


# -------------------- TELEGRAM --------------------

def telegram_send(message):
    if (not BOT_TOKEN or not CHAT_ID or
            BOT_TOKEN == "PASTE_BOT_TOKEN_HERE" or
            CHAT_ID == "PASTE_CHAT_ID_HERE"):
        print("Telegram credentials are not configured.")
        print(message)
        return False

    try:
        import urllib.parse
        import urllib.request

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": "true",
        }).encode("utf-8")

        request = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            result = response.read().decode("utf-8", errors="replace")
            print("Telegram response:", result[:500])
            return response.status == 200
    except Exception as exc:
        print("Telegram send error:", repr(exc))
        return False


# -------------------- INDICATORS --------------------

def add_indicators(df):
    df = df.copy()

    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-12)
    df["rsi"] = 100 - (100 / (1 + rs))

    previous_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["volume_sma"] = df["volume"].rolling(20).mean()

    return df


def fetch_frame(symbol, timeframe, limit=120):
    rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not rows or len(rows) < 60:
        raise RuntimeError(f"Insufficient candles for {symbol} {timeframe}")

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()
    return add_indicators(df)


# -------------------- MARKET UNIVERSE --------------------

def get_watchlist():
    markets = exchange.load_markets()
    candidates = []

    for symbol, market in markets.items():
        if not market.get("active", True):
            continue
        if market.get("swap") is not True:
            continue
        if market.get("quote") != "USDT":
            continue
        if market.get("settle") != "USDT":
            continue
        if market.get("linear") is not True:
            continue
        candidates.append(symbol)

    if not candidates:
        raise RuntimeError(
            "OKX returned no active linear USDT perpetual swap markets."
        )

    tickers = exchange.fetch_tickers(candidates)
    ranked = []

    for symbol in candidates:
        ticker = tickers.get(symbol) or {}
        quote_volume = ticker.get("quoteVolume")
        if quote_volume is None:
            base_volume = ticker.get("baseVolume")
            last = ticker.get("last")
            if base_volume is not None and last is not None:
                quote_volume = float(base_volume) * float(last)

        try:
            quote_volume = float(quote_volume or 0)
        except (TypeError, ValueError):
            quote_volume = 0

        if quote_volume >= MIN_24H_QUOTE_VOLUME:
            ranked.append((symbol, quote_volume))

    ranked.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _ in ranked[:SCAN_LIMIT]]


# -------------------- MARKET DIRECTION --------------------

def market_bias(df):
    last = df.iloc[-2]  # use last fully closed candle
    if last["ema20"] > last["ema50"] and last["close"] > last["ema20"]:
        return "BULL"
    if last["ema20"] < last["ema50"] and last["close"] < last["ema20"]:
        return "BEAR"
    return "NEUTRAL"


def candle_confirmation(row, direction):
    candle_range = max(float(row["high"] - row["low"]), 1e-12)
    body = abs(float(row["close"] - row["open"]))
    body_ratio = body / candle_range

    if direction == "LONG":
        return (
            row["close"] > row["open"]
            and row["close"] > row["high"] - candle_range * 0.35
            and body_ratio >= 0.35
        )

    return (
        row["close"] < row["open"]
        and row["close"] < row["low"] + candle_range * 0.35
        and body_ratio >= 0.35
    )


# -------------------- SIGNAL ANALYSIS --------------------

def analyze_symbol(symbol, btc_bias):
    h4 = fetch_frame(symbol, TIMEFRAME_TREND)
    h1 = fetch_frame(symbol, TIMEFRAME_CONFIRM)
    m15 = fetch_frame(symbol, TIMEFRAME_ENTRY)

    h4_bias = market_bias(h4)
    h1_bias = market_bias(h1)

    if h4_bias == "BULL" and h1_bias == "BULL":
        direction = "LONG"
    elif h4_bias == "BEAR" and h1_bias == "BEAR":
        direction = "SHORT"
    else:
        return None

    # BTC filter: avoid entries against a clear BTC move.
    if btc_bias == "BULL" and direction == "SHORT":
        return None
    if btc_bias == "BEAR" and direction == "LONG":
        return None

    row = m15.iloc[-2]  # last closed 15m candle
    previous = m15.iloc[-3]

    close = float(row["close"])
    atr = float(row["atr"])
    rsi = float(row["rsi"])
    volume_sma = float(row["volume_sma"] or 0)
    volume_ratio = float(row["volume"] / volume_sma) if volume_sma > 0 else 0

    if not all(math.isfinite(x) for x in [close, atr, rsi, volume_ratio]):
        return None
    if atr <= 0:
        return None

    score = 60

    # Entry structure: pullback near EMA20 followed by a closed candle confirmation.
    ema20 = float(row["ema20"])
    distance_atr = abs(close - ema20) / atr

    if distance_atr <= 0.8:
        score += 10
    elif distance_atr <= 1.2:
        score += 5
    else:
        return None

    if candle_confirmation(row, direction):
        score += 10
    else:
        return None

    if direction == "LONG":
        if 45 <= rsi <= 68:
            score += 5
        elif rsi > 75 or rsi < 35:
            return None
        if row["close"] > previous["high"]:
            score += 5
    else:
        if 32 <= rsi <= 55:
            score += 5
        elif rsi < 25 or rsi > 65:
            return None
        if row["close"] < previous["low"]:
            score += 5

    if volume_ratio >= 1.5:
        score += 10
    elif volume_ratio >= 1.15:
        score += 5

    if btc_bias in ("BULL", "BEAR"):
        score += 5

    if score < MIN_SCORE:
        return None

    # Swing-based stop with ATR floor.
    recent = m15.iloc[-8:-1]
    if direction == "LONG":
        swing = float(recent["low"].min())
        stop = min(swing, close - ATR_SL_MULTIPLIER * atr)
        risk_distance = close - stop
        if risk_distance <= 0:
            return None
        tp1 = close + risk_distance * MIN_RR
        tp2 = close + risk_distance * 3.0
    else:
        swing = float(recent["high"].max())
        stop = max(swing, close + ATR_SL_MULTIPLIER * atr)
        risk_distance = stop - close
        if risk_distance <= 0:
            return None
        tp1 = close - risk_distance * MIN_RR
        tp2 = close - risk_distance * 3.0

    if risk_distance / close > 0.04:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": close,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "score": score,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "btc_bias": btc_bias,
        "h4_bias": h4_bias,
        "h1_bias": h1_bias,
        "atr_percent": atr / close * 100,
    }


# -------------------- PERSISTENT STATE + SIGNAL TRACKER --------------------


def load_state():
    """Load cooldowns and open signals; migrate the old V7.2 text state if needed."""
    state = {"cooldowns": {}, "open_signals": {}}
    if not os.path.exists(STATE_FILE):
        return state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            raw = file.read().strip()
        if not raw:
            return state
        if raw.startswith("{"):
            loaded = json.loads(raw)
            state["cooldowns"] = loaded.get("cooldowns", {})
            state["open_signals"] = loaded.get("open_signals", {})
        else:
            # Compatibility with V7.2's symbol|timestamp state format.
            for line in raw.splitlines():
                parts = line.strip().split("|")
                if len(parts) == 2:
                    state["cooldowns"][parts[0]] = float(parts[1])
    except Exception as exc:
        print("State read warning:", repr(exc))
    return state


def save_state(state):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, STATE_FILE)


def cooldown_key(signal):
    return f"{signal['symbol']}:{signal['direction']}"


def is_on_cooldown(signal, state):
    last_time = float(state["cooldowns"].get(cooldown_key(signal), 0))
    return (time.time() - last_time) < COOLDOWN_HOURS * 3600


def mark_sent(signal, state):
    state["cooldowns"][cooldown_key(signal)] = time.time()


def add_open_signal(signal, state):
    signal_id = f"{signal['symbol']}:{signal['direction']}:{int(time.time())}"
    state["open_signals"][signal_id] = {
        **signal,
        "created_at": int(time.time()),
        "last_checked": int(time.time() * 1000),
        "tp1_notified": False,
    }
    return signal_id


def tracker_result_message(signal, result, detail=""):
    label = {
        "TP1": "🎯 هدف اول لمس شد",
        "TP2": "🏆 سیگنال با هدف دوم بسته شد",
        "SL": "🛑 حد ضرر فعال شد",
        "AMBIGUOUS": "⚠️ نتیجه این بازه مبهم است؛ برخوردها در یک کندل رخ داده‌اند",
    }.get(result, "📌 به‌روزرسانی سیگنال")
    direction_fa = "LONG" if signal["direction"] == "LONG" else "SHORT"
    msg = (
        f"{BOT_NAME}\n{label}\n"
        f"قرارداد: {signal['symbol']} | جهت: {direction_fa}\n"
        f"ورود: {fmt(signal['entry'])} | قیمت هدف اول: {fmt(signal['tp1'])}\n"
        f"هدف دوم: {fmt(signal['tp2'])} | حد ضرر: {fmt(signal['stop'])}\n"
        f"نتیجه: {result}\n"
        f"منبع بررسی: OKX، کندل یک‌دقیقه‌ای"
    )
    if detail:
        msg += "\n" + detail
    return msg


def track_open_signals(state):
    """Track each open signal using closed 1-minute candles since last check."""
    finished = []
    for signal_id, signal in list(state["open_signals"].items()):
        try:
            since = max(0, int(signal.get("last_checked", signal["created_at"] * 1000)) - 60_000)
            candles = exchange.fetch_ohlcv(
                signal["symbol"], timeframe=TRACK_TIMEFRAME,
                since=since, limit=TRACK_CANDLE_LIMIT
            )
            if not candles:
                continue

            # Ignore the currently forming candle.
            closed_candles = candles[:-1]
            newest_timestamp = int(candles[-1][0])
            for candle in closed_candles:
                candle_ts, _op, high, low, _close, _vol = candle
                if candle_ts < int(signal["created_at"] * 1000):
                    continue

                if signal["direction"] == "LONG":
                    sl_hit = float(low) <= float(signal["stop"])
                    tp1_hit = float(high) >= float(signal["tp1"])
                    tp2_hit = float(high) >= float(signal["tp2"])
                else:
                    sl_hit = float(high) >= float(signal["stop"])
                    tp1_hit = float(low) <= float(signal["tp1"])
                    tp2_hit = float(low) <= float(signal["tp2"])

                # If stop and target occur in the same 1m candle, order is unknowable.
                if sl_hit and (tp1_hit or tp2_hit):
                    telegram_send(tracker_result_message(signal, "AMBIGUOUS"))
                    finished.append(signal_id)
                    break

                if sl_hit:
                    telegram_send(tracker_result_message(signal, "SL"))
                    finished.append(signal_id)
                    break

                if tp1_hit and not signal.get("tp1_notified", False):
                    if telegram_send(tracker_result_message(signal, "TP1")):
                        signal["tp1_notified"] = True

                if tp2_hit:
                    if tp1_hit and not signal.get("tp1_notified", False):
                        telegram_send(tracker_result_message(signal, "TP1"))
                    telegram_send(tracker_result_message(signal, "TP2"))
                    finished.append(signal_id)
                    break

            signal["last_checked"] = newest_timestamp
        except Exception as exc:
            print(f"Tracker error for {signal.get('symbol')}: {type(exc).__name__}: {exc}")

    for signal_id in finished:
        state["open_signals"].pop(signal_id, None)


# -------------------- FORMATTING --------------------

def fmt(value):
    value = float(value)
    if value >= 1000:
        return f"{value:,.3f}"
    if value >= 1:
        return f"{value:.5f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def signal_message(signal):
    direction_fa = "خرید (LONG)" if signal["direction"] == "LONG" else "فروش (SHORT)"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"🚨 {BOT_NAME}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 قرارداد: {signal['symbol']}\n"
        f"🧭 جهت: {direction_fa}\n"
        f"🏦 منبع داده: OKX USDT Perpetual\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 ورود: {fmt(signal['entry'])}\n"
        f"🛑 حد ضرر: {fmt(signal['stop'])}\n"
        f"🥇 هدف اول (2R): {fmt(signal['tp1'])}\n"
        f"🥈 هدف دوم (3R): {fmt(signal['tp2'])}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⭐ امتیاز شرایط: {signal['score']}/100\n"
        f"📈 RSI: {signal['rsi']:.1f}\n"
        f"📦 حجم نسبی: {signal['volume_ratio']:.2f}x\n"
        f"₿ وضعیت BTC: {signal['btc_bias']}\n"
        f"📐 ATR: {signal['atr_percent']:.2f}%\n"
        f"⚠️ ریسک مرجع: {RISK_PERCENT}% از سرمایه، نه تضمین نتیجه\n"
        f"⚙️ اهرم مرجع: {LEVERAGE_REFERENCE}x، تنظیم یا اجرا نمی‌شود\n"
        f"🕒 زمان: {now}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ سیگنال تحلیلی است؛ سفارش خودکار ثبت نمی‌شود. "
        f"قبل از معامله، قیمت و شرایط قرارداد را بررسی کنید."
    )


# -------------------- MAIN SCANNER --------------------

def main():
    print("Starting Adnani Futures V7.3 scan...")
    print("Data source: OKX USDT perpetual public market data")

    try:
        exchange.load_markets()
        btc_symbol = "BTC/USDT:USDT"
        btc_h4 = fetch_frame(btc_symbol, TIMEFRAME_TREND)
        btc_h1 = fetch_frame(btc_symbol, TIMEFRAME_CONFIRM)

        btc_h4_bias = market_bias(btc_h4)
        btc_h1_bias = market_bias(btc_h1)
        btc_bias = btc_h4_bias if btc_h4_bias == btc_h1_bias else "NEUTRAL"

        watchlist = get_watchlist()
        print(f"Watchlist loaded: {len(watchlist)} symbols")
        print(f"BTC bias: {btc_bias}")

    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)}"
        print("Market data error:", detail)
        telegram_send(
            f"{BOT_NAME}\n"
            f"❌ دریافت اطلاعات بازار ناموفق بود.\n"
            f"منبع: OKX USDT Perpetual\n"
            f"جزئیات: {detail[:900]}"
        )
        return

    state = load_state()
    # Follow previously issued signals before looking for new setups.
    track_open_signals(state)
    signals_found = 0
    checked = 0
    errors = []

    for symbol in watchlist:
        try:
            checked += 1
            signal = analyze_symbol(symbol, btc_bias)
            if signal is None:
                continue
            if is_on_cooldown(signal, state):
                continue

            if telegram_send(signal_message(signal)):
                mark_sent(signal, state)
                add_open_signal(signal, state)
                signals_found += 1
                save_state(state)

            time.sleep(0.25)

        except Exception as exc:
            error_text = f"{symbol}: {type(exc).__name__}: {str(exc)}"
            print("Symbol scan error:", error_text)
            if len(errors) < 5:
                errors.append(error_text)
            time.sleep(0.2)

    save_state(state)

    summary = (
        f"📋 گزارش اسکن {BOT_NAME}\n"
        f"منبع داده: OKX USDT Perpetual\n"
        f"قراردادهای بررسی‌شده: {checked}\n"
        f"سیگنال‌های جدید ارسال‌شده: {signals_found}\n"
        f"سیگنال‌های باز تحت پیگیری: {len(state['open_signals'])}\n"
        f"وضعیت BTC: {btc_bias}\n"
        f"حداقل امتیاز: {MIN_SCORE}/100\n"
        f"زمان: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"⚠️ این گزارش به معنی تضمین سود یا اعتبارسنجی بک‌تست نیست."
    )

    if errors:
        summary += "\n\nنمونه خطاهای نمادها:\n" + "\n".join(errors)

    telegram_send(summary)
    print("Scan completed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        detail = traceback.format_exc()
        print(detail)
        telegram_send(
            f"{BOT_NAME}\n"
            f"❌ خطای پیش‌بینی‌نشده:\n{detail[-2500:]}"
        )
        sys.exit(1)
