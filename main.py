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
RADAR_TOP_COINS        = 10        # How many coins to hunt simultaneously
P1_RISK = 30.0
P2_RISK = 20.0

# ── AlgoAlpha Indicator Constants ─────────────────────────────────
ST_FACTOR  = 2.0    
ST_PERIOD  = 14     
WMA_LENGTH = 14     
EMA_LENGTH = 3      
ATR_PERIOD = 14     
BYBIT_MAKER_FEE = 0.00020  # Switched to Maker fee since we use Limit Orders

# ── Runtime State ──────────────────────────────────────────────────
open_positions       = {}
pending_orders       = {}  # Waiting room for 14-min limit orders
daily_pnl_tracker    = {}
last_trade_bar       = {}  
active_watchlist     = []

# ── Exchange ───────────────────────────────────────────────────────
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.enable_demo_trading(True) # NOTE: Turn to False for live funds
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

# ── 🧠 CONTINUOUS MARKET RADAR ──────────────────────────────────────
def scan_market_radar():
    print("📡 [RADAR] Sweeping Bybit USDT Perpetuals for momentum...")
    try:
        tickers = exchange.fetch_tickers()
        valid_coins = []
        
        for symbol, data in tickers.items():
            if not symbol.endswith(':USDT'): continue
            
            quote_volume = float(data.get('quoteVolume', 0))
            last_price = float(data.get('last', 0))
            high_24 = float(data.get('high', 0))
            low_24 = float(data.get('low', 0))
            
            if last_price == 0 or low_24 == 0: continue
            if quote_volume < RADAR_MIN_VOLUME: continue
            
            volatility_score = (high_24 - low_24) / low_24
            valid_coins.append({'symbol': symbol, 'volatility': volatility_score})
            
        valid_coins.sort(key=lambda x: x['volatility'], reverse=True)
        top_coins = [c['symbol'] for c in valid_coins[:RADAR_TOP_COINS]]
        
        global active_watchlist
        active_watchlist = top_coins
        print(f"🎯 [RADAR LOCK] Tracking Top {len(active_watchlist)} Markets: {[s.split('/')[0] for s in active_watchlist]}")
        
    except Exception as e:
        print(f"❌ [RADAR ERROR] Market sweep failed: {e}")

# ── Indicators (SMC & AlgoAlpha) ───────────────────────────────────
def fetch_data(symbol, timeframe='15m', limit=300):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
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

# ── Auto-Leverage & Margin ─────────────────────────────────────────
def set_isolated_and_leverage(symbol, entry_price, sl_price):
    try:
        sl_percent = abs(entry_price - sl_price) / entry_price
        # 1.2 buffer to keep liquidation further away than SL
        safe_leverage = math.floor(1 / (sl_percent * 1.2)) 
        leverage = max(1, min(safe_leverage, 25))

        try: exchange.set_margin_mode('isolated', symbol)
        except Exception: pass
            
        try: exchange.set_leverage(leverage, symbol)
        except Exception: pass
            
        print(f"  ⚙️ {symbol.split('/')[0]} Margin: ISOLATED | Auto-Leverage: {leverage}x")
        return leverage
    except Exception as e:
        print(f"  [Margin Error] {e}")
        return 10 

# ── Limit Execution (Maker Fees) ───────────────────────────────────
def execute_trade(symbol, direction, size, entry, sl, tp):
    side = 'buy' if direction == 'LONG' else 'sell'
    try:
        fmt_size = float(exchange.amount_to_precision(symbol, size))
        
        # Strings strictly required by Bybit API for orders
        fmt_sl   = str(float(exchange.price_to_precision(symbol, sl)))
        fmt_tp   = str(float(exchange.price_to_precision(symbol, tp)))
        fmt_price = float(exchange.price_to_precision(symbol, entry))

        set_isolated_and_leverage(symbol, entry, sl)

        order = exchange.create_order(
            symbol=symbol, 
            type='limit',  
            side=side, 
            amount=fmt_size,
            price=fmt_price, 
            params={
                'stopLoss': fmt_sl, 
                'takeProfit': fmt_tp, 
                'tpslMode': 'Full',
                'slOrderType': 'Market', 
                'tpOrderType': 'Market', 
                'timeInForce': 'GTC'  # Sits in the book waiting for pullback
            }
        )
        return order, fmt_size, float(fmt_sl), float(fmt_tp)
    except Exception as e:
        print(f"❌ Execution Failed ({symbol}): {e}")
        return None, None, None, None

