import ccxt
import pandas as pd
import numpy as np
import requests
import schedule
import time
import math
from datetime import datetime, timezone, date

# ── Credentials & Config ───────────────────────────────────────────
BYBIT_API_KEY    = "FOqGNCN6gRxu4bqMqF"      
BYBIT_API_SECRET = "YmSWYNkQbVXYiFU5v0G3y3R405VLREGu7icy"   

TELEGRAM_BOT_TOKEN = "8734785957:AAGzU-KPRY4mzXARxyTpLSHGemFtJ7AEsUQ"  
TELEGRAM_CHAT_ID   = "1932328527"               

CURRENT_PHASE     = 1        
DAILY_KILL_SWITCH = -150.0   
MAX_CONCURRENT    = 10       # Cap active trades to avoid over-leveraging
FEE_CAP_FRAC      = 0.40     

# 🔥 HOUSE MONEY & RADAR CONFIG
HOUSE_MONEY_THRESHOLD  = 75.0  
HOUSE_MONEY_MULTIPLIER = 1.5   
RADAR_MIN_VOLUME       = 15000000  # $15M minimum 24h volume
RADAR_TOP_COINS        = 5         # Focused on Top 5 for deep backtests
P1_RISK = 30.0
P2_RISK = 20.0

# ── AlgoAlpha Indicator Constants ─────────────────────────────────
ST_FACTOR  = 2.0    
ST_PERIOD  = 14     
WMA_LENGTH = 14     
EMA_LENGTH = 3      
ATR_PERIOD = 14     
BYBIT_MAKER_FEE = 0.00020  

# ── Runtime State ──────────────────────────────────────────────────
open_positions       = {}
pending_orders       = {}  
early_warnings       = {}  
daily_pnl_tracker    = {}
last_trade_bar       = {}  
active_watchlist     = []

edge_cooldowns       = {}  # The Burn Book
approved_coins       = {}  # Approved coin logic & multipliers

# ── Exchange ───────────────────────────────────────────────────────
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.enable_demo_trading(True) 
exchange.load_markets()

# ── Telegram ───────────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text.strip(), 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        print(f"  [Telegram error] {e}")

# ── Safety Check Helpers ───────────────────────────────────────────
def is_kill_switch_active() -> bool:
    today = date.today()
    return daily_pnl_tracker.get(today, 0.0) <= DAILY_KILL_SWITCH

def record_closed_pnl(pnl_usd: float):
    today = date.today()
    daily_pnl_tracker[today] = daily_pnl_tracker.get(today, 0.0) + pnl_usd

# ── 🧠 CONTINUOUS MARKET RADAR (CASCADING) ──────────────────────────
def scan_market_radar():
    print("📡 [RADAR] Sweeping Bybit for Top 5 active momentum targets...")
    try:
        now = time.time()
        expired = [sym for sym, expiry in edge_cooldowns.items() if now > expiry]
        for sym in expired: del edge_cooldowns[sym]

        tickers = exchange.fetch_tickers()
        valid_coins = []
        
        for symbol, data in tickers.items():
            if not symbol.endswith(':USDT'): continue
            if symbol in edge_cooldowns: continue 
            
            quote_volume = float(data.get('quoteVolume', 0))
            last_price = float(data.get('last', 0))
            high_24 = float(data.get('high', 0))
            low_24 = float(data.get('low', 0))
            
            if last_price == 0 or low_24 == 0 or quote_volume < RADAR_MIN_VOLUME: continue
            
            volatility_score = (high_24 - low_24) / low_24
            valid_coins.append({'symbol': symbol, 'volatility': volatility_score})
            
        valid_coins.sort(key=lambda x: x['volatility'], reverse=True)
        top_coins = [c['symbol'] for c in valid_coins[:RADAR_TOP_COINS]]
        
        global active_watchlist
        active_watchlist = top_coins
        print(f"🎯 [RADAR LOCK] Tracking: {[s.split('/')[0] for s in active_watchlist]}")
        
    except Exception as e:
        print(f"❌ [RADAR ERROR] Market sweep failed: {e}")

# ── Indicators & Data ─────────────────────────────────────────────
def fetch_data(symbol, timeframe='15m', limit=300):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        return df
    except Exception: return None

def fetch_deep_data(symbol, timeframe='15m', target_limit=6000):
    try:
        timeframe_ms = 15 * 60 * 1000
        since = exchange.milliseconds() - (target_limit * timeframe_ms)
        all_ohlcv = []
        while len(all_ohlcv) < target_limit:
            limit = min(1000, target_limit - len(all_ohlcv))
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            if not ohlcv: break
            since = ohlcv[-1][0] + 1
            all_ohlcv.extend(ohlcv)
            time.sleep(0.1)
        df = pd.DataFrame(all_ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        return df
    except Exception: return None

def rma(series, length):
    return series.ewm(alpha=1/length, adjust=False).mean()

def calc_atr(df, length):
    prev_close = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - prev_close).abs(), (df['low'] - prev_close).abs()], axis=1).max(axis=1)
    return rma(tr, length)

