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

CURRENT_PHASE     = 1        
DAILY_KILL_SWITCH = -150.0   
DAILY_PROFIT_LOCK = +9999.0  # 🚀 LIFTED
MAX_CONCURRENT    = 999
FEE_CAP_FRAC      = 0.40     # 🎯 PATH D-PLUS: 40% Pareto Sweet Spot

# 🔥 HOUSE MONEY CONFIGURATION
HOUSE_MONEY_THRESHOLD  = 75.0  
HOUSE_MONEY_MULTIPLIER = 1.5   

# ── Per-Symbol Config (🔄 INVERTED STRATEGY MULTIPLIERS) ──────────────────────
PER_SYMBOL_CONFIG = {
    # ── Group 1: SL=0.50× | TP=3.00× | Trail=0.10× | P1=$30 | P2=$20 ─────────
    'ETH/USDT:USDT':    (0.50, 3.00, 0.10, 30.0, 20.0),
    'XRP/USDT:USDT':    (0.50, 3.00, 0.10, 30.0, 20.0),  # 🆕 PATH D-PLUS CHAMPION
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

SYMBOLS = list(PER_SYMBOL_CONFIG.keys())

# ── AlgoAlpha Indicator Constants (v6 APEX OPTIMAL) ────────────────────────────
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

# ── Exchange ───────────────────────────────────────────────────────────────────
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.enable_demo_trading(True)
exchange.load_markets()

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': text.strip(), 'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        print(f"  [Telegram error] {e}")

# ── Safety Check Helpers ───────────────────────────────────────────────────────
def is_kill_switch_active() -> bool:
    today = date.today()
    return daily_pnl_tracker.get(today, 0.0) <= DAILY_KILL_SWITCH

def is_profit_lock_active() -> bool:
    today = date.today()
    return daily_pnl_tracker.get(today, 0.0) >= DAILY_PROFIT_LOCK

def record_closed_pnl(pnl_usd: float):
    today = date.today()
    daily_pnl_tracker[today] = daily_pnl_tracker.get(today, 0.0) + pnl_usd

# ── Indicators ─────────────────────────────────────────────────────────────────
def fetch_data(symbol, timeframe='15m', limit=300):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        for c in ['open', 'high', 'low', 'close']:
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        print(f"  [fetch_data error {symbol}] {e}")
        return None

def rma(series, length):
    return series.ewm(alpha=1/length, adjust=False).mean()

def calc_atr(df, length):
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low']  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return rma(tr, length)

def calc_wma(series, length):
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )

def algoalpha_baseline(df):
    st_atr = calc_atr(df, ST_PERIOD)
    hl2    = (df['high'] + df['low']) / 2
    basic_upper = hl2 + ST_FACTOR * st_atr
    basic_lower = hl2 - ST_FACTOR * st_atr

    upper = np.zeros(len(df))
    lower = np.zeros(len(df))
    close = df['close'].values

    upper[0] = basic_upper.iloc[0]
    lower[0] = basic_lower.iloc[0]

    for i in range(1, len(df)):
        lower[i] = (basic_lower.iloc[i] if (basic_lower.iloc[i] > lower[i-1] or close[i-1] < lower[i-1]) else lower[i-1])
        upper[i] = (basic_upper.iloc[i] if (basic_upper.iloc[i] < upper[i-1] or close[i-1] > upper[i-1]) else upper[i-1])

    mid_line = (pd.Series(lower, index=df.index) + pd.Series(upper, index=df.index)) / 2.0
    wma_line = calc_wma(mid_line, WMA_LENGTH)
    tL       = wma_line.ewm(span=EMA_LENGTH, adjust=False).mean()
    return tL

