import ccxt
import pandas as pd
import numpy as np
import requests
import schedule
import time
from datetime import datetime, timezone, date

# ── Credentials ────────────────────────────────────────────────────────────────
BYBIT_API_KEY    = "FOqGNCN6gRxu4bqMqF"
BYBIT_API_SECRET = "YmSWYNkQbVXYiFU5v0G3y3R405VLREGu7icy"
TELEGRAM_BOT_TOKEN = "8734785957:AAGzU-KPRY4mzXARxyTpLSHGemFtJ7AEsUQ"
TELEGRAM_CHAT_ID   = "1932328527"
REPLIT_WEBHOOK_URL = "https://dcf37de3-95b1-4275-aad3-54160dffeae5-00-1dydpq6kaysyl.riker.replit.dev/api/webhook/trade"

# ── v7.2 PATH D-PLUS Champion Config ───────────────────────────────────────────
CURRENT_PHASE     = 1
DAILY_KILL_SWITCH = -150.0
DAILY_PROFIT_LOCK = +9999.0
MAX_CONCURRENT    = 999
FEE_CAP_FRAC_G2   = 0.40    # 🏆 Champion value
FEE_CAP_FRAC_G1   = 0.55    # 🔧 G1 FIX: bumped from 0.40 so ETH/SOL/LTC/LINK can trade in low-vol periods
MAX_NOTIONAL_USD  = 10000.0  # 🛡️ Bybit position-limit safety (prevents error 110090)

HOUSE_MONEY_THRESHOLD  = 75.0
HOUSE_MONEY_MULTIPLIER = 1.5

# ── Per-Symbol Config (🏆 ORIGINAL v7.2 PATH D-PLUS CHAMPION VALUES) ──────────
PER_SYMBOL_CONFIG = {
    # ── Group 1: SL=0.50× | TP=3.00× | Trail=0.10× | P1=$30 | P2=$20 ─────────
    'ETH/USDT:USDT':    (0.50, 3.00, 0.10, 30.0, 20.0),
    'XRP/USDT:USDT':    (0.50, 3.00, 0.10, 30.0, 20.0),
    'OP/USDT:USDT':     (0.50, 3.00, 0.10, 30.0, 20.0),
    'SOL/USDT:USDT':    (0.50, 3.00, 0.10, 30.0, 20.0),
    'ONDO/USDT:USDT':   (0.50, 3.00, 0.10, 30.0, 20.0),
    'ZEC/USDT:USDT':    (0.50, 3.00, 0.10, 30.0, 20.0),
    'LTC/USDT:USDT':    (0.50, 3.00, 0.10, 30.0, 20.0),
    'ADA/USDT:USDT':    (0.50, 3.00, 0.10, 30.0, 20.0),
    'LINK/USDT:USDT':   (0.50, 3.00, 0.10, 30.0, 20.0),
    # ── Group 2: SL=0.45× | TP=4.00× | Trail=0.10× | P1=$35 | P2=$25 ─────────
    'RIVER/USDT:USDT':    (0.45, 4.00, 0.10, 35.0, 25.0),
    'RENDER/USDT:USDT':   (0.45, 4.00, 0.10, 35.0, 25.0),
    '1000PEPE/USDT:USDT': (0.45, 4.00, 0.10, 35.0, 25.0),
    'AVAX/USDT:USDT':     (0.45, 4.00, 0.10, 35.0, 25.0),
    'INJ/USDT:USDT':      (0.45, 4.00, 0.10, 35.0, 25.0),
    'JUP/USDT:USDT':      (0.45, 4.00, 0.10, 35.0, 25.0),
    'PIPPIN/USDT:USDT':   (0.45, 4.00, 0.10, 35.0, 25.0),
    'POL/USDT:USDT':      (0.45, 4.00, 0.10, 35.0, 25.0),
    'DASH/USDT:USDT':     (0.45, 4.00, 0.10, 35.0, 25.0),
    'SUI/USDT:USDT':      (0.45, 4.00, 0.10, 35.0, 25.0),
}

GROUP1_SYMBOLS = {'ETH/USDT:USDT','XRP/USDT:USDT','OP/USDT:USDT','SOL/USDT:USDT','ONDO/USDT:USDT','ZEC/USDT:USDT','LTC/USDT:USDT','ADA/USDT:USDT','LINK/USDT:USDT'}
SYMBOLS = list(PER_SYMBOL_CONFIG.keys())

