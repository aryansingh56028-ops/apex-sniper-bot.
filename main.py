import ccxt
import pandas as pd
import numpy as np
import requests
import schedule
import time
from datetime import datetime, timezone, date

# ── Credentials & Config (DEMO KEYS INTEGRATED) ────────────────────────────────
BYBIT_API_KEY    = "uaYuCtuHQrPj6vuKEi"
BYBIT_API_SECRET = "Y2aouZ6fGHv9AqWgNl9WdqWLKTEZlP8OqVCe"

TELEGRAM_BOT_TOKEN = "8734785957:AAGzU-KPRY4mzXARxyTpLSHGemFtJ7AEsUQ"
TELEGRAM_CHAT_ID   = "1932328527"               

CURRENT_PHASE     = 1        
DAILY_KILL_SWITCH = -150.0   
DAILY_PROFIT_LOCK = +9999.0  
MAX_CONCURRENT    = 999
FEE_CAP_FRAC      = 0.40     

# 🔥 HOUSE MONEY CONFIGURATION
HOUSE_MONEY_THRESHOLD  = 75.0  
HOUSE_MONEY_MULTIPLIER = 1.5   

# ── Per-Symbol Config (RESTORED PREVIOUS SETTINGS) ─────────────────────────────
PER_SYMBOL_CONFIG = {
    # ── Group 1: Custom SL | TP=3.00× | Trail=0.10× | P1=$30 | P2=$20 ─────────
    'ETH/USDT:USDT':    (1.00, 3.00, 0.10, 30.0, 20.0),
    'XRP/USDT:USDT':    (1.00, 3.00, 0.10, 30.0, 20.0),
    'OP/USDT:USDT':     (1.00, 3.00, 0.10, 30.0, 20.0),
    'SOL/USDT:USDT':    (2.00, 3.00, 0.10, 30.0, 20.0),
    'ONDO/USDT:USDT':   (1.00, 3.00, 0.10, 30.0, 20.0),
    'ZEC/USDT:USDT':    (2.00, 3.00, 0.10, 30.0, 20.0),
    'LTC/USDT:USDT':    (2.00, 3.00, 0.10, 30.0, 20.0),
    'ADA/USDT:USDT':    (1.00, 3.00, 0.10, 30.0, 20.0),
    'LINK/USDT:USDT':   (1.00, 3.00, 0.10, 30.0, 20.0),
    'WLF/USDT:USDT':    (1.00, 3.00, 0.10, 30.0, 20.0),
    
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
    'JASMY/USDT:USDT':    (0.45, 4.00, 0.10, 35.0, 25.0), # Group 2 Active
    'SUI/USDT:USDT':      (0.45, 4.00, 0.10, 35.0, 25.0),
}

SYMBOLS = list(PER_SYMBOL_CONFIG.keys())

# ── AlgoAlpha Indicator Constants ──────────────────────────────────────────────
ST_FACTOR  = 2.0    
ST_PERIOD  = 14     
WMA_LENGTH = 14     
EMA_LENGTH = 3      
ATR_PERIOD = 14     
BYBIT_TAKER_FEE = 0.00055  

# ── Runtime State ──────────────────────────────────────────────────────────────
open_positions       = {}
daily_pnl_tracker    = {}
last_trade_bar       = {}  

# ── Exchange Setup ─────────────────────────────────────────────────────────────
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.set_sandbox_mode(True) # Verified for Demo Use
exchange.load_markets()

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text.strip(), 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        print(f"  [Telegram error] {e}")

# ── Indicators & Core Logic ────────────────────────────────────────────────────
def fetch_data(symbol, timeframe='15m', limit=300):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        return df
    except Exception as e:
        return None

def rma(series, length): return series.ewm(alpha=1/length, adjust=False).mean()

def calc_atr(df, length):
    prev_close = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - prev_close).abs(), (df['low']  - prev_close).abs()], axis=1).max(axis=1)
    return rma(tr, length)

