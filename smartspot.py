# ==============================================================
# SMARTSPOT AI V6 - conservative spot setup scanner
# KuCoin Spot | GitHub Actions | Telegram | NO auto-trading
# ==============================================================
import os, json, time, math
from datetime import datetime, timezone
import ccxt
import pandas as pd
import requests

TOKEN = os.getenv('TELEGRAM_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
STATE_FILE = 'signal_state.json'
COOLDOWN_SECONDS = 8 * 3600
MAX_ACTIVE_SIGNALS = 8
SIGNAL_EXPIRY_HOURS = 48
MIN_SCORE = 78

WATCHLIST = [
    'ETHFI','DASH','ZEC','RENDER','TAO','NEAR','UNI','STX','BOME','ENA','ZRO','QNT','XMR','TUT','PUMP',
    'PYTH','SUI','APT','ONDO','GMX','TIA','WLD','GIGGLE','ARIA','OP','HYPE','VIRTUAL','ARB','BCH','AVNT',
    'AVAX','ADA','JUP','KAS','SOL','DOGE','CAKE','ETC','GRAM','ASTER','ETH','SHIB','DRIFT','BTC','LDO',
    'FIL','HEI','HEMI','WIF','ALLO','SEI','HBAR','BONK','CLANKER','ACE','PEPE','FET','PAXG','XAUT','ATOM',
    'TRX','ORDER','CC','WLFI','ICP','HMSTR','AIO','Q','LUNC','AIN'
]
exchange = ccxt.kucoin({'enableRateLimit': True, 'timeout': 25000})


def utcnow():
    return datetime.now(timezone.utc)


def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        if not isinstance(state, dict): raise ValueError('state is not dict')
        state.setdefault('last_alert', {})
        state.setdefault('active_signals', {})
        state.setdefault('closed_signals', [])
        return state
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {'last_alert': {}, 'active_signals': {}, 'closed_signals': []}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def telegram(message):
    if not TOKEN or not CHAT_ID:
        print('Telegram credentials missing')
        return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                          data={'chat_id': CHAT_ID, 'text': message}, timeout=20)
        r.raise_for_status()
        return bool(r.json().get('ok'))
    except Exception as exc:
        print('Telegram error:', exc)
        return False


def get_symbols():
    markets = exchange.load_markets()
    out = []
    for base in WATCHLIST:
        sym = base + '/USDT'
        m = markets.get(sym)
        if m and m.get('active', True) and m.get('spot', True): out.append(sym)
    return list(dict.fromkeys(out))


def fetch_df(symbol, timeframe, limit=220):
    try:
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(rows, columns=['time','open','high','low','close','volume'])
        if len(df) < 80: return None
        # Exclude currently forming candle for stable signals
        df = df.iloc[:-1].copy()
        df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True)
        for c in ['open','high','low','close','volume']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna().reset_index(drop=True)
        return df if len(df) >= 60 else None
    except Exception as e:
        print('Data error', symbol, timeframe, e)
        return None


def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def atr(df, period=14):
    prev = df['close'].shift(1)
    tr = pd.concat([(df['high']-df['low']), (df['high']-prev).abs(), (df['low']-prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def rsi(s, period=14):
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))


def swing_points(df, left=3, right=3):
    # Confirmed local pivots only; right-side bars mean these are not forward-looking at evaluation time.
    highs, lows = [], []
    for i in range(left, len(df)-right):
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]
        if h >= df['high'].iloc[i-left:i+right+1].max() and (df['high'].iloc[i-left:i+right+1] == h).sum() == 1:
            highs.append((i, float(h)))
        if l <= df['low'].iloc[i-left:i+right+1].min() and (df['low'].iloc[i-left:i+right+1] == l).sum() == 1:
            lows.append((i, float(l)))
    return highs, lows


def bullish_structure(df):
    highs, lows = swing_points(df)
    if len(highs) < 2 or len(lows) < 2: return False, 'Insufficient confirmed swings'
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    return (hh and hl), ('HH + HL' if hh and hl else 'Structure not HH/HL')


def candle_confirmation(df):
    p, c = df.iloc[-2], df.iloc[-1]
    body = abs(c['close'] - c['open'])
    rng = max(c['high'] - c['low'], 1e-12)
    lower = min(c['open'], c['close']) - c['low']
    upper = c['high'] - max(c['open'], c['close'])
    engulf = (p['close'] < p['open'] and c['close'] > c['open'] and c['open'] <= p['close'] and c['close'] >= p['open'])
    pin = (c['close'] > c['open'] and lower >= max(body*2, rng*0.35) and lower > upper)
    return bool(engulf or pin), ('Bullish engulfing' if engulf else 'Bullish rejection pinbar' if pin else 'No confirmation')


