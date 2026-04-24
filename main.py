import ccxt
import pandas as pd
import numpy as np
import requests
import schedule
import time
from datetime import datetime, timezone, date

# ── Credentials & Config (RESTORED V7.2 KEYS) ──────────────────────────────────
BYBIT_API_KEY    = "FOqGNCN6gRxu4bqMqF"
BYBIT_API_SECRET = "YmSWYNkQbVXYiFU5v0G3y3R405VLREGu7icy"

TELEGRAM_BOT_TOKEN = "8734785957:AAGzU-KPRY4mzXARxyTpLSHGemFtJ7AEsUQ"
TELEGRAM_CHAT_ID   = "1932328527"

# ── Replit Webhook Config ──────────────────────────────────────────────────────
REPLIT_WEBHOOK_URL = "https://dcf37de3-95b1-4275-aad3-54160dffeae5-00-1dydpq6kaysyl.riker.replit.dev/api/webhook/trade" 

CURRENT_PHASE     = 1        
DAILY_KILL_SWITCH = -150.0   
DAILY_PROFIT_LOCK = +9999.0  
MAX_CONCURRENT    = 999
FEE_CAP_FRAC      = 0.15     # 🎯 PATCHED: Max 15% of risk allowed for fees
MAX_NOTIONAL_USD  = 10000.0  # 🛡️ PATCHED: Prevents "Position Limit Exceeded" Error 110090

# 🔥 HOUSE MONEY CONFIGURATION
HOUSE_MONEY_THRESHOLD  = 75.0  
HOUSE_MONEY_MULTIPLIER = 1.5   

# ── Per-Symbol Config (🔄 INVERTED STRATEGY MULTIPLIERS) ──────────────────────
PER_SYMBOL_CONFIG = {
    # ── Group 1: Risk synced to $35 to match Group 2 ─────────────────────────
    'ETH/USDT:USDT':    (1.00, 3.00, 0.10, 35.0, 25.0),
    'XRP/USDT:USDT':    (1.00, 3.00, 0.10, 35.0, 25.0),  
    'OP/USDT:USDT':     (1.00, 3.00, 0.10, 35.0, 25.0),
    'SOL/USDT:USDT':    (2.00, 3.00, 0.10, 35.0, 25.0),
    'ONDO/USDT:USDT':   (1.00, 3.00, 0.10, 35.0, 25.0),
    'ZEC/USDT:USDT':    (2.00, 3.00, 0.10, 35.0, 25.0),
    'LTC/USDT:USDT':    (2.00, 3.00, 0.10, 35.0, 25.0),
    'ADA/USDT:USDT':    (1.00, 3.00, 0.10, 35.0, 25.0),
    'LINK/USDT:USDT':   (1.00, 3.00, 0.10, 35.0, 25.0),
    # ── Group 2: 0.45× ATR ───────────────────────────────────────────────────
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
    'JASMY/USDT:USDT':    (0.45, 4.00, 0.10, 35.0, 25.0),
}

SYMBOLS = list(PER_SYMBOL_CONFIG.keys())

# ── AlgoAlpha Constants ────────────────────────────────────────────────────────
ST_FACTOR  = 2.0    
ST_PERIOD  = 14     
WMA_LENGTH = 14     
EMA_LENGTH = 3      
ATR_PERIOD = 14     
BYBIT_TAKER_FEE = 0.00055  

# ── Runtime State ──────────────────────────────────────────────────────────────
open_positions       = {}
daily_pnl_tracker    = {}
profit_lock_notified = {}  
last_trade_bar       = {}  

# ── Exchange Setup ─────────────────────────────────────────────────────────────
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
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        return df
    except Exception as e: return None

def rma(series, length): return series.ewm(alpha=1/length, adjust=False).mean()

def calc_atr(df, length):
    prev_close = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - prev_close).abs(), (df['low'] - prev_close).abs()], axis=1).max(axis=1)
    return rma(tr, length)