def calc_wma(series, length):
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def algoalpha_baseline(df):
    st_atr = calc_atr(df, ST_PERIOD)
    hl2    = (df['high'] + df['low']) / 2
    basic_upper = hl2 + ST_FACTOR * st_atr
    basic_lower = hl2 - ST_FACTOR * st_atr
    upper, lower = np.zeros(len(df)), np.zeros(len(df))
    close = df['close'].values
    upper[0], lower[0] = basic_upper.iloc[0], basic_lower.iloc[0]
    for i in range(1, len(df)):
        lower[i] = (basic_lower.iloc[i] if (basic_lower.iloc[i] > lower[i-1] or close[i-1] < lower[i-1]) else lower[i-1])
        upper[i] = (basic_upper.iloc[i] if (basic_upper.iloc[i] < upper[i-1] or close[i-1] > upper[i-1]) else upper[i-1])
    mid_line = (pd.Series(lower, index=df.index) + pd.Series(upper, index=df.index)) / 2.0
    return calc_wma(mid_line, WMA_LENGTH).ewm(span=EMA_LENGTH, adjust=False).mean()

def calc_smc_structure(df):
    right, window = 3, 7
    df['last_swing_high'] = pd.Series(np.where(df['high'].shift(right) == df['high'].rolling(window=window).max(), df['high'].shift(right), np.nan)).ffill()
    df['last_swing_low'] = pd.Series(np.where(df['low'].shift(right) == df['low'].rolling(window=window).min(), df['low'].shift(right), np.nan)).ffill()
    trend = pd.Series(np.nan, index=df.index)
    trend.loc[df['close'] > df['last_swing_high']] = 1   
    trend.loc[df['close'] < df['last_swing_low']] = -1  
    df['smc_trend'] = trend.ffill().fillna(0)
    return df

# ── Trade Execution ────────────────────────────────────────────────────────────
def calculate_lot_size(symbol, entry_price, sl_price, risk_usd):
    dist = abs(entry_price - sl_price)
    return risk_usd / dist if dist != 0 else 0

def execute_trade(symbol, direction, size, entry, sl, tp):
    side = 'buy' if direction == 'LONG' else 'sell'
    try:
        fmt_size, fmt_sl, fmt_tp = float(exchange.amount_to_precision(symbol, size)), float(exchange.price_to_precision(symbol, sl)), float(exchange.price_to_precision(symbol, tp))
        order = exchange.create_order(symbol=symbol, type='market', side=side, amount=fmt_size,
            params={'stopLoss': str(fmt_sl), 'takeProfit': str(fmt_tp), 'tpslMode': 'Full'})
        return order, fmt_size, fmt_sl, fmt_tp
    except Exception as e:
        send_telegram(f"❌ Execution Failed ({symbol}): {e}")
        return None, None, None, None

def modify_bybit_tpsl(symbol, direction, new_sl, current_tp):
    try:
        fmt_sl = float(exchange.price_to_precision(symbol, new_sl))
        exchange.privatePostV5PositionTradingStop({
            'category': 'linear', 'symbol': exchange.market(symbol)['id'], 'side': 'Buy' if direction == 'LONG' else 'Sell',
            'takeProfit': str(current_tp), 'stopLoss': str(fmt_sl), 'slOrderType': 'Market'
        })
        return fmt_sl
    except: return None

# ── Management Logic ───────────────────────────────────────────────────────────
def sync_open_positions():
    if not open_positions: return
    try:
        live_positions = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live_positions if float(p.get('contracts', 0)) > 0}
        for sym in [s for s in list(open_positions.keys()) if s not in live_syms]:
            pos = open_positions.pop(sym)
            res = exchange.private_get_v5_position_closed_pnl({'category': 'linear', 'symbol': exchange.market(sym)['id'], 'limit': 1})
            pnl = float(res.get('result', {}).get('list', [{}])[0].get('closedPnl', 0.0))
            daily_pnl_tracker[date.today()] = daily_pnl_tracker.get(date.today(), 0.0) + pnl
            send_telegram(f"{'✅' if pnl > 0 else '❌'} <b>CLOSED {sym.split('/')[0]}</b>\nNet PnL: <code>${pnl:.2f}</code>")
    except Exception as e: print(f"Sync error: {e}")

