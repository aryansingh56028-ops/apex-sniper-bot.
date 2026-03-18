import ccxt
import pandas as pd
import pandas_ta as ta
import time
import requests
from datetime import datetime
from flask import Flask
from threading import Thread

==========================================
🛑 BOT SETTINGS & API KEYS 🛑
==========================================
TELEGRAM_BOT_TOKEN = '8672318438:AAF-MHF9c7H2sjxRum2qj8JtJOZ446cXfaA'
TELEGRAM_CHAT_ID = '1932328527'

BINANCE_API_KEY = 'g2iyUq24vwVG8YaoMgwUMlttvudlzR63gRxoGMQDXoeBCsN0ydRvKxLbz8xa8DuC'
BINANCE_SECRET_KEY = 'QPAE8DUDWb1b8xxoInqM2EjGULLWGA9NEPEM3ErksAHxft7rhy9eeibfOzwHNPYz'

ACCOUNT_SIZE = 10,740
RISK_PERCENT = 1.0

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "DOT/USDT", "LINK/USDT", "LTC/USDT"]
TIMEFRAME = "1h"

--- CONNECT TO BINANCE FUTURES TESTNET ---
exchange = ccxt.binance({
'apiKey': g2iyUq24vwVG8YaoMgwUMlttvudlzR63gRxoGMQDXoeBCsN0ydRvKxLbz8xa8DuC,
'secret': QPAE8DUDWb1b8xxoInqM2EjGULLWGA9NEPEM3ErksAHxft7rhy9eeibfOzwHNPYz,
'enableRateLimit': True,
'options': {'defaultType': 'future'}
})
exchange.set_sandbox_mode(True) # ⚠️ THIS KEEPS IT ON FAKE MONEY ⚠️

active_trades = {}

app = Flask(name)
@app.route('/')
def home(): return "🤖 Apex Sniper V5 (Auto-Execution) is Online 🤖"

def run_server():
try: app.run(host='0.0.0.0', port=8080)
except: pass

def send_telegram_message(message):
url = f"https://api.telegram.org/bot8672318438:AAF-MHF9c7H2sjxRum2qj8JtJOZ446cXfaA/sendMessage"
try: requests.post(url, json={'chat_id': 1932328527, 'text': message, 'parse_mode': 'Markdown'})
except Exception: pass

def execute_trade(symbol, direction, entry, sl, tp, coins_to_buy):
"""Sends the actual buy/sell orders to Binance"""
try:
# Format the amount to Binance's required decimal rules
amount = exchange.amount_to_precision(symbol, coins_to_buy)

    if direction == "LONG":
        # 1. Open Long Position
        exchange.create_order(symbol, 'market', 'buy', amount)
        # 2. Place Hard Stop Loss
        exchange.create_order(symbol, 'STOP_MARKET', 'sell', amount, params={'stopPrice': exchange.price_to_precision(symbol, sl), 'reduceOnly': True})
        # 3. Place Take Profit
        exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', 'sell', amount, params={'stopPrice': exchange.price_to_precision(symbol, tp), 'reduceOnly': True})
        
    elif direction == "SHORT":
        exchange.create_order(symbol, 'market', 'sell', amount)
        exchange.create_order(symbol, 'STOP_MARKET', 'buy', amount, params={'stopPrice': exchange.price_to_precision(symbol, sl), 'reduceOnly': True})
        exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', 'buy', amount, params={'stopPrice': exchange.price_to_precision(symbol, tp), 'reduceOnly': True})
        
    return amount
except Exception as e:
    send_telegram_message(f"❌ **TRADE EXECUTION FAILED** ❌\n{symbol}: {str(e)}")
    return None