# 🏹 SMC STRUCTURAL TREND ENGINE (LIVE-SAFE, NO LOOKAHEAD BIAS)
def calc_smc_structure(df):
    # 7-bar fractal (3 left, 1 center, 3 right)
    right = 3
    window = 7
    
    # Identify Swing Highs mathematically confirmed 3 bars late
    roll_max = df['high'].rolling(window=window).max()
    is_swing_high = df['high'].shift(right) == roll_max
    swing_high_vals = np.where(is_swing_high, df['high'].shift(right), np.nan)
    df['last_swing_high'] = pd.Series(swing_high_vals).ffill()
    
    # Identify Swing Lows
    roll_min = df['low'].rolling(window=window).min()
    is_swing_low = df['low'].shift(right) == roll_min
    swing_low_vals = np.where(is_swing_low, df['low'].shift(right), np.nan)
    df['last_swing_low'] = pd.Series(swing_low_vals).ffill()

    # Determine Macro Structure (BOS / CHoCH equivalent)
    bullish_break = (df['close'] > df['last_swing_high'])
    bearish_break = (df['close'] < df['last_swing_low'])
    
    trend = pd.Series(np.nan, index=df.index)
    trend.loc[bullish_break] = 1   # Bullish Structure
    trend.loc[bearish_break] = -1  # Bearish Structure
    df['smc_trend'] = trend.ffill().fillna(0)
    
    return df

# ── Execution ──────────────────────────────────────────────────────────────────
def calculate_lot_size(symbol, entry_price, sl_price, risk_usd):
    sl_distance = abs(entry_price - sl_price)
    if sl_distance == 0: return 0
    return risk_usd / sl_distance

def execute_trade(symbol, direction, size, entry, sl, tp):
    side = 'buy' if direction == 'LONG' else 'sell'
    try:
        fmt_size = float(exchange.amount_to_precision(symbol, size))
        fmt_sl   = float(exchange.price_to_precision(symbol, sl))
        fmt_tp   = float(exchange.price_to_precision(symbol, tp))

        order = exchange.create_order(
            symbol=symbol, type='market', side=side, amount=fmt_size,
            params={'stopLoss': str(fmt_sl), 'takeProfit': str(fmt_tp), 'tpslMode': 'Full',
                    'slOrderType': 'Market', 'tpOrderType': 'Market',
                    'tpTriggerBy': 'LastPrice', 'slTriggerBy': 'LastPrice'}
        )
        return order, fmt_size, fmt_sl, fmt_tp
    except Exception as e:
        err = f"❌ Bybit Execution Failed ({symbol}): {e}"
        print(err)
        send_telegram(err)
        return None, None, None, None

def modify_bybit_tpsl(symbol, direction, new_sl, current_tp):
    market_id  = exchange.market(symbol)['id']
    bybit_side = 'Buy' if direction == 'LONG' else 'Sell'
    try:
        fmt_sl = float(exchange.price_to_precision(symbol, new_sl))
        exchange.privatePostV5PositionTradingStop({
            'category': 'linear', 'symbol': market_id, 'side': bybit_side, 'tpslMode': 'Full',
            'takeProfit': str(current_tp), 'stopLoss': str(fmt_sl), 'slOrderType': 'Market',
            'tpOrderType': 'Market', 'slTriggerBy': 'LastPrice', 'tpTriggerBy': 'LastPrice'
        })
        return fmt_sl
    except Exception as e:
        print(f"  [Bybit modify SL error {symbol}] {e}")
        return None

# ── Position Management ────────────────────────────────────────────────────────
def sync_open_positions():
    if not open_positions: return
    try:
        live_positions = exchange.fetch_positions()
        live_syms = {p['symbol'] for p in live_positions if float(p.get('contracts', 0)) > 0}
        closed = [sym for sym in list(open_positions.keys()) if sym not in live_syms]

        for sym in closed:
            pos = open_positions.pop(sym)
            market_id = exchange.market(sym)['id']
            try:
                response = exchange.private_get_v5_position_closed_pnl({'category': 'linear', 'symbol': market_id, 'limit': 1})
                records = response.get('result', {}).get('list', [])
                if records:
                    exact_pnl = float(records[0].get('closedPnl', 0.0))
                    record_closed_pnl(exact_pnl)
                    emoji = "✅" if exact_pnl > 0 else "❌"
                    msg = f"{emoji} <b>TRADE CLOSED — {sym.split('/')[0]}</b>\nSettled Net PnL: <code>${exact_pnl:.2f}</code>"
                    send_telegram(msg)
                    print(f"  ℹ️  {sym.split('/')[0]} closed. Settled PnL: ${exact_pnl:.2f}")
                else:
                    send_telegram(f"ℹ️ <b>TRADE CLOSED — {sym.split('/')[0]}</b>\n(PnL syncing in background)")
                    print(f"  ℹ️  {sym.split('/')[0]} closed. (No PnL record found)")
            except Exception as e:
                print(f"  [PnL fetch error {sym.split('/')[0]}] {e}", flush=True)
    except Exception as e:
        print(f"  [sync_positions error] {e}", flush=True)