def calc_wma(series, length):
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def algoalpha_baseline(df):
    st_atr = calc_atr(df, ST_PERIOD)
    hl2 = (df['high'] + df['low']) / 2
    upper, lower, close = np.zeros(len(df)), np.zeros(len(df)), df['close'].values
    upper[0], lower[0] = hl2.iloc[0] + ST_FACTOR * st_atr.iloc[0], hl2.iloc[0] - ST_FACTOR * st_atr.iloc[0]
    for i in range(1, len(df)):
        lower[i] = (hl2.iloc[i] - ST_FACTOR * st_atr.iloc[i] if (hl2.iloc[i] - ST_FACTOR * st_atr.iloc[i] > lower[i-1] or close[i-1] < lower[i-1]) else lower[i-1])
        upper[i] = (hl2.iloc[i] + ST_FACTOR * st_atr.iloc[i] if (hl2.iloc[i] + ST_FACTOR * st_atr.iloc[i] < upper[i-1] or close[i-1] > upper[i-1]) else upper[i-1])
    mid_line = (pd.Series(lower, index=df.index) + pd.Series(upper, index=df.index)) / 2.0
    tL = calc_wma(mid_line, WMA_LENGTH).ewm(span=EMA_LENGTH, adjust=False).mean()
    return tL

def calc_smc_structure(df):
    right, window = 3, 7
    df['last_swing_high'] = pd.Series(np.where(df['high'].shift(right) == df['high'].rolling(window=window).max(), df['high'].shift(right), np.nan)).ffill()
    df['last_swing_low'] = pd.Series(np.where(df['low'].shift(right) == df['low'].rolling(window=window).min(), df['low'].shift(right), np.nan)).ffill()
    trend = pd.Series(np.nan, index=df.index)
    trend.loc[df['close'] > df['last_swing_high']] = 1
    trend.loc[df['close'] < df['last_swing_low']] = -1
    df['smc_trend'] = trend.ffill().fillna(0)
    return df

# ── Execution Logic ────────────────────────────────────────────────────────────
def calculate_lot_size(symbol, entry_price, sl_price, risk_usd):
    sl_dist = abs(entry_price - sl_price)
    if sl_dist == 0: return 0
    size = risk_usd / sl_dist
    # 🛡️ POSITION SIZE CAP (Fixes 110090)
    notional = size * entry_price
    if notional > MAX_NOTIONAL_USD:
        size = MAX_NOTIONAL_USD / entry_price
    return size

def execute_trade(symbol, direction, size, entry, sl, tp):
    side = 'buy' if direction == 'LONG' else 'sell'
    try:
        fmt_size = float(exchange.amount_to_precision(symbol, size))
        fmt_sl, fmt_tp = float(exchange.price_to_precision(symbol, sl)), float(exchange.price_to_precision(symbol, tp))
        order = exchange.create_order(symbol=symbol, type='market', side=side, amount=fmt_size,
            params={'stopLoss': str(fmt_sl), 'takeProfit': str(fmt_tp), 'tpslMode': 'Full'})
        return order, fmt_size, fmt_sl, fmt_tp
    except Exception as e:
        send_telegram(f"❌ Bybit Execution Failed ({symbol}): {e}")
        return None, None, None, None

def modify_bybit_tpsl(symbol, direction, new_sl, current_tp):
    try:
        market_id, bybit_side = exchange.market(symbol)['id'], 'Buy' if direction == 'LONG' else 'Sell'
        fmt_sl = float(exchange.price_to_precision(symbol, new_sl))
        exchange.privatePostV5PositionTradingStop({'category': 'linear', 'symbol': market_id, 'side': bybit_side, 'tpslMode': 'Full',
            'takeProfit': str(current_tp), 'stopLoss': str(fmt_sl), 'slOrderType': 'Market'})
        return fmt_sl
    except: return None

# ── Management Loop ────────────────────────────────────────────────────────────
def sync_open_positions():
    if not open_positions: return
    try:
        live_pos = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live_pos if float(p.get('contracts', 0)) > 0}
        for sym in [s for s in list(open_positions.keys()) if s not in live_syms]:
            pos = open_positions.pop(sym)
            res = exchange.private_get_v5_position_closed_pnl({'category': 'linear', 'symbol': exchange.market(sym)['id'], 'limit': 1})
            records = res.get('result', {}).get('list', [])
            if records:
                pnl = float(records[0].get('closedPnl', 0.0))
                record_closed_pnl(pnl)
                outcome = "WIN" if pnl > 0.5 else "LOSS" if pnl < -0.5 else "BREAKEVEN"
                send_webhook({"timestamp": datetime.now(timezone.utc).isoformat(), "asset": sym, "direction": pos['direction'], "entry": float(pos['entry']), "outcome": outcome, "strategy": "Liquidity Sweep", "pnl": pnl})
                send_telegram(f"{'✅' if pnl > 0 else '❌'} <b>TRADE CLOSED — {sym.split('/')[0]}</b>\nSettled Net PnL: <code>${pnl:.2f}</code>")
    except Exception as e: print(f"sync error: {e}")

