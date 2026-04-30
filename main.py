import ccxt
import pandas as pd
import numpy as np
import requests
import schedule
import time
import math
import threading
from datetime import datetime, timezone, date

# ── Credentials & Config ───────────────────────────────────────────
BYBIT_API_KEY    = "jImaJiIeKBjAQW9z3W"      
BYBIT_API_SECRET = "YBinRS6gX355mnRmiRCCwo2rRVQUGMo3pgSu"   

TELEGRAM_BOT_TOKEN = "8586984642:AAEMFum2ICKmwS1NF8XYmUNDxRdYN7aRJmY"  
TELEGRAM_CHAT_ID   = "1932328527"               

CURRENT_PHASE     = 1        
# ── Prop Firm 5k Calibration ──
DAILY_KILL_SWITCH = -180.0   
MAX_CONCURRENT    = 5        
FEE_CAP_FRAC      = 0.40     

# 🔥 HOUSE MONEY & RADAR CONFIG (NET WIDENED)
HOUSE_MONEY_THRESHOLD  = 60.0  
HOUSE_MONEY_MULTIPLIER = 1.5   
RADAR_MIN_VOLUME       = 25000000  
RADAR_TOP_COINS        = 50        
P1_RISK = 25.0                     
P2_RISK = 25.0

# 🛑 CRYPTO-ONLY BLOCKLIST
# Prevents the bot from trading Metals, Energy, Forex, and Global Indices on Bybit
NON_CRYPTO_BLOCKLIST = {
    'XAU', 'XAG', 'WTI', 'BRENT', 'COPPER', 'PLAT', 'PALLADIUM', 
    'EUR', 'GBP', 'JPY', 'CHF', 'AUD', 'NZD', 'CAD', 'SGD',
    'US30', 'US100', 'US500', 'UK100', 'DE40', 'NI225', 'HK50', 'EU50'
}

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
daily_trade_stats    = {'total_trades': 0, 'wins': 0, 'coin_pnl': {}} 
last_trade_bar       = {}  
active_watchlist     = []

edge_cooldowns       = {}  
approved_coins       = {}  

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
    try: requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text.strip(), 'parse_mode': 'HTML'}, timeout=10)
    except Exception: pass

def is_kill_switch_active() -> bool:
    return daily_pnl_tracker.get(date.today(), 0.0) <= DAILY_KILL_SWITCH

def record_closed_pnl(symbol: str, pnl_usd: float):
    today = date.today()
    daily_pnl_tracker[today] = daily_pnl_tracker.get(today, 0.0) + pnl_usd
    
    base_coin = symbol.split('/')[0]
    daily_trade_stats['total_trades'] += 1
    if pnl_usd > 0:
        daily_trade_stats['wins'] += 1
    daily_trade_stats['coin_pnl'][base_coin] = daily_trade_stats['coin_pnl'].get(base_coin, 0.0) + pnl_usd

def send_eod_summary():
    today = date.today()
    total_pnl = daily_pnl_tracker.get(today, 0.0)
    trades = daily_trade_stats['total_trades']
    wins = daily_trade_stats['wins']
    wr = (wins / trades * 100) if trades > 0 else 0.0
    
    best_coin = "None"
    best_coin_pnl = 0.0
    if daily_trade_stats['coin_pnl']:
        best_coin = max(daily_trade_stats['coin_pnl'], key=daily_trade_stats['coin_pnl'].get)
        best_coin_pnl = daily_trade_stats['coin_pnl'][best_coin]
    
    emoji = "🟢" if total_pnl >= 0 else "🔴"
    msg = (f"📅 <b>EOD SUMMARY ({today})</b>\n"
           f"{emoji} Total PnL: {total_pnl:+.2f} USD\n"
           f"📊 Win Rate: {wr:.1f}%\n"
           f"🔢 Total Trades: {trades}\n"
           f"🏆 Best Coin: {best_coin} ({best_coin_pnl:+.2f} USD)")
    
    send_telegram(msg)
    
    daily_pnl_tracker.clear()
    daily_trade_stats['total_trades'] = 0
    daily_trade_stats['wins'] = 0
    daily_trade_stats['coin_pnl'].clear()
    approved_coins.clear()
    edge_cooldowns.clear()