def manage_trailing_stops():
    if not open_positions: return
    for symbol, pos in list(open_positions.items()):
        # ⚡ Fetch 1-minute data instead of 15-minute to catch exact wicks
        df = fetch_data(symbol, '1m', 5)
        if df is None or len(df) < 1: continue

        # ⚡ Track the LIVE breathing candle
        live_bar   = df.iloc[-1]
        high_now   = float(live_bar['high'])
        low_now    = float(live_bar['low'])
        live_price = float(live_bar['close']) # 🔴 NEW: The absolute live current price
        
        is_long    = pos['direction'] == 'LONG'
        trail_dist = pos['trail_mult'] * pos['atr']

        new_best = max(pos['best_price'], high_now) if is_long else min(pos['best_price'], low_now)
        pos['best_price'] = new_best
        profit_distance = abs(new_best - pos['entry'])

        if profit_distance >= (1.0 * pos['atr']) and not pos.get('free_ride_triggered', False):
            pos['free_ride_triggered'] = True
            send_telegram(f"🛡️ <b>FREE RIDE SECURED — {symbol.split('/')[0]}</b>\nTrade reached +1.0× ATR.")

        soft_tp_dist = pos['tp_mult'] * pos['atr']
        if profit_distance >= soft_tp_dist and not pos.get('trailing_tp_triggered', False):
            pos['trailing_tp_triggered'] = True
            send_telegram(f"🚀 <b>TRAILING TP ACTIVATED — {symbol.split('/')[0]}</b>\nTarget smashed! Uncapped 0.10x trail active.")

        if profit_distance < (0.25 * pos['atr']): continue

        raw_new_sl  = (new_best - trail_dist) if is_long else (new_best + trail_dist)
        sl_improved = raw_new_sl > pos['current_sl'] if is_long else raw_new_sl < pos['current_sl']

        if not sl_improved: continue

        # 🛡️ THE UPGRADE: LIVE PRICE SANITY CHECK
        # Before spamming Bybit, check if the flash-wick already reversed!
        if is_long and raw_new_sl >= live_price:
            continue  # Price dumped too fast. Bybit will reject. Wait silently.
        if not is_long and raw_new_sl <= live_price:
            continue  # Price pumped too fast. Bybit will reject. Wait silently.

        fmt_new_sl = modify_bybit_tpsl(symbol, pos['direction'], raw_new_sl, pos['catastrophic_tp'])
        if fmt_new_sl:
            pos['current_sl'] = fmt_new_sl
            locked = abs(fmt_new_sl - pos['entry'])
            msg = (f"<b>🔄 Trail Updated — {symbol.split('/')[0]}</b>\n{'▲' if is_long else '▼'} {pos['direction']}\n"
                   f"<b>New SL :</b> <code>{fmt_new_sl}</code>\n<b>Locked :</b> <code>{locked:.4f} pts</code> {'above' if is_long else 'below'} entry\n")
            send_telegram(msg)
            print(f"  🔄 Trail SL {symbol.split('/')[0]} → {fmt_new_sl}", flush=True)

# ── Fast Management Loop ───────────────────────────────────────────────────────
def fast_management():
    # ⚡ Decoupled function runs every 1 minute
    sync_open_positions()
    manage_trailing_stops()