def manage_trailing_stops():
    if not open_positions: return
    for symbol, pos in list(open_positions.items()):
        df = fetch_data(symbol, '1m', 5)
        if df is None or len(df) < 1: continue
        live_bar = df.iloc[-1]
        is_long = pos['direction'] == 'LONG'
        new_best = max(pos['best_price'], float(live_bar['high'])) if is_long else min(pos['best_price'], float(live_bar['low']))
        pos['best_price'] = new_best
        prof_dist = abs(new_best - pos['entry'])
        
        if prof_dist >= (1.0 * pos['atr']) and not pos.get('free_ride_triggered', False):
            pos['free_ride_triggered'] = True
            send_telegram(f"🛡️ <b>FREE RIDE SECURED — {symbol.split('/')[0]}</b>")

        if prof_dist < (1.0 * pos['atr']): continue
        raw_new_sl = (new_best - pos['trail_mult'] * pos['atr']) if is_long else (new_best + pos['trail_mult'] * pos['atr'])
        if (is_long and raw_new_sl > pos['current_sl']) or (not is_long and raw_new_sl < pos['current_sl']):
            if (is_long and raw_new_sl < float(live_bar['close'])) or (not is_long and raw_new_sl > float(live_bar['close'])):
                fmt_sl = modify_bybit_tpsl(symbol, pos['direction'], raw_new_sl, pos['catastrophic_tp'])
                if fmt_sl:
                    pos['current_sl'] = fmt_sl
                    send_telegram(f"<b>🔄 Trail Updated — {symbol.split('/')[0]}</b>\nNew SL: <code>{fmt_sl}</code>")

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
        c, price, atr, smc_trend = df.iloc[-2], float(df.iloc[-1]['close']), float(df.iloc[-2]['atr_14']), int(df.iloc[-2]['smc_trend'])
        
        # 🎯 PATCHED: More sensitive ATR threshold for Group 1 to avoid "Ignoring" them
        atr_limit = price * 0.00001 if symbol in ['ETH/USDT:USDT', 'SOL/USDT:USDT', 'LINK/USDT:USDT'] else price * 0.00005
        if atr < atr_limit or np.isnan(atr): continue

        algo_long = (df['tL'].iloc[-2] > df['tL'].iloc[-3]) and (df['tL'].iloc[-3] <= df['tL'].iloc[-4])
        algo_short = (df['tL'].iloc[-2] < df['tL'].iloc[-3]) and (df['tL'].iloc[-3] >= df['tL'].iloc[-4])
        
        l_sig, s_sig = algo_short and (smc_trend == -1), algo_long and (smc_trend == 1)
        if not l_sig and not s_sig: continue

        sl_m, tp_m, tr_m, p1, p2 = PER_SYMBOL_CONFIG[symbol]
        risk = (p1 if CURRENT_PHASE == 1 else p2) * (HOUSE_MONEY_MULTIPLIER if today_pnl >= HOUSE_MONEY_THRESHOLD else 1.0)
        
        raw_sl = price - sl_m * atr if l_sig else price + sl_m * atr
        size = calculate_lot_size(symbol, price, raw_sl, risk)
        
        # 🎯 PATCHED: Strict Fee Cap Check
        est_fee = size * price * BYBIT_TAKER_FEE * 2
        if est_fee > (risk * FEE_CAP_FRAC): continue

        order, f_size, f_sl, f_tp = execute_trade(symbol, 'LONG' if l_sig else 'SHORT', size, price, raw_sl, price + (10*atr if l_sig else -10*atr))
        if order:
            open_positions[symbol] = {'direction': 'LONG' if l_sig else 'SHORT', 'entry': price, 'atr': atr, 'best_price': price, 'current_sl': f_sl, 'catastrophic_tp': f_tp, 'trail_mult': tr_m, 'tp_mult': tp_m, 'size': f_size}
            msg = f"<b>🤯 APEX v7.2</b>\n{'🟢 LONG' if l_sig else '🔴 SHORT'} <b>{symbol.split('/')[0]}</b>\nRisk: ${risk:.0f} | Fee: ${est_fee:.2f}"
            send_telegram(msg)

def daily_reset():
    daily_pnl_tracker.clear()
    send_telegram("📅 Daily Reset Complete.")

if __name__ == '__main__':
    send_telegram("<b>🤯 APEX v7.2 RAILWAY LIVE</b>")
    schedule.every(1).minutes.do(fast_management)
    schedule.every(5).minutes.at(":00").do(check_signal)
    schedule.every().day.at("00:05").do(daily_reset)
    while True:
        schedule.run_pending()
        time.sleep(1)