def modify_bybit_tpsl(symbol, direction, new_sl, current_tp):
    market_id = exchange.market(symbol)['id']
    bybit_side = 'Buy' if direction == 'LONG' else 'Sell'
    try:
        fmt_sl = float(exchange.price_to_precision(symbol, new_sl))
        exchange.privatePostV5PositionTradingStop({
            'category': 'linear', 'symbol': market_id, 'side': bybit_side, 'tpslMode': 'Full',
            'takeProfit': str(current_tp), 'stopLoss': str(fmt_sl), 'slOrderType': 'Market',
            'tpOrderType': 'Market', 'slTriggerBy': 'LastPrice', 'tpTriggerBy': 'LastPrice'
        })
        return fmt_sl
    except Exception: return None

# ── Order Monitoring & Cleanup ─────────────────────────────────────
def monitor_pending_orders():
    if not pending_orders: return
    try:
        live_positions = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live_positions if float(p.get('contracts', 0)) > 0}

        for sym in list(pending_orders.keys()):
            if sym in live_syms:
                # Order filled! Move to open positions
                pos_data = pending_orders.pop(sym)
                open_positions[sym] = pos_data

                long_signal = pos_data['direction'] == 'LONG'
                send_telegram(
                    f"<b>✅ TRADE FILLED — APEX BEAST</b>\n"
                    f"{'🟢 ▲ LONG' if long_signal else '🔴 ▼ SHORT'} <b>{sym.split('/')[0]}</b>\n\n"
                    f"<b>Entry  :</b> <code>{pos_data['entry']:.6f}</code>\n"
                    f"<b>SL     :</b> <code>{pos_data['current_sl']}</code> (1R)\n"
                    f"<b>Target :</b> 1:2 Strict RR 🚀\n"
                    f"<b>Risk   :</b> ${pos_data['risk_usd']:.0f}\n"
                )
                print(f"  ✅ Filled limit order for {sym.split('/')[0]}")
    except Exception: pass

def cancel_stale_orders():
    if not pending_orders: return
    try:
        open_orders = exchange.fetch_open_orders()
        for order in open_orders:
            sym = order['symbol']
            if sym in pending_orders:
                order_time = order['timestamp']
                now = exchange.milliseconds()
                
                # 14 Minutes = 840,000 milliseconds
                if (now - order_time) > 840000:
                    exchange.cancel_order(order['id'], sym)
                    pending_orders.pop(sym, None)
                    print(f"  🗑️ 14-min Limit order expired silently for {sym.split('/')[0]}")
    except Exception: pass

def sync_open_positions():
    if not open_positions: return
    try:
        live_positions = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live_positions if float(p.get('contracts', 0)) > 0}
        closed = [sym for sym in list(open_positions.keys()) if sym not in live_syms]

        for sym in closed:
            open_positions.pop(sym)
            market_id = exchange.market(sym)['id']
            try:
                records = exchange.private_get_v5_position_closed_pnl({'category': 'linear', 'symbol': market_id, 'limit': 1}).get('result', {}).get('list', [])
                if records:
                    exact_pnl = float(records[0].get('closedPnl', 0.0))
                    record_closed_pnl(exact_pnl)
                    send_telegram(f"{'✅' if exact_pnl > 0 else '❌'} <b>TRADE CLOSED — {sym.split('/')[0]}</b>\nSettled Net PnL: <code>${exact_pnl:.2f}</code>")
            except Exception: pass
    except Exception: pass