# ── Signal Engine ──────────────────────────────────────────────────────────────
def check_signal():
    ts_now    = datetime.now(timezone.utc)
    today     = ts_now.date()
    today_pnl = daily_pnl_tracker.get(today, 0.0)

    print(f"[{ts_now.strftime('%H:%M UTC')}] Bybit Demo (15m) | PnL: ${today_pnl:.2f} | Open: {len(open_positions)}/{MAX_CONCURRENT}", flush=True)

    if is_kill_switch_active():
        print(f"  🛑 KILL-SWITCH ACTIVE — daily PnL ${today_pnl:.2f} <= ${DAILY_KILL_SWITCH}.", flush=True)
        manage_trailing_stops()
        return

    sync_open_positions()
    manage_trailing_stops()

    if len(open_positions) >= MAX_CONCURRENT:
        print(f"  ⏸  Max {MAX_CONCURRENT} concurrent positions reached.", flush=True)
        return

    for symbol in SYMBOLS:
        if len(open_positions) >= MAX_CONCURRENT: break
        if symbol in open_positions: continue

        df_15m = fetch_data(symbol, '15m', 250)
        if df_15m is None or len(df_15m) < 150: continue

        # Calculate APEX Indicators + SMC Structure
        df_15m['atr_14'] = calc_atr(df_15m, ATR_PERIOD)
        df_15m['tL']     = algoalpha_baseline(df_15m)
        df_15m           = calc_smc_structure(df_15m)

        c15m  = df_15m.iloc[-2]
        price = float(df_15m.iloc[-1]['close'])   
        atr   = float(c15m['atr_14'])
        smc_trend = int(c15m['smc_trend'])
        
        signal_bar_ts = int(c15m['ts'])
        if last_trade_bar.get(symbol) == signal_bar_ts: continue
        if atr < (price * 0.00005) or np.isnan(atr): continue

        tL_curr = float(df_15m['tL'].iloc[-2])
        tL_prev = float(df_15m['tL'].iloc[-3])
        tL_old  = float(df_15m['tL'].iloc[-4])

        # Raw APEX Signals
        algo_long  = (tL_curr > tL_prev) and (tL_prev <= tL_old)
        algo_short = (tL_curr < tL_prev) and (tL_prev >= tL_old)

        # 🤯 THE LIQUIDITY SWEEP (INVERTED SMC LOGIC):
        long_signal  = algo_short and (smc_trend == -1)
        short_signal = algo_long and (smc_trend == 1)

        # Log ignored traps
        if (algo_long or algo_short) and not (long_signal or short_signal):
            struct_str = "BULLISH" if smc_trend == 1 else "BEARISH" if smc_trend == -1 else "FLAT"
            print(f"  🛡️ {symbol.split('/')[0]:<10} | Ignored: APEX fired but SMC Structure ({struct_str}) hasn't capitulated.", flush=True)
            continue

        if not long_signal and not short_signal: continue

        sl_mult, tp_mult, trail_mult, p1_risk, p2_risk = PER_SYMBOL_CONFIG[symbol]
        base_risk_usd = p1_risk if CURRENT_PHASE == 1 else p2_risk
        grp = 'G1' if sl_mult == 0.50 else 'G2'

        if today_pnl >= HOUSE_MONEY_THRESHOLD:
            risk_usd = base_risk_usd * HOUSE_MONEY_MULTIPLIER
            hm_active = True
        else:
            risk_usd = base_risk_usd
            hm_active = False

        actual_sl_mult = min(sl_mult, tp_mult)
        direction = 'LONG' if long_signal else 'SHORT'
        raw_sl    = price - actual_sl_mult * atr if long_signal else price + actual_sl_mult * atr
        catastrophic_tp = price + 10.0 * atr if long_signal else price - 10.0 * atr

        raw_lot_size = calculate_lot_size(symbol, price, raw_sl, risk_usd)
        estimated_fee_usd  = raw_lot_size * price * BYBIT_TAKER_FEE * 2  
        
        if estimated_fee_usd > (risk_usd * FEE_CAP_FRAC):
            print(f"  ⏭️ {symbol.split('/')[0]:<10} | SKIP: SL too tight. Fee > {int(FEE_CAP_FRAC*100)}%.", flush=True)
            continue

        order, fmt_size, fmt_sl, fmt_tp = execute_trade(symbol, direction, raw_lot_size, price, raw_sl, catastrophic_tp)
        if not order: continue

        last_trade_bar[symbol] = signal_bar_ts
        open_positions[symbol] = {
            'direction': direction, 'entry': price, 'atr': atr, 'best_price': price,
            'current_sl': fmt_sl, 'catastrophic_tp': fmt_tp, 'trail_mult': trail_mult,
            'tp_mult': tp_mult, 'size': fmt_size, 'free_ride_triggered': False, 'trailing_tp_triggered': False
        }

        hm_badge = "🔥 <b>[HOUSE MONEY ACTIVE]</b>" if hm_active else ""
        struct_str = "BEARISH (Capitulation)" if smc_trend == -1 else "BULLISH (Greed Trap)"
        
        msg = (
            f"<b>🤯 APEX v7.2 LIQUIDITY SWEEP</b>\n"
            f"{'🟢 ▲ LONG' if long_signal else '🔴 ▼ SHORT'} "
            f"<b>{symbol.split('/')[0]}</b>  <i>[{grp} | Phase {CURRENT_PHASE}]</i>\n{hm_badge}\n\n"
            f"<b>Entry   :</b>  <code>{price:.6f}</code>\n"
            f"<b>SL      :</b>  <code>{fmt_sl}</code>  <i>({actual_sl_mult}× ATR)</i>\n"
            f"<b>Target  :</b>  Uncapped 🚀 <i>({tp_mult}×)</i>\n"
            f"<b>SMC Trap:</b> 🪤 <code>{struct_str}</code>\n"
            f"<b>Size    :</b>  <code>{fmt_size}</code>\n"
            f"<b>Est. Fee:</b>  <code>~${estimated_fee_usd:.2f}</code>\n"
            f"<b>Risk    :</b>  ${risk_usd:.0f}\n"
            f"<b>Open    :</b>  {len(open_positions)}/{MAX_CONCURRENT}"
        )
        send_telegram(msg)
        print(f"  ✅ {direction} {symbol.split('/')[0]:<10} | Entry={price:.6f} | SMC={struct_str} | Risk=${risk_usd:.0f}", flush=True)