# ── 🧠 CONTINUOUS MARKET RADAR ─────────────────────────────────────
def scan_market_radar():
    print(f"📡 [RADAR] Sweeping Bybit for Top {RADAR_TOP_COINS} Liquid Movers...")
    try:
        now = time.time()
        expired = [sym for sym, expiry in edge_cooldowns.items() if now > expiry]
        for sym in expired: del edge_cooldowns[sym]

        tickers = exchange.fetch_tickers()
        valid_coins = []
        for symbol, data in tickers.items():
            if not symbol.endswith(':USDT'): continue
            
            # 👉 NEW: Apply Blocklist
            base_coin = symbol.split('/')[0]
            if base_coin in NON_CRYPTO_BLOCKLIST: continue
            
            if symbol in edge_cooldowns: continue 
            
            qv = float(data.get('quoteVolume', 0))
            lp = float(data.get('last', 0))
            h24 = float(data.get('high', 0))
            l24 = float(data.get('low', 0))
            
            if lp == 0 or l24 == 0 or qv < RADAR_MIN_VOLUME: continue
            
            volatility = (h24 - l24) / l24
            valid_coins.append({'symbol': symbol, 'volatility': volatility, 'volume': qv})
            
        valid_coins.sort(key=lambda x: x['volume'], reverse=True)
        top_liquid_50 = valid_coins[:50] 
        top_liquid_50.sort(key=lambda x: x['volatility'], reverse=True)
        
        global active_watchlist
        active_watchlist = [c['symbol'] for c in top_liquid_50[:RADAR_TOP_COINS]]
        print(f"🎯 [RADAR LOCK] Crypto Targets: {[s.split('/')[0] for s in active_watchlist]}")
    except Exception as e: 
        print(f"Radar Error: {e}")

# ── Indicators & Data ─────────────────────────────────────────────
def fetch_data(symbol, timeframe='15m', limit=500):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        return df
    except Exception: return None

def fetch_deep_data(symbol, timeframe='15m', target_limit=6000):
    try:
        since = exchange.milliseconds() - (target_limit * 15 * 60 * 1000)
        all_ohlcv = []
        while len(all_ohlcv) < target_limit:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=min(1000, target_limit - len(all_ohlcv)))
            if not ohlcv: break
            since = ohlcv[-1][0] + 1
            all_ohlcv.extend(ohlcv)
            time.sleep(0.1)
        df = pd.DataFrame(all_ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        return df
    except Exception: return None

def calc_rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=period-1, adjust=False).mean()
    ema_down = down.ewm(com=period-1, adjust=False).mean()
    rs = ema_up / ema_down
    return 100 - (100 / (1 + rs))

def rma(series, length): return series.ewm(alpha=1/length, adjust=False).mean()

def calc_atr(df, length):
    prev_close = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - prev_close).abs(), (df['low'] - prev_close).abs()], axis=1).max(axis=1)
    return rma(tr, length)

def calc_wma(series, length):
    w = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)

def algoalpha_baseline(df):
    st_atr = calc_atr(df, ST_PERIOD)
    hl2    = (df['high'] + df['low']) / 2
    b_up, b_dn = hl2 + ST_FACTOR * st_atr, hl2 - ST_FACTOR * st_atr
    upper, lower, close = np.zeros(len(df)), np.zeros(len(df)), df['close'].values
    upper[0], lower[0] = b_up.iloc[0], b_dn.iloc[0]
    for i in range(1, len(df)):
        lower[i] = b_dn.iloc[i] if (b_dn.iloc[i] > lower[i-1] or close[i-1] < lower[i-1]) else lower[i-1]
        upper[i] = b_up.iloc[i] if (b_up.iloc[i] < upper[i-1] or close[i-1] > upper[i-1]) else upper[i-1]
    mid_line = (pd.Series(lower, index=df.index) + pd.Series(upper, index=df.index)) / 2.0
    return calc_wma(mid_line, WMA_LENGTH).ewm(span=EMA_LENGTH, adjust=False).mean()

def calc_smc_structure(df):
    r_max = df['high'].rolling(window=7).max()
    is_sh = df['high'].shift(3) == r_max
    df['last_sh'] = pd.Series(np.where(is_sh, df['high'].shift(3), np.nan)).ffill()
    r_min = df['low'].rolling(window=7).min()
    is_sl = df['low'].shift(3) == r_min
    df['last_sl'] = pd.Series(np.where(is_sl, df['low'].shift(3), np.nan)).ffill()
    trend = pd.Series(np.nan, index=df.index)
    trend.loc[(df['close'] > df['last_sh'])] = 1   
    trend.loc[(df['close'] < df['last_sl'])] = -1  
    df['smc_trend'] = trend.ffill().fillna(0)
    return df