# ── AlgoAlpha Constants (v7.2 Champion: ST=2/14, WMA=14, EMA=3) ───────────────
ST_FACTOR  = 2.0
ST_PERIOD  = 14
WMA_LENGTH = 14
EMA_LENGTH = 3
ATR_PERIOD = 14
BYBIT_TAKER_FEE = 0.00055

# ── Runtime State ──────────────────────────────────────────────────────────────
open_positions       = {}
daily_pnl_tracker    = {}

# ── Exchange ───────────────────────────────────────────────────────────────────
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.enable_demo_trading(True)
exchange.load_markets()

# ── Helpers ───────────────────────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text.strip(), 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e: print(f"  [Telegram error] {e}")

def send_webhook(data):
    if not REPLIT_WEBHOOK_URL: return
    try: requests.post(REPLIT_WEBHOOK_URL, json=data, timeout=5)
    except Exception as e: print(f"  [Webhook error] {e}")

def is_kill_switch_active() -> bool:
    return daily_pnl_tracker.get(date.today(), 0.0) <= DAILY_KILL_SWITCH

def record_closed_pnl(pnl_usd: float):
    today = date.today()
    daily_pnl_tracker[today] = daily_pnl_tracker.get(today, 0.0) + pnl_usd

# ── Indicator Math ─────────────────────────────────────────────────────────────
def fetch_data(symbol, timeframe='15m', limit=300):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['ts','open','high','low','close','volume'])
        for c in ['open','high','low','close']: df[c] = df[c].astype(float)
        return df
    except: return None

def rma(s, n): return s.ewm(alpha=1/n, adjust=False).mean()