def calc_wma(series, length):
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

def algoalpha_baseline(df):
    st_atr = calc_atr(df, ST_PERIOD)
    hl2    = (df['high'] + df['low']) / 2
    basic_upper = hl2 + ST_FACTOR * st_atr
    basic_lower = hl2 - ST_FACTOR * st_atr
    upper = np.zeros(len(df))
    lower = np.zeros(len(df))
    close = df['close'].values
    upper[0], lower[0] = basic_upper.iloc[0], basic_lower.iloc[0]
    for i in range(1, len(df)):
        lower[i] = (basic_lower.iloc[i] if (basic_lower.iloc[i] > lower[i-1] or close[i-1] < lower[i-1]) else lower[i-1])
        upper[i] = (basic_upper.iloc[i] if (basic_upper.iloc[i] < upper[i-1] or close[i-1] > upper[i-1]) else upper[i-1])
    mid_line = (pd.Series(lower, index=df.index) + pd.Series(upper, index=df.index)) / 2.0
    return calc_wma(mid_line, WMA_LENGTH).ewm(span=EMA_LENGTH, adjust=False).mean()

def calc_smc_structure(df):
    roll_max = df['high'].rolling(window=7).max()
    is_swing_high = df['high'].shift(3) == roll_max
    df['last_swing_high'] = pd.Series(np.where(is_swing_high, df['high'].shift(3), np.nan)).ffill()
    roll_min = df['low'].rolling(window=7).min()
    is_swing_low = df['low'].shift(3) == roll_min
    df['last_swing_low'] = pd.Series(np.where(is_swing_low, df['low'].shift(3), np.nan)).ffill()
    trend = pd.Series(np.nan, index=df.index)
    trend.loc[(df['close'] > df['last_swing_high'])] = 1   
    trend.loc[(df['close'] < df['last_swing_low'])] = -1  
    df['smc_trend'] = trend.ffill().fillna(0)
    return df

# ── 🧠 MIRROR LOGIC DYNAMIC ENGINE ────────────────────────────────
def calculate_historical_edge(df, min_trades=100):
    algo_long = (df['tL'].shift(1) > df['tL'].shift(2)) & (df['tL'].shift(2) <= df['tL'].shift(3))
    algo_short = (df['tL'].shift(1) < df['tL'].shift(2)) & (df['tL'].shift(2) >= df['tL'].shift(3))
    
    modes = ['Inverted', 'Standard']
    test_multipliers = [1.50, 2.00, 2.50, 3.00]
    best_mult, best_mode, best_exp = None, None, 0.0
    
    for mode in modes:
        if mode == 'Inverted':
            l_sig = algo_short & (df['smc_trend'].shift(1) == -1)
            s_sig = algo_long & (df['smc_trend'].shift(1) == 1)
        else:
            l_sig = algo_long & (df['smc_trend'].shift(1) == 1)
            s_sig = algo_short & (df['smc_trend'].shift(1) == -1)
            
        indices = df.index[l_sig | s_sig].tolist()
        for sl_m in test_multipliers:
            tp_m, trades = sl_m * 2.0, []
            for idx in indices:
                if idx >= len(df) - 2: continue
                is_l, entry, atr = l_sig[idx], df['close'].iloc[idx], df['atr_14'].iloc[idx]
                if pd.isna(atr) or atr == 0: continue
                sl_p = entry - (atr * sl_m) if is_l else entry + (atr * sl_m)
                tp_p = entry + (atr * tp_m) if is_l else entry - (atr * tp_m)
                tr_r = 0.0
                for fwd in range(idx + 1, len(df)):
                    h, l = df['high'].iloc[fwd], df['low'].iloc[fwd]
                    if is_l:
                        if l <= sl_p: tr_r = -1.0; break
                        elif h >= tp_p: tr_r = 2.0; break
                    else:
                        if h >= sl_p: tr_r = -1.0; break
                        elif l <= tp_p: tr_r = 2.0; break
                if tr_r != 0.0: trades.append(tr_r)
            if len(trades) >= min_trades:
                exp = sum(trades) / len(trades)
                if exp > 0.15 and exp > best_exp:
                    best_exp, best_mult, best_mode = exp, sl_m, mode
    return best_mult, best_mode