def calc_macd(df):
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    df['macd_flip_bull'] = (df['macd_hist'] > 0) & (df['macd_hist'].shift(1) <= 0)
    df['macd_flip_bear'] = (df['macd_hist'] < 0) & (df['macd_hist'].shift(1) >= 0)
    return df

def calc_sweeps(df, lookback=96, holding_period=12): 
    session_high = df['high'].rolling(lookback).max().shift(1)
    session_low = df['low'].rolling(lookback).min().shift(1)
    df['sweep_hod'] = ((df['high'] > session_high) & (df['close'] < session_high)).astype(int)
    df['sweep_lod'] = ((df['low'] < session_low) & (df['close'] > session_low)).astype(int)
    df['recent_sweep_hod'] = df['sweep_hod'].rolling(holding_period).max().fillna(0)
    df['recent_sweep_lod'] = df['sweep_lod'].rolling(holding_period).max().fillna(0)
    return df

def calc_fvg(df):
    df['fvg_bull'] = df['low'] > df['high'].shift(2)
    df['fvg_bear'] = df['high'] < df['low'].shift(2)
    return df

def calc_ict(df, holding=12):
    df['mss_bull'] = ((df['smc_trend'] == 1) & (df['smc_trend'].shift(1) == -1)).astype(int)
    df['mss_bear'] = ((df['smc_trend'] == -1) & (df['smc_trend'].shift(1) == 1)).astype(int)
    df['recent_mss_bull'] = df['mss_bull'].rolling(holding).max().fillna(0)
    df['recent_mss_bear'] = df['mss_bear'].rolling(holding).max().fillna(0)
    df['recent_fvg_bull'] = df['fvg_bull'].astype(int).rolling(holding).max().fillna(0)
    df['recent_fvg_bear'] = df['fvg_bear'].astype(int).rolling(holding).max().fillna(0)
    return df

def calc_sessions(df):
    df['datetime'] = pd.to_datetime(df['ts'], unit='ms')
    df['hour'] = df['datetime'].dt.hour
    df['minute'] = df['datetime'].dt.minute
    
    is_london_open = (df['hour'] == 7) & (df['minute'] == 0)
    is_ny_open = (df['hour'] == 13) & (df['minute'] == 0)
    
    df['or_high'] = np.where(is_london_open | is_ny_open, df['high'], np.nan)
    df['or_low'] = np.where(is_london_open | is_ny_open, df['low'], np.nan)
    
    df['or_high'] = pd.Series(df['or_high']).ffill()
    df['or_low'] = pd.Series(df['or_low']).ffill()
    
    df['is_active_session'] = df['hour'].isin([7, 8, 9, 10, 13, 14, 15, 16])
    
    df['sweep_or_low'] = (df['low'] < df['or_low']) & (df['close'] > df['or_low']) & df['is_active_session']
    df['sweep_or_high'] = (df['high'] > df['or_high']) & (df['close'] < df['or_high']) & df['is_active_session']
    
    df['recent_or_sweep_low'] = df['sweep_or_low'].rolling(12).max().fillna(0)
    df['recent_or_sweep_high'] = df['sweep_or_high'].rolling(12).max().fillna(0)
    
    return df