def manage_trailing_stops():
    for symbol, pos in list(open_positions.items()):
        df = fetch_data(symbol, '1m', 5)
        if df is None or len(df) < 1: continue
        live = df.iloc[-1]
        is_long = pos['direction'] == 'LONG'
        new_best = max(pos['best_price'], float(live['high'])) if is_long else min(pos['best_price'], float(live['low']))
        pos['best_price'] = new_best
        
        profit_dist = abs(new_best - pos['entry'])
        if profit_dist >= (1.0 * pos['atr']) and not pos.get('free_ride', False):
            pos['free_ride'] = True
            send_telegram(f"🛡️ <b>FREE RIDE — {symbol.split('/')[0]}</b>")

        if profit_dist < (1.0 * pos['atr']): continue
        
        raw_sl = (new_best - (pos['trail_mult'] * pos['atr'])) if is_long else (new_best + (pos['trail_mult'] * pos['atr']))
        if (is_long and raw_sl > pos['current_sl']) or (not is_long and raw_sl < pos['current_sl']):
            fmt_sl = modify_bybit_tpsl(symbol, pos['direction'], raw_sl, pos['catastrophic_tp'])
            if fmt_sl: pos['current_sl'] = fmt_sl

# ── Main Loop ──────────────────────────────────────────────────────────────────
def check_signal():
    today_pnl = daily_pnl_tracker.get(date.today(), 0.0)
    if today_pnl <= DAILY_KILL_SWITCH: return

    sync_open_positions()
    manage_trailing_stops()

    for symbol in SYMBOLS:
        if symbol in open_positions or len(open_positions) >= MAX_CONCURRENT: continue
        df = fetch_data(symbol, '15m', 200)
        if df is None or len(df) < 150: continue
        
        df['atr'] = calc_atr(df, ATR_PERIOD)
        df['tL']  = algoalpha_baseline(df)
        df = calc_smc_structure(df)
        
        c, p, o = df.iloc[-2], df.iloc[-3], df.iloc[-4]
        price, atr, trend = float(df.iloc[-1]['close']), float(c['atr']), int(c['smc_trend'])
        
        long_sig = (c['tL'] < p['tL']) and (p['tL'] >= o['tL']) and trend == -1
        short_sig = (c['tL'] > p['tL']) and (p['tL'] <= o['tL']) and trend == 1

        if long_sig or short_sig:
            sl_m, tp_m, tr_m, p1, p2 = PER_SYMBOL_CONFIG[symbol]
            risk = (p1 if CURRENT_PHASE == 1 else p2) * (HOUSE_MONEY_MULTIPLIER if today_pnl >= HOUSE_MONEY_THRESHOLD else 1.0)
            
            side = 'LONG' if long_sig else 'SHORT'
            sl_price = price - sl_m * atr if long_sig else price + sl_m * atr
            
            order, sz, fsl, ftp = execute_trade(symbol, side, calculate_lot_size(symbol, price, sl_price, risk), price, sl_price, (price + 10*atr if long_sig else price - 10*atr))
            if order:
                open_positions[symbol] = {'direction': side, 'entry': price, 'atr': atr, 'best_price': price, 'current_sl': fsl, 'catastrophic_tp': ftp, 'trail_mult': tr_m, 'tp_mult': tp_m}
                send_telegram(f"<b>🚀 {side} {symbol.split('/')[0]}</b>\nEntry: {price}\nRisk: ${risk:.0f}")

if __name__ == '__main__':
    send_telegram("<b>🔄 APEX SANDBOX V7.2.8 ONLINE</b>\nDemo Mode Enabled")
    schedule.every(1).minutes.do(lambda: (sync_open_positions(), manage_trailing_stops()))
    schedule.every(5).minutes.at(":00").do(check_signal)
    while True:
        schedule.run_pending()
        time.sleep(1)