# ── Strict 1R/2R Engine ────────────────────────────────────────────
def manage_trailing_stops():
    if not open_positions: return
    for symbol, pos in list(open_positions.items()):
        df = fetch_data(symbol, '1m', 5)
        if df is None or len(df) < 1: continue

        live_bar = df.iloc[-1]
        high_now, low_now, live_price = float(live_bar['high']), float(live_bar['low']), float(live_bar['close'])
        is_long = pos['direction'] == 'LONG'
        
        trail_dist = 0.10 * pos['atr']
        new_best = max(pos['best_price'], high_now) if is_long else min(pos['best_price'], low_now)
        pos['best_price'] = new_best
        profit_distance = abs(new_best - pos['entry'])
        
        one_r_dist = pos['sl_distance']

        # ── 1. THE 1R FREE RIDE (Break-Even) ──
        if profit_distance >= one_r_dist and not pos.get('free_ride_triggered', False):
            pos['free_ride_triggered'] = True
            be_sl = pos['entry'] * 1.001 if is_long else pos['entry'] * 0.999
            fmt_new_sl = modify_bybit_tpsl(symbol, pos['direction'], be_sl, pos['catastrophic_tp'])
            if fmt_new_sl:
                pos['current_sl'] = fmt_new_sl
                send_telegram(f"🛡️ <b>FREE RIDE SECURED (1R) — {symbol.split('/')[0]}</b>\nRisk removed. SL at Break-Even.")

        # ── 2. THE 2R TARGET SQUASH & DYNAMIC TRAIL ──
        if profit_distance >= (one_r_dist * 2.0):
            if not pos.get('trailing_tp_triggered', False):
                pos['trailing_tp_triggered'] = True
                send_telegram(f"🚀 <b>2R TARGET HIT — {symbol.split('/')[0]}</b>\nStrict 1:2 RR achieved! 0.10x ATR Trail activated.")

            raw_new_sl = (new_best - trail_dist) if is_long else (new_best + trail_dist)
            sl_improved = raw_new_sl > pos['current_sl'] if is_long else raw_new_sl < pos['current_sl']

            if sl_improved:
                if (is_long and raw_new_sl >= live_price) or (not is_long and raw_new_sl <= live_price): continue
                fmt_new_sl = modify_bybit_tpsl(symbol, pos['direction'], raw_new_sl, pos['catastrophic_tp'])
                if fmt_new_sl:
                    pos['current_sl'] = fmt_new_sl
                    locked_r = abs(fmt_new_sl - pos['entry']) / one_r_dist
                    send_telegram(f"<b>🔄 Trail Updated — {symbol.split('/')[0]}</b>\n{'▲' if is_long else '▼'} {pos['direction']}\n<b>New SL :</b> <code>{fmt_new_sl}</code>\n<b>Locked :</b> <code>{locked_r:.2f} R</code>\n")

def fast_management():
    monitor_pending_orders()
    cancel_stale_orders()
    sync_open_positions()
    manage_trailing_stops()