# ── 🧠 PHASE 2: MATCHED EXECUTION BRUTE FORCE OPTIMIZER ────────────
def calculate_historical_edge(df, min_trades=75):
    algo_l = (df['tL'].shift(1) > df['tL'].shift(2)) & (df['tL'].shift(2) <= df['tL'].shift(3))
    algo_s = (df['tL'].shift(1) < df['tL'].shift(2)) & (df['tL'].shift(2) >= df['tL'].shift(3))
    smc_t = df['smc_trend'].shift(1)
    rsi = df['rsi_14'].shift(1)
    close = df['close'].shift(1)

    regimes = {
        'Regime 1 (Pure Standard)': (algo_l & (smc_t == 1), algo_s & (smc_t == -1)),
        'Regime 2 (Pure Inverted)': (algo_s & (smc_t == -1), algo_l & (smc_t == 1))
    }
    
    emas = [9, 15, 20, 21, 50, 100, 200]
    for e in emas:
        ema_col = df[f'ema_{e}'].shift(1)
        regimes[f'Regime 3 (Standard + {e} EMA Sync)'] = (algo_l & (smc_t == 1) & (close > ema_col), algo_s & (smc_t == -1) & (close < ema_col))

    df['hour'] = pd.to_datetime(df['ts'], unit='ms').dt.hour
    df['in_window'] = df['hour'].isin([7, 8, 13, 14]) 
    
    b_long = (df['recent_sweep_lod'].shift(1) > 0) & (df['recent_mss_bull'].shift(1) > 0) & (df['recent_fvg_bull'].shift(1) > 0) & (close > df['ema_20'].shift(1)) & (df['ema_20'].shift(1) > df['ema_50'].shift(1)) & df['macd_flip_bull'].shift(1)
    b_short = (df['recent_sweep_hod'].shift(1) > 0) & (df['recent_mss_bear'].shift(1) > 0) & (df['recent_fvg_bear'].shift(1) > 0) & (close < df['ema_20'].shift(1)) & (df['ema_20'].shift(1) < df['ema_50'].shift(1)) & df['macd_flip_bear'].shift(1)

    regimes['Regime 5 (Beast: SMC + MACD 24/7)'] = (b_long, b_short)
    regimes['Regime 6 (Beast: SMC + MACD Time Window)'] = (b_long & df['in_window'].shift(1), b_short & df['in_window'].shift(1))

    orb_l = (df['recent_or_sweep_low'].shift(1) > 0) & (df['recent_mss_bull'].shift(1) > 0) & (df['recent_fvg_bull'].shift(1) > 0) & (close > df['ema_100'].shift(1)) & df['is_active_session'].shift(1)
    orb_s = (df['recent_or_sweep_high'].shift(1) > 0) & (df['recent_mss_bear'].shift(1) > 0) & (df['recent_fvg_bear'].shift(1) > 0) & (close < df['ema_100'].shift(1)) & df['is_active_session'].shift(1)
    regimes['Regime 7 (ICT: ORB + FVG Retest)'] = (orb_l, orb_s)

    test_multipliers = [1.50, 2.00, 2.50, 3.00]
    best_mult, best_mode, best_exp, best_wr = None, None, 0.0, 0.0
    highest_wr_tracked = -1.0
    rejection_reason = "Insufficient Trade Volume"

    for mode_name, (l_sig, s_sig) in regimes.items():
        indices = df.index[l_sig | s_sig].tolist()
        for sl_m in test_multipliers:
            trades = []
            for idx in indices:
                if idx >= len(df) - 2: continue
                is_l = l_sig[idx]
                entry = df['close'].iloc[idx]
                atr = df['atr_14'].iloc[idx]
                if pd.isna(atr) or atr == 0: continue
                
                sl_dist = atr * sl_m
                cur_sl = entry - sl_dist if is_l else entry + sl_dist
                cat_tp = entry + (2.0 * sl_dist) if is_l else entry - (2.0 * sl_dist)
                
                tr_r = 0.0
                for fwd in range(idx + 1, len(df)):
                    h, l = df['high'].iloc[fwd], df['low'].iloc[fwd]
                    if is_l:
                        if l <= cur_sl:
                            tr_r = -1.0
                            break
                        if h >= cat_tp:
                            tr_r = 2.0
                            break
                    else:
                        if h >= cur_sl:
                            tr_r = -1.0
                            break
                        if l <= cat_tp:
                            tr_r = 2.0
                            break
                if tr_r != 0.0: trades.append(tr_r)
            
            if len(trades) >= min_trades:
                exp = sum(trades) / len(trades)
                wr = (sum(1 for t in trades if t > 0.05) / len(trades)) * 100
                gross_profit = sum(t for t in trades if t > 0)
                gross_loss = abs(sum(t for t in trades if t < 0))
                pf = gross_profit / gross_loss if gross_loss != 0 else gross_profit

                if wr > highest_wr_tracked:
                    highest_wr_tracked = wr
                    if wr <= 40.0: rejection_reason = f"Low WR [WR: {wr:.1f}% | Exp: {exp:.2f}]"
                    elif exp <= 0.27: rejection_reason = f"Low Exp [WR: {wr:.1f}% | Exp: {exp:.2f}]"
                    elif pf <= 1.2: rejection_reason = f"Poor PF [WR: {wr:.1f}% | PF: {pf:.2f}]"
                    else: rejection_reason = "Passed"

                if exp > 0.27 and wr > 40.0 and pf > 1.2 and wr > best_wr:
                    best_exp, best_mult, best_mode, best_wr = exp, sl_m, mode_name, wr

    return best_mult, best_mode, best_exp, best_wr, 0.0, 0.0, rejection_reason