# ── Auto-Leverage & Margin ─────────────────────────────────────────
def set_isolated_and_leverage(symbol, entry_price, sl_price):
    try:
        sl_percent = abs(entry_price - sl_price) / entry_price
        safe_leverage = math.floor(1 / (sl_percent * 1.2)) 
        leverage = max(1, min(safe_leverage, 25))
        try: exchange.set_margin_mode('isolated', symbol)
        except Exception: pass
        try: exchange.set_leverage(leverage, symbol)
        except Exception: pass
        return leverage
    except Exception: return 10 

# ── Limit Execution ────────────────────────────────────────────────
def execute_trade(symbol, direction, size, entry, sl, tp):
    side = 'buy' if direction == 'LONG' else 'sell'
    try:
        fmt_size = float(exchange.amount_to_precision(symbol, size))
        fmt_sl, fmt_tp = str(float(exchange.price_to_precision(symbol, sl))), str(float(exchange.price_to_precision(symbol, tp)))
        fmt_price = float(exchange.price_to_precision(symbol, entry))
        set_isolated_and_leverage(symbol, entry, sl)
        order = exchange.create_order(symbol=symbol, type='limit', side=side, amount=fmt_size, price=fmt_price, 
            params={'stopLoss': fmt_sl, 'takeProfit': fmt_tp, 'tpslMode': 'Full', 'slOrderType': 'Market', 'tpOrderType': 'Market', 'timeInForce': 'GTC'})
        return order, fmt_size, float(fmt_sl), float(fmt_tp)
    except Exception: return None, None, None, None

def modify_bybit_tpsl(symbol, direction, new_sl, current_tp):
    market_id = exchange.market(symbol)['id']
    bybit_side = 'Buy' if direction == 'LONG' else 'Sell'
    try:
        fmt_sl = float(exchange.price_to_precision(symbol, new_sl))
        exchange.privatePostV5PositionTradingStop({'category': 'linear', 'symbol': market_id, 'side': bybit_side, 'tpslMode': 'Full',
            'takeProfit': str(current_tp), 'stopLoss': str(fmt_sl), 'slOrderType': 'Market', 'tpOrderType': 'Market', 'slTriggerBy': 'LastPrice', 'tpTriggerBy': 'LastPrice'})
        return fmt_sl
    except Exception: return None

# ── Order Monitoring ───────────────────────────────────────────────
def monitor_pending_orders():
    if not pending_orders: return
    try:
        live_positions = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live_positions if float(p.get('contracts', 0)) > 0}
        for sym in list(pending_orders.keys()):
            if sym in live_syms:
                pos_data = pending_orders.pop(sym)
                open_positions[sym] = pos_data
                send_telegram(f"<b>✅ FILLED — {sym.split('/')[0]}</b>\nMode: {pos_data['mode']}\nEntry: {pos_data['entry']}\nRisk: ${pos_data['risk_usd']}")
    except Exception: pass

def cancel_stale_orders():
    if not pending_orders: return
    try:
        open_orders = exchange.fetch_open_orders()
        for order in open_orders:
            if order['symbol'] in pending_orders and (exchange.milliseconds() - order['timestamp']) > 840000:
                exchange.cancel_order(order['id'], order['symbol'])
                pending_orders.pop(order['symbol'], None)
    except Exception: pass

def sync_open_positions():
    if not open_positions: return
    try:
        live_positions = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live_positions if float(p.get('contracts', 0)) > 0}
        for sym in list(open_positions.keys()):
            if sym not in live_syms:
                open_positions.pop(sym)
                market_id = exchange.market(sym)['id']
                recs = exchange.private_get_v5_position_closed_pnl({'category': 'linear', 'symbol': market_id, 'limit': 1}).get('result', {}).get('list', [])
                if recs: record_closed_pnl(float(recs[0].get('closedPnl', 0.0)))
    except Exception: pass

# ── 🛡️ FEE-SAFE BREAK EVEN ENGINE ──────────────────────────────────
def manage_trailing_stops():
    if not open_positions: return
    for symbol, pos in list(open_positions.items()):
        df = fetch_data(symbol, '1m', 5)
        if df is None or len(df) < 1: continue
        live = float(df.iloc[-1]['close'])
        is_l, entry, sl_dist = pos['direction'] == 'LONG', pos['entry'], pos['sl_distance']
        best = max(pos['best_price'], float(df.iloc[-1]['high'])) if is_l else min(pos['best_price'], float(df.iloc[-1]['low']))
        pos['best_price'] = best
        profit_d = abs(best - entry)

        # 1. THE FEE-SAFE FREE RIDE (Break-Even + 0.2% Profit Buffer)
        if profit_d >= sl_dist and not pos.get('free_ride_triggered', False):
            pos['free_ride_triggered'] = True
            # Move SL slightly past entry (0.002 = 0.2%) to cover fees
            be_sl = entry * 1.002 if is_l else entry * 0.998
            fmt_sl = modify_bybit_tpsl(symbol, pos['direction'], be_sl, pos['catastrophic_tp'])
            if fmt_sl: 
                pos['current_sl'] = fmt_sl
                send_telegram(f"🛡️ <b>FEE-SAFE BE — {symbol.split('/')[0]}</b>\nSL moved to Entry + 0.2%")

        # 2. THE 2R TRAIL
        if profit_d >= (sl_dist * 2.0):
            trail_p = (best - (0.10 * pos['atr'])) if is_l else (best + (0.10 * pos['atr']))
            if (is_l and trail_p > pos['current_sl']) or (not is_l and trail_p < pos['current_sl']):
                fmt_sl = modify_bybit_tpsl(symbol, pos['direction'], trail_p, pos['catastrophic_tp'])
                if fmt_sl: pos['current_sl'] = fmt_sl

