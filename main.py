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
# ── Prop Firm 5k Calibration ──
DAILY_KILL_SWITCH = -180.0   
MAX_CONCURRENT    = 5        
FEE_CAP_FRAC      = 0.40     

# 🔥 HOUSE MONEY & RADAR CONFIG
HOUSE_MONEY_THRESHOLD  = 60.0  
HOUSE_MONEY_MULTIPLIER = 1.5   
RADAR_MIN_VOLUME       = 75000000  # 75M Liquidity Gate
RADAR_TOP_COINS        = 25        
P1_RISK = 25.0                     
P2_RISK = 25.0

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
daily_trade_stats    = {'total_trades': 0, 'wins': 0, 'coin_pnl': {}} # EOD Tracking
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
    
    # EOD Tracking
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
    
    # Clear stats for the new day
    daily_pnl_tracker.clear()
    daily_trade_stats['total_trades'] = 0
    daily_trade_stats['wins'] = 0
    daily_trade_stats['coin_pnl'].clear()
    approved_coins.clear()
    edge_cooldowns.clear()

# ── 🧠 CONTINUOUS MARKET RADAR (Liquid Momentum Upgrade) ───────────
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
        print(f"🎯 [RADAR LOCK] Liquid Targets: {[s.split('/')[0] for s in active_watchlist]}")
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

# ── 🧠 PHASE 2: MATCHED EXECUTION BRUTE FORCE OPTIMIZER ────────────
def calculate_historical_edge(df, min_trades=100):
    algo_l = (df['tL'].shift(1) > df['tL'].shift(2)) & (df['tL'].shift(2) <= df['tL'].shift(3))
    algo_s = (df['tL'].shift(1) < df['tL'].shift(2)) & (df['tL'].shift(2) >= df['tL'].shift(3))
    smc_t = df['smc_trend'].shift(1)
    rsi = df['rsi_14'].shift(1)
    close = df['close'].shift(1)

    l_std, s_std = algo_l & (smc_t == 1), algo_s & (smc_t == -1)
    l_inv, s_inv = algo_s & (smc_t == -1), algo_l & (smc_t == 1)

    regimes = {
        'Regime 1 (Pure Standard)': (l_std, s_std),
        'Regime 2 (Pure Inverted)': (l_inv, s_inv)
    }
    
    emas = [9, 15, 20, 21, 50, 200]
    for e in emas:
        ema_col = df[f'ema_{e}'].shift(1)
        regimes[f'Regime 3 (Standard + {e} EMA Sync)'] = (l_std & (close > ema_col), s_std & (close < ema_col))
        regimes[f'Regime 4 (Inverted + {e} EMA Sync)'] = (l_inv & (close > ema_col), s_inv & (close < ema_col))

    regimes['Regime 3 (Standard + RSI Momentum)'] = (l_std & (rsi > 50), s_std & (rsi < 50))
    regimes['Regime 4 (Inverted + RSI Momentum)'] = (l_inv & (rsi > 50), s_inv & (rsi < 50))
    regimes['Regime 3 (Standard + RSI Exhaustion)'] = (l_std & (rsi < 40), s_std & (rsi > 60))
    regimes['Regime 4 (Inverted + RSI Exhaustion)'] = (l_inv & (rsi < 40), s_inv & (rsi > 60))
    
    test_multipliers = [1.50, 2.00, 2.50, 3.00]
    best_mult, best_mode, best_exp, best_wr, best_roi, best_pf = None, None, 0.0, 0.0, 0.0, 0.0
    
    highest_exp = -999.0
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
                be_price = entry * 1.002 if is_l else entry * 0.998
                cat_tp = entry + (10.0 * atr) if is_l else entry - (10.0 * atr)
                best_px = entry
                be_triggered = False
                tr_r = 0.0
                for fwd in range(idx + 1, len(df)):
                    h, l = df['high'].iloc[fwd], df['low'].iloc[fwd]
                    if is_l:
                        if l <= cur_sl:
                            tr_r = (cur_sl - entry) / sl_dist
                            break
                        if h >= cat_tp:
                            tr_r = (cat_tp - entry) / sl_dist
                            break
                        best_px = max(best_px, h)
                        if (best_px - entry) >= sl_dist and not be_triggered:
                            be_triggered = True
                            cur_sl = max(cur_sl, be_price)
                        if (best_px - entry) >= (sl_dist * 1.5):
                            cur_sl = max(cur_sl, best_px - (0.10 * atr))
                    else:
                        if h >= cur_sl:
                            tr_r = (entry - cur_sl) / sl_dist
                            break
                        if l <= cat_tp:
                            tr_r = (entry - cat_tp) / sl_dist
                            break
                        best_px = min(best_px, l)
                        if (entry - best_px) >= sl_dist and not be_triggered:
                            be_triggered = True
                            cur_sl = min(cur_sl, be_price)
                        if (entry - best_px) >= (sl_dist * 1.5):
                            cur_sl = min(cur_sl, best_px + (0.10 * atr))
                if tr_r != 0.0: trades.append(tr_r)
            
            if len(trades) >= min_trades:
                exp = sum(trades) / len(trades)
                wr = (sum(1 for t in trades if t > 0.05) / len(trades)) * 100
                roi = sum(trades)
                
                gross_profit = sum(t for t in trades if t > 0)
                gross_loss = abs(sum(t for t in trades if t < 0))
                pf = gross_profit / gross_loss if gross_loss != 0 else gross_profit

                # 0.29 Expectancy update & WR/PF Logging
                if exp > highest_exp:
                    highest_exp = exp
                    stats_log = f"[WR: {wr:.1f}% | Exp: +{exp:.2f}R | PF: {pf:.2f}]"
                    
                    if wr <= 40.0: rejection_reason = f"Low WR {stats_log}"
                    elif exp <= 0.29: rejection_reason = f"Low Exp {stats_log}"
                    elif pf <= 1.2: rejection_reason = f"Poor PF {stats_log}"
                    else: rejection_reason = f"Passed {stats_log}"

                # PROFITABILITY FILTER (Exp > 0.29, WR > 40%, PF > 1.2)
                if exp > 0.29 and wr > 40.0 and pf > 1.2 and exp > best_exp:
                    best_exp, best_mult, best_mode, best_wr = exp, sl_m, mode_name, wr
                    best_roi, best_pf = roi, pf

    if best_mult:
        return best_mult, best_mode, best_exp, best_wr, best_roi, best_pf, "Passed"
    else:
        return None, None, None, None, None, None, rejection_reason