# ── Order Monitoring & Execution ──────────────────────────────────
def pass_sanity_check(symbol, entry_price, sl_distance):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        bid = ob['bids'][0][0] if ob['bids'] else entry_price
        ask = ob['asks'][0][0] if ob['asks'] else entry_price
        spread = ask - bid
        if spread > (sl_distance * 0.10): return False
        return True
    except Exception: return False

def set_isolated_and_leverage(symbol, entry_price, sl_price):
    try:
        leverage = max(1, min(math.floor(1 / (abs(entry_price - sl_price) / entry_price * 1.2)), 10))
        try: exchange.set_margin_mode('isolated', symbol)
        except Exception: pass
        try: exchange.set_leverage(leverage, symbol)
        except Exception: pass
        return leverage
    except Exception: return 10 

def execute_trade(symbol, direction, size, entry, sl, tp):
    side = 'buy' if direction == 'LONG' else 'sell'
    try:
        f_sz = float(exchange.amount_to_precision(symbol, size))
        f_sl, f_tp = str(float(exchange.price_to_precision(symbol, sl))), str(float(exchange.price_to_precision(symbol, tp)))
        f_px = float(exchange.price_to_precision(symbol, entry))
        set_isolated_and_leverage(symbol, entry, sl)
        order = exchange.create_order(symbol=symbol, type='limit', side=side, amount=f_sz, price=f_px, 
            params={'stopLoss': f_sl, 'takeProfit': f_tp, 'tpslMode': 'Full', 'slOrderType': 'Market', 'tpOrderType': 'Market', 'timeInForce': 'GTC'})
        return order, f_sz, float(f_sl), float(f_tp)
    except Exception: return None, None, None, None

def handle_closed_trade(sym, pos):
    time.sleep(5) 
    try:
        recs = exchange.private_get_v5_position_closed_pnl({'category': 'linear', 'symbol': exchange.market(sym)['id'], 'limit': 3}).get('result', {}).get('list', [])
        pnl, now_ms = None, time.time() * 1000
        if recs:
            for r in recs:
                close_time = float(r.get('updatedTime', r.get('createdTime', 0)))
                if (now_ms - close_time) < (5 * 60 * 1000): 
                    pnl = float(r.get('closedPnl', 0.0))
                    break
        if pnl is None:
            size = pos['risk_usd'] / pos['sl_distance']
            pnl = (pos['current_sl'] - pos['entry']) * size if pos['direction'] == 'LONG' else (pos['entry'] - pos['current_sl']) * size
            pnl -= (size * pos['entry'] * 0.0011) 
        record_closed_pnl(sym, pnl)
        emoji = "💰" if pnl > 0 else "🩸"
        send_telegram(f"{emoji} <b>TRADE SETTLED: {sym.split('/')[0]}</b>\nOutcome: {pnl:+.2f} USD")
    except Exception: pass

def fast_management():
    if not pending_orders and not open_positions: return
    try:
        live_syms = {p['symbol'] for p in exchange.fetch_positions() if float(p.get('contracts', 0)) > 0}
        for sym in list(pending_orders.keys()):
            if sym in live_syms:
                p = pending_orders.pop(sym)
                open_positions[sym] = p
                direction_emoji = "🟢" if p['direction'] == 'LONG' else "🔴"
                msg = (f"🚨 {direction_emoji} {p['direction']} EXECUTION: {sym.split('/')[0]}\n"
                       f"Entry: {p['entry']:.5f}\nSL: {p['current_sl']}\nTP (2R): {p['take_profit']:.5f}\n"
                       f"Regime: {p['mode']}\nWR: {p['win_rate']:.1f}%")
                send_telegram(msg)
        for sym in list(open_positions.keys()):
            if sym not in live_syms:
                pos = open_positions.pop(sym)
                handle_closed_trade(sym, pos)
    except Exception: pass