def fast_management():
    monitor_pending_orders(); cancel_stale_orders(); sync_open_positions(); manage_trailing_stops()

# ── Signal Engine ──────────────────────────────────────────────────
def check_signal():
    ts_now, today = datetime.now(timezone.utc), datetime.now(timezone.utc).date()
    today_pnl = daily_pnl_tracker.get(today, 0.0)
    if is_kill_switch_active(): return
    scan_market_radar(); fast_management()
    if len(open_positions) + len(pending_orders) >= MAX_CONCURRENT: return

    for symbol in active_watchlist:
        if symbol in open_positions or symbol in pending_orders: continue
        
        if symbol in approved_coins:
            df = fetch_data(symbol, '15m', 500)
            config = approved_coins[symbol]
            opt_sl_m, mode = config['mult'], config['mode']
        else:
            df = fetch_deep_data(symbol, '15m', 6000)
            if df is None or len(df) < 3000: continue
            df['atr_14'] = calc_atr(df, ATR_PERIOD); df['tL'] = algoalpha_baseline(df); df = calc_smc_structure(df)
            opt_sl_m, mode = calculate_historical_edge(df, min_trades=100)
            if not opt_sl_m: edge_cooldowns[symbol] = time.time() + 14400; continue
            approved_coins[symbol] = {'mult': opt_sl_m, 'mode': mode}

        df['atr_14'] = calc_atr(df, ATR_PERIOD); df['tL'] = algoalpha_baseline(df); df = calc_smc_structure(df)
        c15m, price = df.iloc[-2], float(df.iloc[-1]['close'])
        atr, smc_t, bar_ts = float(c15m['atr_14']), int(c15m['smc_trend']), int(c15m['ts'])
        if last_trade_bar.get(symbol) == bar_ts: continue

        algo_l = (float(df['tL'].iloc[-2]) > float(df['tL'].iloc[-3])) and (float(df['tL'].iloc[-3]) <= float(df['tL'].iloc[-4]))
        algo_s = (float(df['tL'].iloc[-2]) < float(df['tL'].iloc[-3])) and (float(df['tL'].iloc[-3]) >= float(df['tL'].iloc[-4]))

        if mode == 'Inverted':
            l_sig, s_sig = (algo_s and smc_t == -1), (algo_l and smc_t == 1)
        else:
            l_sig, s_sig = (algo_l and smc_t == 1), (algo_s and smc_t == -1)

        if not l_sig and not s_sig: continue
        
        risk_usd = (P1_RISK if CURRENT_PHASE == 1 else P2_RISK) * (HOUSE_MONEY_MULTIPLIER if today_pnl >= HOUSE_MONEY_THRESHOLD else 1.0)
        direction = 'LONG' if l_sig else 'SHORT'
        sl_p = price - (opt_sl_m * atr) if l_sig else price + (opt_sl_m * atr)
        tp_p = price + (10.0 * atr) if l_sig else price - (10.0 * atr)
        sl_d = abs(price - sl_p)
        lot = risk_usd / sl_d
        
        order, f_size, f_sl, f_tp = execute_trade(symbol, direction, lot, price, sl_p, tp_p)
        if order:
            last_trade_bar[symbol] = bar_ts
            pending_orders[symbol] = {'direction': direction, 'entry': price, 'atr': atr, 'best_price': price, 'current_sl': f_sl, 
                                      'catastrophic_tp': f_tp, 'sl_distance': sl_d, 'risk_usd': risk_usd, 'mode': mode}

def daily_reset():
    daily_pnl_tracker.clear(); early_warnings.clear(); edge_cooldowns.clear(); approved_coins.clear()

if __name__ == '__main__':
    check_signal()
    schedule.every(1).minutes.do(fast_management)
    schedule.every(5).minutes.at(":00").do(check_signal) 
    schedule.every().day.at("00:05").do(daily_reset)
    while True:
        schedule.run_pending(); time.sleep(1)
