import ccxt
import pandas as pd
import numpy as np
import requests
import schedule
import time
import math
import json
import threading
import os
from datetime import datetime, timezone, date

# ── Credentials & Config ───────────────────────────────────────────
BYBIT_API_KEY    = "FOqGNCN6gRxu4bqMqF"      
BYBIT_API_SECRET = "YmSWYNkQbVXYiFU5v0G3y3R405VLREGu7icy"   
TELEGRAM_BOT_TOKEN = "8734785957:AAGzU-KPRY4mzXARxyTpLSHGemFtJ7AEsUQ"  
TELEGRAM_CHAT_ID   = "1932328527"               

CURRENT_PHASE     = 1        
DAILY_KILL_SWITCH = -180.0   
MAX_CONCURRENT    = 5        
FEE_CAP_FRAC      = 0.40     

# 🔥 HOUSE MONEY & RADAR CONFIG
HOUSE_MONEY_THRESHOLD  = 60.0  
HOUSE_MONEY_MULTIPLIER = 1.5   
RADAR_MIN_VOLUME       = 75000000  
RADAR_TOP_COINS        = 15        
P1_RISK = 25.0                     
P2_RISK = 25.0
STATE_FILE = "bot_state.json"

# ── Runtime State ──────────────────────────────────────────────────
open_positions       = {}
pending_orders       = {}  
daily_pnl_tracker    = {}
last_trade_bar       = {}  
active_watchlist     = []
edge_cooldowns       = {}  
approved_coins       = {}  
is_scanning          = False

# ── Exchange & Persistence ─────────────────────────────────────────
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.enable_demo_trading(True) 
exchange.load_markets()

def save_state():
    with open(STATE_FILE, 'w') as f:
        json.dump({'open': open_positions, 'pending': pending_orders}, f)

def load_state():
    global open_positions, pending_orders
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
            open_positions = data.get('open', {})
            pending_orders = data.get('pending', {})

# ── Telegram ───────────────────────────────────────────────────────
def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try: requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text.strip(), 'parse_mode': 'HTML'}, timeout=10)
    except Exception: pass

def is_kill_switch_active() -> bool:
    return daily_pnl_tracker.get(date.today(), 0.0) <= DAILY_KILL_SWITCH

def record_closed_pnl(pnl_usd: float):
    today = date.today()
    daily_pnl_tracker[today] = daily_pnl_tracker.get(today, 0.0) + pnl_usd

# ── 🧠 INSTITUTIONAL TOOLKIT & DATA ────────────────────────────────
def fetch_deep_data(symbol, timeframe='15m', target_limit=3000):
    try:
        since = exchange.milliseconds() - (target_limit * 15 * 60 * 1000)
        all_ohlcv = []
        while len(all_ohlcv) < target_limit:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not ohlcv: break
            since = ohlcv[-1][0] + 1
            all_ohlcv.extend(ohlcv)
            time.sleep(0.1)
        df = pd.DataFrame(all_ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        return df
    except Exception: return None

def add_vwap(df):
    df['datetime'] = pd.to_datetime(df['ts'], unit='ms')
    df['date'] = df['datetime'].dt.date
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['pv'] = df['typical_price'] * df['volume']
    df['daily_vwap'] = df.groupby('date')['pv'].cumsum() / df.groupby('date')['volume'].cumsum()
    return df

def add_fvg_obv(df):
    df['fvg_bull'] = df['low'] > df['high'].shift(2)
    df['fvg_bear'] = df['high'] < df['low'].shift(2)
    df['obv'] = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
    df['obv_ema'] = df['obv'].ewm(span=20, adjust=False).mean()
    return df

def add_squeeze(df, length=20):
    df['basis'] = df['close'].rolling(length).mean()
    dev = 2.0 * df['close'].rolling(length).std()
    df['bb_upper'] = df['basis'] + dev
    df['bb_lower'] = df['basis'] - dev
    tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1)
    df['kc_upper'] = df['basis'] + (tr.rolling(length).mean() * 1.5)
    df['kc_lower'] = df['basis'] - (tr.rolling(length).mean() * 1.5)
    df['squeeze_on'] = (df['bb_upper'] < df['kc_upper']) & (df['bb_lower'] > df['kc_lower'])
    return df

def get_htf_trend(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, '4h', limit=100)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ema_50'] = df['close'].ewm(span=50).mean()
        return "BULLISH" if df['close'].iloc[-1] > df['ema_50'].iloc[-1] else "BEARISH"
    except Exception: return "UNKNOWN"