# ── Order Monitoring & Execution ──────────────────────────────────
def pass_sanity_check(symbol, entry_price, sl_distance):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        bid = ob['bids'][0][0] if ob['bids'] else entry_price
        ask = ob['asks'][0][0] if ob['asks'] else entry_price
        spread = ask - bid
        
        if spread > (sl_distance * 0.10):
            print(f"⚠️ [SANITY FAILED] Spread blow-out on {symbol}. Spread: {spread:.5f} | SL Dist: {sl_distance:.5f}")
            return False
        return True
    except Exception as e:
        print(f"Sanity Check Error: {e}")
        return False

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

def modify_bybit_tpsl(symbol, direction, new_sl, current_tp):
    try:
        f_sl = float(exchange.price_to_precision(symbol, new_sl))
        exchange.privatePostV5PositionTradingStop({'category': 'linear', 'symbol': exchange.market(symbol)['id'], 'side': 'Buy' if direction == 'LONG' else 'Sell', 'tpslMode': 'Full',
            'takeProfit': str(current_tp), 'stopLoss': str(f_sl), 'slOrderType': 'Market', 'tpOrderType': 'Market', 'slTriggerBy': 'LastPrice', 'tpTriggerBy': 'LastPrice'})
        return f_sl
    except Exception: return None