# ── Signal Engine ──────────────────────────────────────────────────
def check_signal():
    ts_now, today = datetime.now(timezone.utc), datetime.now(timezone.utc).date()
    today_pnl = daily_pnl_tracker.get(today, 0.0)

    print(f"[{ts_now.strftime('%H:%M UTC')}] Beast Mode | PnL: ${today_pnl:.2f} | Pending: {len(pending_orders)} | Open: {len(open_positions)}/{MAX_CONCURRENT}")
    if is_kill_switch_active():
        print("  🛑 KILL-SWITCH ACTIVE.")
        manage_trailing_stops()
        return

    scan_market_radar()
    fast_management()

    if len(open_positions) + len(pending_orders) >= MAX_CONCURRENT: return

    for symbol in active_watchlist:
        if symbol in open_positions or symbol in pending_orders: continue

        df = fetch_data(symbol, '15m', 250)
        if df is None or len(df) < 150: continue

        df['atr_14'] = calc_atr(df, ATR_PERIOD)
        df['tL']     = algoalpha_baseline(df)
        df           = calc_smc_structure(df)

        c15m, price = df.iloc[-2], float(df.iloc[-1]['close'])
        atr, smc_trend = float(c15m['atr_14']), int(c15m['smc_trend'])
        signal_bar_ts = int(c15m['ts'])
        
        if last_trade_bar.get(symbol) == signal_bar_ts or atr < (price * 0.00005) or np.isnan(atr): continue

        algo_long  = (float(df['tL'].iloc[-2]) > float(df['tL'].iloc[-3])) and (float(df['tL'].iloc[-3]) <= float(df['tL'].iloc[-4]))
        algo_short = (float(df['tL'].iloc[-2]) < float(df['tL'].iloc[-3])) and (float(df['tL'].iloc[-3]) >= float(df['tL'].iloc[-4]))

        long_signal  = algo_short and (smc_trend == -1)
        short_signal = algo_long and (smc_trend == 1)
        if not long_signal and not short_signal: continue

        base_risk_usd = P1_RISK if CURRENT_PHASE == 1 else P2_RISK
        risk_usd = base_risk_usd * HOUSE_MONEY_MULTIPLIER if today_pnl >= HOUSE_MONEY_THRESHOLD else base_risk_usd
        
        direction = 'LONG' if long_signal else 'SHORT'
        
        # Initial SL distance logic (1.50x ATR multiplier)
        raw_sl = price - (1.50 * atr) if long_signal else price + (1.50 * atr)
        catastrophic_tp = price + 10.0 * atr if long_signal else price - 10.0 * atr

        sl_distance = abs(price - raw_sl)
        raw_lot_size = risk_usd / sl_distance if sl_distance > 0 else 0
        
        # Calculate Maker Fees for the fee cap check
        if raw_lot_size * price * BYBIT_MAKER_FEE * 2 > (risk_usd * FEE_CAP_FRAC): continue

        order, fmt_size, fmt_sl, fmt_tp = execute_trade(symbol, direction, raw_lot_size, price, raw_sl, catastrophic_tp)
        if not order: continue

        last_trade_bar[symbol] = signal_bar_ts
        
        # Save to PENDING silently
        pending_orders[symbol] = {
            'direction': direction, 'entry': price, 'atr': atr, 'best_price': price,
            'current_sl': fmt_sl, 'catastrophic_tp': fmt_tp, 
            'sl_distance': sl_distance, 
            'free_ride_triggered': False, 'trailing_tp_triggered': False,
            'risk_usd': risk_usd
        }
        
        print(f"  ⏳ Pending Limit Order placed silently for {symbol.split('/')[0]} at {price:.6f}")

def daily_reset():
    yesterday = datetime.now(timezone.utc).date()
    final_pnl = daily_pnl_tracker.get(yesterday, 0.0)
    send_telegram(f"<b>📅 Daily Reset</b>\nYesterday PnL: <code>${final_pnl:.2f}</code>\nKill-switch was {'🛑 ACTIVE' if final_pnl <= DAILY_KILL_SWITCH else '✅ NOT triggered'}")
    daily_pnl_tracker.clear()

if __name__ == '__main__':
    send_telegram(
        f"<b>🤑 APEX BEAST V7.3 Online</b>\nContinuous Radar & Maker Orders\n\n"
        f"Kill-Switch ${DAILY_KILL_SWITCH}/day\n"
        "🔀 DYNAMIC 5-MIN WATCHLIST\n🔥 HOUSE MONEY ACTIVE\n"
        "⚡ STRICT 1R FREE-RIDE / 2R TRAIL"
    )
    
    check_signal()
    schedule.every(1).minutes.do(fast_management)
    schedule.every(5).minutes.at(":00").do(check_signal) 
    schedule.every().day.at("00:05").do(daily_reset)
    
    while True:
        try: schedule.run_pending()
        except Exception as e: print(f"  [loop error] {e}", flush=True)
        time.sleep(1)