# ── 🧠 REGIME OPTIMIZER (WITH ROI, PF & FAILURE TRACKING) ──────────
def calculate_historical_edge(df, min_trades=50):
    df['atr'] = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs()], axis=1).max(axis=1).ewm(span=14).mean()
    
    # Strictly Uninverted Directional Signals
    l_fvg, s_fvg = df['fvg_bull'] & (df['close'] > df['daily_vwap']), df['fvg_bear'] & (df['close'] < df['daily_vwap'])
    l_obv, s_obv = (df['obv'] > df['obv_ema']) & (df['close'] > df['daily_vwap']), (df['obv'] < df['obv_ema']) & (df['close'] < df['daily_vwap'])
    l_sqz, s_sqz = df['squeeze_on'] & (df['close'] > df['daily_vwap']), df['squeeze_on'] & (df['close'] < df['daily_vwap'])

    regimes = {
        'Regime 1 (FVG Sniper)': (l_fvg.shift(1), s_fvg.shift(1)),
        'Regime 2 (OBV Breakout)': (l_obv.shift(1), s_obv.shift(1)),
        'Regime 3 (Squeeze Trend)': (l_sqz.shift(1), s_sqz.shift(1))
    }
    
    test_multipliers = [1.50, 2.00, 2.50]
    best_mult, best_mode, best_exp, best_wr, best_pf, best_roi = None, None, 0.0, 0.0, 0.0, 0.0
    best_overall = {'exp': -99.0, 'reason': "No valid setups found"}
    
    for mode_name, (l_sig, s_sig) in regimes.items():
        indices = df.index[l_sig | s_sig].tolist()
        for sl_m in test_multipliers:
            trades = []
            for idx in indices:
                if idx >= len(df) - 2: continue
                is_l = l_sig[idx]
                entry = df['close'].iloc[idx]
                atr = df['atr'].iloc[idx]
                if pd.isna(atr) or atr == 0: continue
                sl_dist = atr * sl_m
                cur_sl = entry - sl_dist if is_l else entry + sl_dist
                cat_tp = entry + (10.0 * atr) if is_l else entry - (10.0 * atr)
                best_px = entry
                tr_r = 0.0
                
                for fwd in range(idx + 1, len(df)):
                    h, l = df['high'].iloc[fwd], df['low'].iloc[fwd]
                    if is_l:
                        if l <= cur_sl: tr_r = (cur_sl - entry) / sl_dist; break
                        if h >= cat_tp: tr_r = (cat_tp - entry) / sl_dist; break
                        best_px = max(best_px, h)
                        # Backtest trailing logic updated to 1:1 R -> 0.10x ATR
                        if (best_px - entry) >= sl_dist:
                            cur_sl = max(cur_sl, best_px - (0.10 * atr))
                    else:
                        if h >= cur_sl: tr_r = (entry - cur_sl) / sl_dist; break
                        if l <= cat_tp: tr_r = (entry - cat_tp) / sl_dist; break
                        best_px = min(best_px, l)
                        # Backtest trailing logic updated to 1:1 R -> 0.10x ATR
                        if (entry - best_px) >= sl_dist:
                            cur_sl = min(cur_sl, best_px + (0.10 * atr))
                if tr_r != 0.0: trades.append(tr_r)
            
            if len(trades) >= min_trades:
                exp = sum(trades) / len(trades)
                wr = (sum(1 for t in trades if t > 0.05) / len(trades)) * 100
                gross_profit = sum(t for t in trades if t > 0)
                gross_loss = abs(sum(t for t in trades if t < 0))
                pf = gross_profit / gross_loss if gross_loss > 0 else 99.0
                total_roi = sum(trades)

                # Track best failed reason
                if exp > best_overall['exp']:
                    best_overall['exp'] = exp
                    if exp <= 0.45: best_overall['reason'] = f"Low Exp (+{exp:.2f}R)"
                    elif wr <= 42.0: best_overall['reason'] = f"Low WR ({wr:.1f}%)"
                    elif pf <= 1.5: best_overall['reason'] = f"Low PF ({pf:.2f})"
                    elif total_roi <= 20.0: best_overall['reason'] = f"Low ROI (+{total_roi:.1f}R)"
                    else: best_overall['reason'] = "" # Passed all filters

                # Approve if it passes strict filters
                if exp > 0.45 and wr > 42.0 and pf > 1.5 and total_roi > 20.0 and exp > best_exp:
                    best_exp, best_mult, best_mode, best_wr, best_pf, best_roi = exp, sl_m, mode_name, wr, pf, total_roi

    if best_exp > 0:
        return best_mult, best_mode, best_exp, best_wr, best_roi, ""
    return None, None, None, None, None, best_overall['reason']