def fast_management():
    if not pending_orders and not open_positions: return
    try:
        live_syms = {p['symbol'] for p in exchange.fetch_positions() if float(p.get('contracts', 0)) > 0}
        
        # Sync Orders & FULL TELEGRAM ALERT
        for sym in list(pending_orders.keys()):
            if sym in live_syms:
                p = pending_orders.pop(sym)
                open_positions[sym] = p
                
                direction_emoji = "🟢" if p['direction'] == 'LONG' else "🔴"
                msg = (f"🚨 {direction_emoji} {p['direction']} EXECUTION: {sym.split('/')[0]}\n"
                       f"Entry: {p['entry']:.5f}\n"
                       f"Stop Loss: {p['current_sl']}\n"
                       f"Break Even At: {p['be_price']:.5f}\n"
                       f"Regime: {p['mode']}\n"
                       f"Backtest: WR {p['win_rate']:.1f}% | Exp +{p['expectancy']:.2f}R | ROI +{p.get('roi', 0):.2f}R")
                send_telegram(msg)
        
        # Sync Closed & PNL TELEGRAM ALERT
        for sym in list(open_positions.keys()):
            if sym not in live_syms:
                pos = open_positions.pop(sym)
                recs = exchange.private_get_v5_position_closed_pnl({'category': 'linear', 'symbol': exchange.market(sym)['id'], 'limit': 1}).get('result', {}).get('list', [])
                if recs:
                    pnl = float(recs[0].get('closedPnl', 0.0))
                    record_closed_pnl(sym, pnl)
                    emoji = "💰" if pnl > 0 else "🩸"
                    send_telegram(f"{emoji} <b>TRADE SETTLED: {sym.split('/')[0]}</b>\nOutcome: {pnl:+.2f} USD")

        # Live Trailing Management
        for symbol, pos in list(open_positions.items()):
            df = fetch_data(symbol, '1m', 5)
            if df is None: continue
            is_l, entry, sl_dist = pos['direction'] == 'LONG', pos['entry'], pos['sl_distance']
            best = max(pos['best_price'], float(df.iloc[-1]['high'])) if is_l else min(pos['best_price'], float(df.iloc[-1]['low']))
            pos['best_price'] = best
            diff = abs(best - entry)
            
            # 1:1 RR -> Break Even & FREE RIDE ALERT
            if diff >= sl_dist and not pos.get('be_on', False):
                pos['be_on'] = True
                modify_bybit_tpsl(symbol, pos['direction'], pos['be_price'], pos['catastrophic_tp'])
                send_telegram(f"🛡️ <b>FREE RIDE SECURED: {symbol.split('/')[0]}</b>\nStop Loss moved to Break Even ({pos['be_price']:.5f}).")
            
            # 1.5:1 RR -> Start Aggressive Trailing
            if diff >= (sl_dist * 1.5):
                trail = (best - (0.10 * pos['atr'])) if is_l else (best + (0.10 * pos['atr']))
                modify_bybit_tpsl(symbol, pos['direction'], trail, pos['catastrophic_tp'])
    except Exception: pass