def check_signal():
    if is_kill_switch_active(): return
    scan_market_radar(); fast_management()
    if len(open_positions) + len(pending_orders) >= MAX_CONCURRENT: return
    today_pnl = daily_pnl_tracker.get(date.today(), 0.0)

    for symbol in active_watchlist:
        if symbol in open_positions or symbol in pending_orders: continue
        df = fetch_deep_data(symbol, '15m', 6000)
        if df is None or len(df) < 3000: continue
        df['atr_14'] = calc_atr(df, ATR_PERIOD)
        df['tL'] = algoalpha_baseline(df)
        df = calc_smc_structure(df)
        df['rsi_14'] = calc_rsi(df['close'])
        
        for e in [9, 15, 20, 21, 50, 100, 200]: df[f'ema_{e}'] = df['close'].ewm(span=e, adjust=False).mean()
        
        df = calc_sweeps(df); df = calc_fvg(df); df = calc_ict(df); df = calc_macd(df); df = calc_sessions(df)
        
        opt_sl_m, mode, exp, wr, roi, pf, reason = calculate_historical_edge(df, min_trades=75)
        if not opt_sl_m: 
            print(f"  🚫 {symbol.split('/')[0]} REJECTED: {reason}")
            continue
        
        print(f"  🌟 {symbol.split('/')[0]} APPROVED! WR: {wr:.1f}% | Mode: {mode}")
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
        c15m = df.iloc[-2]; price = float(df.iloc[-1]['close'])
        atr, smc_t, bar_ts = float(c15m['atr_14']), int(c15m['smc_trend']), int(c15m['ts'])
        if last_trade_bar.get(symbol) == bar_ts: continue

        volume_surge = float(c15m['volume']) > (float(c15m['vol_ma']) * 1.1)
        if not volume_surge: continue

        algo_l = (float(df['tL'].iloc[-2]) > float(df['tL'].iloc[-3]))
        direction = 'LONG' if algo_l else 'SHORT' 
        
        if 'Regime 5' in mode or 'Regime 6' in mode:
            b_l = (float(df['recent_sweep_lod'].iloc[-2]) > 0) and (float(df['recent_mss_bull'].iloc[-2]) > 0) and (float(df['recent_fvg_bull'].iloc[-2]) > 0) and (float(df['close'].iloc[-2]) > float(df['ema_20'].iloc[-2])) and (float(df['macd_flip_bull'].iloc[-2]))
            direction = 'LONG' if b_l else 'SHORT'
        elif 'Regime 7' in mode:
            orb_l = (float(df['recent_or_sweep_low'].iloc[-2]) > 0) and (float(df['recent_mss_bull'].iloc[-2]) > 0) and (float(df['recent_fvg_bull'].iloc[-2]) > 0) and (float(df['close'].iloc[-2]) > float(df['ema_100'].iloc[-2]))
            direction = 'LONG' if orb_l else 'SHORT'

        sl_p = price - (opt_sl_m * atr) if direction == 'LONG' else price + (opt_sl_m * atr)
        tp_p = price + (opt_sl_m * atr * 2.0) if direction == 'LONG' else price - (opt_sl_m * atr * 2.0)
        sl_d = abs(price - sl_p)
        
        if not pass_sanity_check(symbol, price, sl_d): continue

        risk = (P1_RISK if CURRENT_PHASE == 1 else P2_RISK) * (HOUSE_MONEY_MULTIPLIER if today_pnl >= HOUSE_MONEY_THRESHOLD else 1.0)
        order, f_size, f_sl, f_tp = execute_trade(symbol, direction, risk / sl_d, price, sl_p, tp_p)
        if order:
            last_trade_bar[symbol] = bar_ts
            pending_orders[symbol] = {'symbol': symbol, 'direction': direction, 'entry': price, 'atr': atr, 'current_sl': f_sl, 'take_profit': f_tp, 'sl_distance': sl_d, 'risk_usd': risk, 'mode': mode, 'win_rate': wr, 'expectancy': exp}

def run_threaded(job_func): threading.Thread(target=job_func).start()

if __name__ == '__main__':
    send_telegram("🤖 <b>Apex Beast V8.4 (Crypto Only) ONLINE</b>\n📡 Target: Top 50 Cryptos\n🛑 Blocklist: Metals, Indices & Forex Disabled")
    threading.Thread(target=check_signal).start()
    schedule.every(30).seconds.do(fast_management)
    schedule.every(15).minutes.at(":00").do(run_threaded, check_signal) 
    schedule.every().day.at("00:05").do(run_threaded, send_eod_summary)
    while True:
        schedule.run_pending()
        time.sleep(1)