# ── Radar & Execution ──────────────────────────────────────────────
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
            if symbol in edge_cooldowns: continue 
            qv, lp, h24, l24 = float(data.get('quoteVolume', 0)), float(data.get('last', 0)), float(data.get('high', 0)), float(data.get('low', 0))
            if lp == 0 or l24 == 0 or qv < RADAR_MIN_VOLUME: continue
            valid_coins.append({'symbol': symbol, 'volatility': (h24 - l24) / l24, 'volume': qv})
            
        valid_coins.sort(key=lambda x: x['volume'], reverse=True)
        top_liquid_50 = valid_coins[:50] 
        top_liquid_50.sort(key=lambda x: x['volatility'], reverse=True)
        
        global active_watchlist
        active_watchlist = [c['symbol'] for c in top_liquid_50[:RADAR_TOP_COINS]]
        print(f"🎯 [RADAR LOCK] Liquid Targets: {[s.split('/')[0] for s in active_watchlist]}")
    except Exception: pass

def execute_trade(symbol, direction, size, entry, sl, tp):
    side = 'buy' if direction == 'LONG' else 'sell'
    try:
        f_sz = float(exchange.amount_to_precision(symbol, size))
        f_sl, f_tp = str(float(exchange.price_to_precision(symbol, sl))), str(float(exchange.price_to_precision(symbol, tp)))
        f_px = float(exchange.price_to_precision(symbol, entry))
        exchange.set_margin_mode('isolated', symbol)
        exchange.set_leverage(10, symbol)
        order = exchange.create_order(symbol=symbol, type='limit', side=side, amount=f_sz, price=f_px, 
            params={'stopLoss': f_sl, 'takeProfit': f_tp, 'tpslMode': 'Full', 'slOrderType': 'Market', 'tpOrderType': 'Market', 'timeInForce': 'GTC'})
        return order, f_sz, float(f_sl), float(f_tp)
    except Exception: return None, None, None, None

# ── ⚡ 1-MINUTE FAST MANAGEMENT ────────────────────────────────────
def fast_management():
    if not pending_orders and not open_positions: return
    try:
        live_positions = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live_positions if float(p.get('contracts', 0)) > 0}

        # Sync New Orders & Send Alert
        for sym in list(pending_orders.keys()):
            if sym in live_syms:
                p = pending_orders.pop(sym)
                open_positions[sym] = p
                save_state()
                dir_icon = "🟢 LONG" if p['direction'] == 'LONG' else "🔴 SHORT"
                msg = (f"🚨 {dir_icon} EXECUTION: {sym.split('/')[0]}\n"
                       f"Entry: {p['entry']:.5f}\n"
                       f"Stop Loss: {p['current_sl']}\n"
                       f"Break Even At: {p['be_price']:.5f}\n"
                       f"Regime: {p['mode']}\n"
                       f"Backtest: WR {p['win_rate']:.1f}% | Exp +{p['expectancy']:.2f}R | ROI +{p['roi']:.2f}R")
                send_telegram(msg)
        
        # Sync Closed
        for sym in list(open_positions.keys()):
            if sym not in live_syms:
                del open_positions[sym]
                save_state()
                continue

        # Live Trailing Logic (1:1 R -> 0.10x ATR Trail)
        for symbol, pos in list(open_positions.items()):
            df = exchange.fetch_ohlcv(symbol, '1m', limit=5)
            if not df: continue
            current_price = df[-1][4]
            is_l = pos['direction'] == 'LONG'
            entry, sl_dist = pos['entry'], pos['sl_distance']
            
            # Track highest/lowest price seen for accurate trailing
            if is_l: pos['best_price'] = max(pos.get('best_price', entry), current_price)
            else: pos['best_price'] = min(pos.get('best_price', entry), current_price)
            
            # SANITY CHECK 1: Time-Based Exit (24H / 86400s)
            if time.time() - pos.get('entry_time', time.time()) > 86400:
                exchange.create_market_order(symbol, 'sell' if is_l else 'buy', pos['size'], params={'reduceOnly': True})
                send_telegram(f"⏰ <b>TIME STOP HIT: {symbol}</b> (24H elapsed)")
                continue

            # NEW LOGIC: Trailing Stop at 1:1 RR distance
            diff = abs(pos['best_price'] - entry)
            if diff >= sl_dist:
                if is_l:
                    trail_sl = pos['best_price'] - (0.10 * pos