# ── Signal Engine ──────────────────────────────────────────────────
def check_signal():
    if is_kill_switch_active(): return
    scan_market_radar(); fast_management()
    if len(open_positions) + len(pending_orders) >= MAX_CONCURRENT: return
    today_pnl = daily_pnl_tracker.get(date.today(), 0.0)

    for symbol in active_watchlist:
        if symbol in open_positions or symbol in pending_orders: continue
        
        if symbol in approved_coins:
            df = fetch_data(symbol, '15m', 500)
            conf = approved_coins[symbol]
            opt_sl_m, mode, exp, wr = conf['mult'], conf['mode'], conf['exp'], conf['wr']
            print(f"  🔍 Hunting for {mode} entry on {symbol.split('/')[0]}...")
        else:
            df = fetch_deep_data(symbol, '15m', 6000)
            if df is None or len(df) < 3000: continue
            df['atr_14'] = calc_atr(df, ATR_PERIOD)
            df['tL'] = algoalpha_baseline(df)
            df = calc_smc_structure(df)
            df['rsi_14'] = calc_rsi(df['close'])
            for e in [9, 15, 20, 21, 50, 200]: df[f'ema_{e}'] = df['close'].ewm(span=e, adjust=False).mean()
            
            opt_sl_m, mode, exp, wr, roi, pf, reason = calculate_historical_edge(df, min_trades=100)
            
            if not opt_sl_m: 
                print(f"  🚫 {symbol.split('/')[0]} REJECTED: {reason}. Burned.")
                edge_cooldowns[symbol] = time.time() + 3600
                continue
            print(f"  🌟 {symbol.split('/')[0]} APPROVED! Mode: {mode} | SL: {opt_sl_m}x | WR: {wr:.1f}% | Exp: +{exp:.2f}R | ROI: +{roi:.2f}R | PF: {pf:.2f}")
            approved_coins[symbol] = {'mult': opt_sl_m, 'mode': mode, 'exp': exp, 'wr': wr, 'roi': roi, 'pf': pf}

        df['atr_14'] = calc_atr(df, ATR_PERIOD)
        df['tL'] = algoalpha_baseline(df)
        df = calc_smc_structure(df)
        df['rsi_14'] = calc_rsi(df['close'])
        for e in [9, 15, 20, 21, 50, 200]: df[f'ema_{e}'] = df['close'].ewm(span=e, adjust=False).mean()
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
        
        c15m = df.iloc[-2]
        price = float(df.iloc[-1]['close'])
        atr, smc_t, bar_ts = float(c15m['atr_14']), int(c15m['smc_trend']), int(c15m['ts'])
        if last_trade_bar.get(symbol) == bar_ts: continue

        volume_surge = float(c15m['volume']) > (float(c15m['vol_ma']) * 1.2)

        algo_l = (float(df['tL'].iloc[-2]) > float(df['tL'].iloc[-3])) and (float(df['tL'].iloc[-3]) <= float(df['tL'].iloc[-4]))
        algo_s = (float(df['tL'].iloc[-2]) < float(df['tL'].iloc[-3])) and (float(df['tL'].iloc[-3]) >= float(df['tL'].iloc[-4]))
        rsi = float(c15m['rsi_14'])
        l_std, s_std = algo_l and (smc_t == 1), algo_s and (smc_t == -1)
        l_inv, s_inv = algo_s and (smc_t == -1), algo_l and (smc_t == 1)

        l_sig, s_sig = False, False
        if mode == 'Regime 1 (Pure Standard)': l_sig, s_sig = l_std, s_std
        elif mode == 'Regime 2 (Pure Inverted)': l_sig, s_sig = l_inv, s_inv
        elif 'EMA Sync' in mode:
            e = int(mode.split('+ ')[1].split(' EMA')[0])
            check = price > float(c15m[f'ema_{e}'])
            l_sig, s_sig = (l_std if 'Standard' in mode else l_inv) and check, (s_std if 'Standard' in mode else s_inv) and not check
        elif 'RSI Momentum' in mode:
            l_sig, s_sig = (l_std if 'Standard' in mode else l_inv) and rsi > 50, (s_std if 'Standard' in mode else s_inv) and rsi < 50
        elif 'RSI Exhaustion' in mode:
            l_sig, s_sig = (l_std if 'Standard' in mode else l_inv) and rsi < 40, (s_std if 'Standard' in mode else s_inv) and rsi > 60

        l_sig, s_sig = l_sig and volume_surge, s_sig and volume_surge
        if not l_sig and not s_sig: continue
        
        risk = (P1_RISK if CURRENT_PHASE == 1 else P2_RISK) * (HOUSE_MONEY_MULTIPLIER if today_pnl >= HOUSE_MONEY_THRESHOLD else 1.0)
        direction = 'LONG' if l_sig else 'SHORT'
        sl_p = price - (opt_sl_m * atr) if l_sig else price + (opt_sl_m * atr)
        tp_p = price + (10.0 * atr) if l_sig else price - (10.0 * atr)
        sl_d = abs(price - sl_p)
        
        if not pass_sanity_check(symbol, price, sl_d):
            continue

        order, f_size, f_sl, f_tp = execute_trade(symbol, direction, risk / sl_d, price, sl_p, tp_p)
        if order:
            last_trade_bar[symbol] = bar_ts
            stored_roi = approved_coins.get(symbol, {}).get('roi', 0.0)
            
            pending_orders[symbol] = {
                'symbol': symbol, 'direction': direction, 'entry': price, 'atr': atr, 'best_price': price, 'opt_sl_m': opt_sl_m,
                'current_sl': f_sl, 'catastrophic_tp': f_tp, 'sl_distance': sl_d, 
                'be_price': price * 1.0025 if l_sig else price * 0.9975, 
                'risk_usd': risk, 'mode': mode, 'win_rate': wr, 'expectancy': exp, 'roi': stored_roi
            }

if __name__ == '__main__':
    send_telegram("🤖 <b>Apex Beast V8.1 is ONLINE</b>\n📡 Scanning Top 15 Coins...\n📊 Fast 30s Execution Logs: Active")
    check_signal()
    schedule.every(30).seconds.do(fast_management)
    schedule.every(5).minutes.at(":00").do(check_signal) 
    schedule.every().day.at("00:05").do(send_eod_summary)
    while True:
        schedule.run_pending()
        time.sleep(1)