def daily_reset():
    yesterday = datetime.now(timezone.utc).date()
    final_pnl = daily_pnl_tracker.get(yesterday, 0.0)
    send_telegram(f"<b>📅 Daily Reset</b>\nYesterday PnL: <code>${final_pnl:.2f}</code>\nKill-switch was {'🛑 ACTIVE' if final_pnl <= DAILY_KILL_SWITCH else '✅ NOT triggered'}")
    daily_pnl_tracker.clear()
    profit_lock_notified.clear()

if __name__ == '__main__':
    send_telegram(
        f"<b>🤯 APEX v7.2 PATH D-PLUS (19 Symbols | 1m Fast-Trail) Online</b>\n"
        "19 Symbols | Market Orders\n\n"
        f"Kill-Switch ${DAILY_KILL_SWITCH}/day | Profit-Lock LIFTED 🚀\n"
        "ST=2/14 | WMA=14 | EMA=3 | ATR=14\n"
        "🪤 LIQUIDITY SWEEP (SMC INVERSION) ACTIVE\n"
        f"🔥 HOUSE MONEY (1.5x) ACTIVE\n"
        "⚡ 1-Minute Live Fast-Trailing Engine (Sanity Check Patched) ENGAGED"
    )
    check_signal()
    
    # ⚡ The Decoupled 1-Minute Position Loop
    schedule.every(1).minutes.do(fast_management)
    
    # The Original 5-Minute Entry Loop
    schedule.every(5).minutes.at(":00").do(check_signal)
    schedule.every().day.at("00:05").do(daily_reset)
    
    while True:
        try: schedule.run_pending()
        except Exception as e: print(f"  [loop error] {e}", flush=True)
        time.sleep(1)