def analyze(symbol):
    h4, h1, m15 = fetch_df(symbol, '4h', 160), fetch_df(symbol, '1h', 180), fetch_df(symbol, '15m', 180)
    btc4 = fetch_df('BTC/USDT', '4h', 160)
    if any(x is None for x in [h4,h1,m15,btc4]): return None
    price = float(m15.close.iloc[-1])
    # Higher-timeframe trend filters
    h4e20, h4e50 = ema(h4.close,20), ema(h4.close,50)
    h1e20, h1e50 = ema(h1.close,20), ema(h1.close,50)
    btc_e20, btc_e50 = ema(btc4.close,20), ema(btc4.close,50)
    trend4 = h4.close.iloc[-1] > h4e20.iloc[-1] > h4e50.iloc[-1]
    trend1 = h1.close.iloc[-1] > h1e20.iloc[-1] > h1e50.iloc[-1]
    btc_ok = btc4.close.iloc[-1] > btc_e50.iloc[-1] or btc_e20.iloc[-1] > btc_e50.iloc[-1]
    structure, struct_label = bullish_structure(h1)
    atrv = float(atr(m15,14).iloc[-1])
    if not math.isfinite(atrv) or atrv <= 0: return None
    atr_pct = atrv / price * 100
    if atr_pct < 0.15 or atr_pct > 6.0: return None
    # Recent range-based retracement proxy: use recent 1h high/low and avoid claiming exact swing fib.
    recent = h1.iloc[-60:]
    swing_low, swing_high = float(recent.low.min()), float(recent.high.max())
    if swing_high <= swing_low: return None
    rng = swing_high - swing_low
    fib50, fib618 = swing_high-rng*.50, swing_high-rng*.618
    zone_lo, zone_hi = sorted([fib50, fib618])
    # Allow a modest ATR buffer around retracement zone
    in_zone = zone_lo - atrv*0.35 <= price <= zone_hi + atrv*0.35
    confirm, candle_label = candle_confirmation(m15)
    vol_avg = float(m15.volume.iloc[-21:-1].mean())
    relvol = float(m15.volume.iloc[-1] / vol_avg) if vol_avg > 0 else 0
    # score is rule-based setup quality, NOT probability of profit
    score = 0; reasons=[]; details=[]
    if trend4: score += 15; reasons.append('4H trend aligned')
    else: details.append('4H trend not aligned')
    if trend1: score += 15; reasons.append('1H trend aligned')
    else: details.append('1H trend not aligned')
    if structure: score += 20; reasons.append('Confirmed HH/HL structure')
    else: details.append(struct_label)
    if in_zone: score += 15; reasons.append('Retracement zone proximity')
    else: details.append('Outside retracement zone')
    if confirm: score += 10; reasons.append(candle_label)
    else: details.append(candle_label)
    if relvol >= 1.2: score += 10; reasons.append(f'Relative volume {relvol:.2f}x')
    elif relvol >= .8: score += 5; reasons.append(f'Moderate relative volume {relvol:.2f}x')
    else: details.append(f'Low relative volume {relvol:.2f}x')
    if btc_ok: score += 10; reasons.append('BTC regime filter passed')
    else: details.append('BTC regime weak')
    rsi_val = float(rsi(m15.close,14).iloc[-1])
    if 48 <= rsi_val <= 68: score += 5; reasons.append(f'RSI balanced ({rsi_val:.1f})')
    else: details.append(f'RSI outside preferred range ({rsi_val:.1f})')
    if score < MIN_SCORE or not (trend4 and trend1 and structure and in_zone and confirm and btc_ok): return None
    # Entry is close of last completed candle. Structure/ATR-based protective stop.
    entry = price
    lows = [v for _,v in swing_points(m15)[1][-3:]]
    pivot_low = min(lows) if lows else float(m15.low.iloc[-10:].min())
    sl = min(pivot_low - atrv*0.25, entry - atrv*1.5)
    if sl >= entry: return None
    risk = entry-sl
    # Filter excessively wide risk and minimum RR to nearby range high
    risk_pct = risk/entry*100
    if risk_pct > 8 or risk_pct < .35: return None
    tp1, tp2, tp3 = entry+risk*1.5, entry+risk*2.5, entry+risk*3.5
    # Use fee assumption on round-trip net R estimate (user can adapt fee rate)
    fee_rate = 0.001
    net_rr1 = ((tp1-entry) - fee_rate*(entry+tp1)) / ((entry-sl) + fee_rate*(entry+sl))
    if net_rr1 < 1.2: return None
    return {'symbol':symbol,'score':score,'entry':entry,'sl':sl,'tp1':tp1,'tp2':tp2,'tp3':tp3,
            'risk_pct':risk_pct,'atr_pct':atr_pct,'relvol':relvol,'rsi':rsi_val,'net_rr1':net_rr1,
            'fib50':fib50,'fib618':fib618,'reasons':reasons,'candle_time':str(m15.time.iloc[-1]),
            'created':utcnow().isoformat(),'status':'ACTIVE'}


def fmt(x): return f'{x:.8g}'


def signal_message(s):
    lines = [
        '━━━━━━━━━━━━━━━━━━━━','🧠 SMARTSPOT AI V6','🟢 SPOT SETUP | BUY','━━━━━━━━━━━━━━━━━━━━',
        f"📌 {s['symbol']}",f"🧪 Setup Quality: {s['score']}/100 (NOT win probability)",
        f"📊 Relative Volume: {s['relvol']:.2f}x",f"📈 RSI(14): {s['rsi']:.1f}",f"🌡 ATR(14): {s['atr_pct']:.2f}%",
        '━━━━━━━━━━━━━━━━━━━━','💰 ENTRY (reference)',fmt(s['entry']),'','🛑 STOP LOSS',fmt(s['sl']),
        '━━━━━━━━━━━━━━━━━━━━','🎯 TP1 | 1.5R',fmt(s['tp1']),'🎯 TP2 | 2.5R',fmt(s['tp2']),
        '🎯 TP3 | 3.5R',fmt(s['tp3']),'','📐 Retracement reference (1H)',
        f"50%: {fmt(s['fib50'])}",f"61.8%: {fmt(s['fib618'])}",'','🧭 FILTERS PASSED'
    ]
    lines += ['• '+r for r in s['reasons']]
    lines += ['','⚖️ Risk estimate: '+f"{s['risk_pct']:.2f}% price distance",
              f"🧮 Estimated net R/R to TP1 (fee assumption): {s['net_rr1']:.2f}R",
              '⏳ Entry is NOT executed or guaranteed. Check price and liquidity before acting.',
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
          