def calc_atr(df, n):
    pc = df['close'].shift(1)
    tr = pd.concat([df['high']-df['low'], (df['high']-pc).abs(), (df['low']-pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)

def calc_wma(s, n):
    w = np.arange(1, n+1)
    return s.rolling(n).apply(lambda x: np.dot(x, w)/w.sum(), raw=True)

def algoalpha_baseline(df):
    a = calc_atr(df, ST_PERIOD); hl2 = (df['high']+df['low'])/2
    cl = df['close'].values; up = np.zeros(len(df)); lo = np.zeros(len(df))
    up[0] = hl2.iloc[0] + ST_FACTOR*a.iloc[0]
    lo[0] = hl2.iloc[0] - ST_FACTOR*a.iloc[0]
    for i in range(1, len(df)):
        bu = hl2.iloc[i] + ST_FACTOR*a.iloc[i]
        bl = hl2.iloc[i] - ST_FACTOR*a.iloc[i]
        lo[i] = bl if (bl > lo[i-1] or cl[i-1] < lo[i-1]) else lo[i-1]
        up[i] = bu if (bu < up[i-1] or cl[i-1] > up[i-1]) else up[i-1]
    mid = (pd.Series(lo, index=df.index)+pd.Series(up, index=df.index))/2
    return calc_wma(mid, WMA_LENGTH).ewm(span=EMA_LENGTH, adjust=False).mean()

def calc_smc_structure(df):
    r, w = 3, 7
    df['last_swing_high'] = pd.Series(np.where(df['high'].shift(r)==df['high'].rolling(w).max(), df['high'].shift(r), np.nan)).ffill()
    df['last_swing_low']  = pd.Series(np.where(df['low'].shift(r) ==df['low'].rolling(w).min(),  df['low'].shift(r),  np.nan)).ffill()
    t = pd.Series(np.nan, index=df.index)
    t.loc[df['close'] > df['last_swing_high']] = 1
    t.loc[df['close'] < df['last_swing_low']]  = -1
    df['smc_trend'] = t.ffill().fillna(0)
    return df

# ── Execution ──────────────────────────────────────────────────────────────────
def calculate_lot_size(entry, sl, risk_usd):
    d = abs(entry - sl)
    if d == 0: return 0
    size = risk_usd / d
    if size * entry > MAX_NOTIONAL_USD:
        size = MAX_NOTIONAL_USD / entry
    return size

def execute_trade(symbol, direction, size, entry, sl, tp):
    side = 'buy' if direction == 'LONG' else 'sell'
    try:
        fmt_size = float(exchange.amount_to_precision(symbol, size))
        fmt_sl, fmt_tp = float(exchange.price_to_precision(symbol, sl)), float(exchange.price_to_precision(symbol, tp))
        order = exchange.create_order(symbol=symbol, type='market', side=side, amount=fmt_size,
            params={'stopLoss': str(fmt_sl), 'takeProfit': str(fmt_tp), 'tpslMode': 'Full',
                    'slOrderType':'Market','tpOrderType':'Market','slTriggerBy':'LastPrice','tpTriggerBy':'LastPrice'})
        return order, fmt_size, fmt_sl, fmt_tp
    except Exception as e:
        send_telegram(f"❌ Bybit Execution Failed ({symbol}): {e}")
        return None, None, None, None

def modify_bybit_tpsl(symbol, direction, new_sl, current_tp):
    try:
        market_id = exchange.market(symbol)['id']
        bybit_side = 'Buy' if direction == 'LONG' else 'Sell'
        fmt_sl = float(exchange.price_to_precision(symbol, new_sl))
        exchange.privatePostV5PositionTradingStop({'category':'linear','symbol':market_id,'side':bybit_side,'tpslMode':'Full',
            'takeProfit':str(current_tp),'stopLoss':str(fmt_sl),'slOrderType':'Market','tpOrderType':'Market',
            'slTriggerBy':'LastPrice','tpTriggerBy':'LastPrice'})
        return fmt_sl
    except: return None

# ── Position Management ────────────────────────────────────────────────────────
def sync_open_positions():
    if not open_positions: return
    try:
        live = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live if float(p.get('contracts', 0)) > 0}
        for sym in [s for s in list(open_positions.keys()) if s not in live_syms]:
            pos = open_positions.pop(sym)
            res = exchange.private_get_v5_position_closed_pnl({'category':'linear','symbol':exchange.market(sym)['id'],'limit':1})
            recs = res.get('result',{}).get('list',[])
            if recs:
                pnl = float(recs[0].get('closedPnl', 0.0))
                record_closed_pnl(pnl)
                outcome = "WIN" if pnl > 0.5 else "LOSS" if pnl < -0.5 else "BREAKEVEN"
                send_webhook({"timestamp": datetime.now(timezone.utc).isoformat(), "asset": sym, "direction": pos['direction'],
                              "entry": float(pos['entry']), "outcome": outcome, "strategy": "Liquidity Sweep", "pnl": pnl})
                send_telegram(f"{'✅' if pnl > 0 else '❌'} <b>TRADE CLOSED — {sym.split('/')[0]}</b>\nSettled Net PnL: <code>${pnl:.2f}</code>")
    except Exception as e: print(f"sync error: {e}")

def manage_trailing_stops():
    if not open_positions: return
    for symbol, pos in list(open_positions.items()):
        df = fetch_data(symbol, '1m', 5)
        if df is None or len(df) < 1: continue
        bar = df.iloc[-1]
        is_long = pos['direction'] == 'LONG'
        new_best = max(pos['best_price'], float(bar['high'])) if is_long else min(pos['best_price'], float(bar['low']))
        pos['best_price'] = new_best
        prof = abs(new_best - pos['entry'])
        if prof >= (1.0 * pos['atr']) and not pos.get('free_ride_triggered', False):
            pos['free_ride_triggered'] = True
            send_telegram(f"🛡️ <b>FREE RIDE SECURED — {symbol.split('/')[0]}</b>\nTrade reached +1.0× ATR.")
        if prof < (1.0 * pos['atr']): continue
        raw = (new_best - pos['trail_mult']*pos['atr']) if is_long else (new_best + pos['trail_mult']*pos['atr'])
        if (is_long and raw > pos['current_sl']) or (not is_long and raw < pos['current_sl']):
            if (is_long and raw < float(bar['close'])) or (not is_long and raw > float(bar['close'])):
                fmt = modify_bybit_tpsl(symbol, pos['direction'], raw, pos['catastrophic_tp'])
                if fmt:
                    pos['current_sl'] = fmt
                    send_telegram(f"<b>🔄 Trail Updated — {symbol.split('/')[0]}</b>\n{'▲ LONG' if is_long else '▼ SHORT'}\nNew SL: <code>{fmt}</code>")

def fast_management():
    sync_open_positions()
    manage_trailing_stops()

# ── Signal Engine ──────────────────────────────────────────────────────────────
def check_signal():
    today_pnl = daily_pnl_tracker.get(date.today(), 0.0)
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M')}] PnL: ${today_pnl:.2f} | Open: {len(open_positions)}", flush=True)
    if is_kill_switch_active(): return
    sync_open_positions()
    for symbol in SYMBOLS:
        if len(open_positions) >= MAX_CONCURRENT or symbol in open_positions: continue
        df = fetch_data(symbol, '15m', 250)
        if df is None or len(df) < 150: continue
        df['atr_14'], df['tL'] = calc_atr(df, ATR_PERIOD), algoalpha_baseline(df)
        df = calc_smc_structure(df)
        c = df.iloc[-2]
        price = float(df.iloc[-1]['open'])     # ← entry at next-bar open (matches Pine)
        atr = float(df.iloc[-2]['atr_14'])
        smc_trend = int(df.iloc[-2]['smc_trend'])
        if atr < (price * 0.00005) or np.isnan(atr): continue
        algo_long  = (df['tL'].iloc[-2] > df['tL'].iloc[-3]) and (df['tL'].iloc[-3] <= df['tL'].iloc[-4])
        algo_short = (df['tL'].iloc[-2] < df['tL'].iloc[-3]) and (df['tL'].iloc[-3] >= df['tL'].iloc[-4])
        l_sig = algo_short and (smc_trend == -1)   # INVERTED Liquidity Sweep
        s_sig = algo_long  and (smc_trend == 1)
        if not l_sig and not s_sig: continue

        sl_m, tp_m, tr_m, p1, p2 = PER_SYMBOL_CONFIG[symbol]
        base_risk = p1 if CURRENT_PHASE == 1 else p2
        risk = base_risk * (HOUSE_MONEY_MULTIPLIER if today_pnl >= HOUSE_MONEY_THRESHOLD else 1.0)

        raw_sl = price - sl_m*atr if l_sig else price + sl_m*atr
        size = calculate_lot_size(price, raw_sl, risk)
        if size == 0: continue

        # 🔧 PER-GROUP FEE CAP (G1 = 0.55, G2 = 0.40)
        fee_cap = FEE_CAP_FRAC_G1 if symbol in GROUP1_SYMBOLS else FEE_CAP_FRAC_G2
        est_fee = size * price * BYBIT_TAKER_FEE * 2
        if est_fee > (risk * fee_cap):
            continue

        cat_tp = price + (10*atr if l_sig else -10*atr)
        order, f_size, f_sl, f_tp = execute_trade(symbol, 'LONG' if l_sig else 'SHORT', size, price, raw_sl, cat_tp)
        if order:
            grp = "G1" if symbol in GROUP1_SYMBOLS else "G2"
            open_positions[symbol] = {'direction':'LONG' if l_sig else 'SHORT','entry':price,'atr':atr,
                                      'best_price':price,'current_sl':f_sl,'catastrophic_tp':f_tp,
                                      'trail_mult':tr_m,'tp_mult':tp_m,'size':f_size}
            arrow = "🟢 ▲ LONG" if l_sig else "🔴 ▼ SHORT"
            trap = "🪤 BEARISH (Capitulation)" if l_sig else "🪤 BULLISH (Greed Trap)"
            msg = (f"<b>🤯 APEX v7.2 LIQUIDITY SWEEP</b>\n{arrow} <b>{symbol.split('/')[0]}</b>  [{grp} | Phase {CURRENT_PHASE}]\n\n"
                   f"Entry   : <code>{price:.6f}</code>\nSL      : <code>{f_sl}</code>  ({sl_m}× ATR)\n"
                   f"Target  : Uncapped 🚀 ({tp_m}×)\nSMC Trap: {trap}\nSize    : <code>{f_size}</code>\n"
                   f"Est. Fee: ~${est_fee:.2f}\nRisk    : ${risk:.0f}\nOpen    : {len(open_positions)}/{MAX_CONCURRENT}")
            send_telegram(msg)

def daily_reset():
    yesterday = daily_pnl_tracker.get(date.today(), 0.0)
    daily_pnl_tracker.clear()
    ks = "🛑 TRIGGERED" if yesterday <= DAILY_KILL_SWITCH else "✅ NOT triggered"
    send_telegram(f"📅 <b>Daily Reset</b>\nYesterday PnL: <code>${yesterday:.2f}</code>\nKill-switch was {ks}")

if __name__ == '__main__':
    send_telegram("<b>🤯 APEX v7.2 PATH D-PLUS CHAMPION — LIVE</b>\n19 syms | SL 0.50/0.45 | TP 3.0/4.0\nFee cap: G1=55% / G2=40%")
    schedule.every(1).minutes.do(fast_management)
    schedule.every(5).minutes.at(":00").do(check_signal)
    schedule.every().day.at("00:05").do(daily_reset)
    while True:
        schedule.run_pending()
        time.sleep(1)