def get_signal(symbol):
try:
candles_4h = exchange.fetch_ohlcv(symbol, '4h', limit=100)
df_4h = pd.DataFrame(candles_4h, columns=["time", "open", "high", "low", "close", "volume"])
df_4h['ema_200'] = ta.ema(df_4h['close'], length=200)
st_4h = ta.supertrend(df_4h['high'], df_4h['low'], df_4h['close'], length=10, multiplier=3.0)
df_4h['trend_dir'] = st_4h['SUPERTd_10_3.0'] if st_4h is not None else 0
macro = df_4h.iloc[-2]

    candles = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=100)
    df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
    df['ema_200'] = ta.ema(df['close'], length=200) 
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
    df['adx'] = adx_df['ADX_14'] if adx_df is not None else 0
    st = ta.supertrend(df['high'], df['low'], df['close'], length=10, multiplier=3.0)
    df['trend_dir'] = st['SUPERTd_10_3.0'] if st is not None else 0
    
    df['fvg_bull'] = (df['high'].shift(2) < df['low']) & (df['close'].shift(1) > df['open'].shift(1))
    df['fvg_bot'] = df['high'].shift(2).where(df['fvg_bull']).ffill(limit=5)
    df['fvg_bear'] = (df['low'].shift(2) > df['high']) & (df['close'].shift(1) < df['open'].shift(1))
    df['fvg_top'] = df['low'].shift(2).where(df['fvg_bear']).ffill(limit=5)
    direction, entry, sl, tp = None, 0, 0, 0
    row = df.iloc[-2] 
    
    is_4h_bullish = macro['close'] > macro['ema_200'] and macro['trend_dir'] == 1
    is_4h_bearish = macro['close'] < macro['ema_200'] and macro['trend_dir'] == -1
    if (is_4h_bullish and row['close'] > row['ema_200'] and row['trend_dir'] == 1 and 
        row['adx'] > 25 and pd.notna(row['fvg_bot']) and row['low'] <= row['fvg_bot'] and row['close'] > row['open']):
        direction, entry = "LONG", row['close']
        sl, tp = entry - (row['atr'] * 2.0), entry + (row['atr'] * 3.0)
        
    elif (is_4h_bearish and row['close'] < row['ema_200'] and row['trend_dir'] == -1 and 
          row['adx'] > 25 and pd.notna(row['fvg_top']) and row['high'] >= row['fvg_top'] and row['close'] < row['open']):
        direction, entry = "SHORT", row['close']
        sl, tp = entry + (row['atr'] * 2.0), entry - (row['atr'] * 3.0)
    if direction:
        risk_amount = ACCOUNT_SIZE * (RISK_PERCENT / 100)
        sl_distance = abs(entry - sl)
        coins_to_buy = risk_amount / sl_distance
        return direction, entry, sl, tp, coins_to_buy
except Exception: pass
return None, 0, 0, 0, 0
def manage_active_trades():
keys_to_remove = []
for symbol, trade in active_trades.items():
try:
candles = exchange.fetch_ohlcv(symbol, '15m', limit=10)
df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
st = ta.supertrend(df['high'], df['low'], df['close'], length=10, multiplier=3.0)
trend_dir = st['SUPERTd_10_3.0'].iloc[-2] if st is not None else 0

        # Auto-Close Position if Emergency Exit triggered
        if trade['direction'] == "LONG" and trend_dir == -1:
            exchange.create_order(symbol, 'market', 'sell', trade['amount'], params={'reduceOnly': True})
            send_telegram_message(f"🚨 **EMERGENCY AUTO-EXIT** 🚨\n🪙 **{symbol}** LONG closed by bot.\n⚠️ 15m Trend Reversed.")
            keys_to_remove.append(symbol)
            
        elif trade['direction'] == "SHORT" and trend_dir == 1:
            exchange.create_order(symbol, 'market', 'buy', trade['amount'], params={'reduceOnly': True})
            send_telegram_message(f"🚨 **EMERGENCY AUTO-EXIT** 🚨\n🪙 **{symbol}** SHORT closed by bot.\n⚠️ 15m Trend Reversed.")
            keys_to_remove.append(symbol)
            
    except Exception: pass
    time.sleep(1) 
    
for k in keys_to_remove: del active_trades[k]
def main_loop():
send_telegram_message("🤖 Apex Sniper V5 LIVE: Auto-Execution connected to Binance Testnet.")
last_scan_hour = -1
last_manage_minute = -1

while True:
    current_time = datetime.now()
    
    # 1. SCAN & AUTO-TRADE 
    if current_time.minute == 1 and current_time.hour != last_scan_hour:
        for symbol in SYMBOLS:
            if symbol in active_trades: continue 
                
            direction, entry, sl, tp, coins_to_buy = get_signal(symbol)
            if direction:
                # 🔥 THE BOT PULLS THE TRIGGER HERE 🔥
                actual_amount = execute_trade(symbol, direction, entry, sl, tp, coins_to_buy)
                
                if actual_amount:
                    msg = f"⚡ **AUTO-TRADE EXECUTED** ⚡\n\n🪙 **Symbol:** {symbol} {direction}\n🎯 **Entry:** {entry:.4f}\n🛑 **SL:** {sl:.4f}\n💸 **TP:** {tp:.4f}\n💼 **Size:** {actual_amount} coins"
                    send_telegram_message(msg)
                    active_trades[symbol] = {'direction': direction, 'amount': actual_amount}
            time.sleep(1) 
        last_scan_hour = current_time.hour
        
    # 2. GUARDIAN AUTO-MANAGEMENT
    if current_time.minute % 5 == 0 and current_time.minute != last_manage_minute:
        if len(active_trades) > 0: manage_active_trades()
        last_manage_minute = current_time.minute
        
    time.sleep(15)
if name == "main":
Thread(target=run_server).start()
main_loop()
