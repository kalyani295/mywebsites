from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask import render_template

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
import time as time_module
from threading import Thread, Lock
from functools import lru_cache
import requests
from bs4 import BeautifulSoup
import json
from flask import make_response

app = Flask(__name__)

# Stock List (NSE Symbols)
stock_list = [
    "^NSEI",  # Nifty 50
    "^NSEBANK",  # Nifty Bank
    "NIFTY_MIDCAP_100.NS",  # Nifty Midcap 100
    "^BSESN",  # SENSEX
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "LICI.NS",
    "LT.NS", "KOTAKBANK.NS", "AXISBANK.NS", "ASIANPAINT.NS",
    "MARUTI.NS", "TITAN.NS", "BAJFINANCE.NS", "TATASTEEL.NS", "POWERGRID.NS",
    "NTPC.NS", "ULTRACEMCO.NS", "SUNPHARMA.NS", "TATAMOTORS.NS", "ADANIENT.NS",
    "BAJAJ-AUTO.NS", "WIPRO.NS", "ONGC.NS", "JSWSTEEL.NS", "HCLTECH.NS",
    "TATACONSUM.NS", "NESTLEIND.NS", "INDUSINDBK.NS", "COALINDIA.NS", "BPCL.NS"
]

# Cache configuration
data_cache = {
    '5m': {'data': None, 'next_refresh': None, 'last_refresh': None, 'lock': Lock()},
    '15m': {'data': None, 'next_refresh': None, 'last_refresh': None, 'lock': Lock()},
    '1h': {'data': None, 'next_refresh': None, 'last_refresh': None, 'lock': Lock()},
    '1d': {'data': None, 'next_refresh': None, 'last_refresh': None, 'lock': Lock()}
}

waiting_list_cache = {
    '5m': {'data': None, 'next_refresh': None, 'last_refresh': None, 'lock': Lock()},
    '15m': {'data': None, 'next_refresh': None, 'last_refresh': None, 'lock': Lock()},
    '1h': {'data': None, 'next_refresh': None, 'last_refresh': None, 'lock': Lock()},
    '1d': {'data': None, 'next_refresh': None, 'last_refresh': None, 'lock': Lock()}
}

def get_nse_holidays():
    """Return NSE holidays for 2025"""
    return [
        "2025-02-26",  # Mahashivratri
        "2025-03-14",  # Holi
        "2025-03-31",  # Id-Ul-Fitr (Ramadan Eid)
        "2025-04-10",  # Shri Mahavir Jayanti
        "2025-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
        "2025-04-18",  # Good Friday
        "2025-05-01",  # Maharashtra Day
        "2025-08-15",  # Independence Day / Parsi New Year
        "2025-08-27",  # Shri Ganesh Chaturthi
        "2025-10-02",  # Mahatma Gandhi Jayanti/Dussehra
        "2025-10-21",  # Diwali Laxmi Pujan
        "2025-10-22",  # Balipratipada
        "2025-11-05",  # Prakash Gurpurb Sri Guru Nanak Dev
        "2025-12-25"   # Christmas
    ]

@lru_cache(maxsize=1)
def get_nse_holidays_cached():
    """Cached version of get_nse_holidays that refreshes daily"""
    return get_nse_holidays()

def get_nearest_expiry():
    """Get the nearest Thursday expiry date (NSE options expire on Thursdays)"""
    today = datetime.now().date()
    # Find next Thursday
    days_ahead = (3 - today.weekday()) % 7  # 3 is Thursday
    if days_ahead == 0:  # Today is Thursday
        if datetime.now().time() > time(15, 30):  # After market close
            days_ahead = 7  # Move to next Thursday
    expiry_date = today + timedelta(days=days_ahead)
    return expiry_date.strftime('%d%b%y').upper()  # Format like "25APR24"

@app.route("/api/holidays")
def get_holidays():
    holidays = get_nse_holidays_cached()
    return jsonify(holidays)

def schedule_refresh(timeframe, delay):
    """Background thread to trigger refresh at candle close"""
    time_module.sleep(delay)
    get_cached_data(timeframe)

def schedule_waiting_list_refresh(timeframe, delay):
    """Background thread to trigger refresh at exact times"""
    time_module.sleep(delay)
    with waiting_list_cache[timeframe]['lock']:
        now = datetime.now()
        stocks = get_live_data(timeframe, live_mode=True)
        waiting_list_cache[timeframe]['data'] = stocks
        waiting_list_cache[timeframe]['last_refresh'] = now
        waiting_list_cache[timeframe]['next_refresh'] = get_waiting_list_exact_refresh_time(timeframe, now)
        
        # Schedule next refresh
        refresh_delay = (waiting_list_cache[timeframe]['next_refresh'] - now).total_seconds()
        if refresh_delay > 0:
            Thread(target=schedule_waiting_list_refresh, args=(timeframe, refresh_delay)).start()

def is_market_open(now=None):
    """Check if current time is within Indian stock market hours"""
    now = now or datetime.now()
    if not is_trading_day(now):
        return False
    
    market_open = time(9, 15)
    market_close = time(15, 30)
    current_time = now.time()
    return market_open <= current_time <= market_close

def is_trading_day(date=None):
    """Check if the date is a trading day (not weekend or holiday)"""
    date = date or datetime.now()
    holidays = get_nse_holidays_cached()
    return date.weekday() < 5 and date.strftime('%Y-%m-%d') not in holidays

def get_next_trading_day(date):
    """Get the next trading day (skips weekends and holidays)"""
    next_day = date + timedelta(days=1)
    while not is_trading_day(next_day):
        next_day += timedelta(days=1)
    return next_day

def get_next_refresh_time(timeframe, now=None):
    """Calculate the next refresh time for main screener"""
    now = now or datetime.now()
    
    if not is_market_open(now):
        # Market is closed, schedule for next trading day
        next_trading_day = get_next_trading_day(now)
        if timeframe == '5m':
            return next_trading_day.replace(hour=9, minute=19, second=35, microsecond=0)
        elif timeframe == '15m':
            return next_trading_day.replace(hour=9, minute=29, second=35, microsecond=0)
        elif timeframe == '1h':
            return next_trading_day.replace(hour=10, minute=14, second=35, microsecond=0)
        elif timeframe == '1d':
            return next_trading_day.replace(hour=15, minute=29, second=35, microsecond=0)
    
    # Define the exact refresh schedules
    if timeframe == '5m':
        base_time = now.replace(hour=9, minute=19, second=35, microsecond=0)
        if now < base_time:
            return base_time
        
        minutes_since_open = (now.hour - 9) * 60 + (now.minute - 15)
        periods_passed = (minutes_since_open - 4) // 5
        next_period = periods_passed + 1
        next_minute = 19 + (next_period * 5)
        
        if next_minute >= 60:
            next_hour = 9 + (next_minute // 60)
            next_minute = next_minute % 60
        else:
            next_hour = 9
            
        next_refresh = now.replace(hour=next_hour, minute=next_minute, second=35, microsecond=0)
        
        if next_refresh <= now:
            next_refresh = next_refresh + timedelta(minutes=5)
            
        if next_refresh.time() > time(15, 30):
            next_refresh = get_next_trading_day(now).replace(hour=9, minute=19, second=35, microsecond=0)
            
    elif timeframe == '15m':
        base_time = now.replace(hour=9, minute=29, second=35, microsecond=0)
        if now < base_time:
            return base_time
            
        minutes_since_open = (now.hour - 9) * 60 + (now.minute - 15)
        periods_passed = (minutes_since_open - 14) // 15
        next_period = periods_passed + 1
        next_minute = 29 + (next_period * 15)
        
        if next_minute >= 60:
            next_hour = 9 + (next_minute // 60)
            next_minute = next_minute % 60
        else:
            next_hour = 9
            
        next_refresh = now.replace(hour=next_hour, minute=next_minute, second=35, microsecond=0)
        
        if next_refresh <= now:
            next_refresh = next_refresh + timedelta(minutes=15)
            
        if next_refresh.time() > time(15, 30):
            next_refresh = get_next_trading_day(now).replace(hour=9, minute=29, second=35, microsecond=0)
            
    elif timeframe == '1h':
        base_time = now.replace(hour=10, minute=14, second=35, microsecond=0)
        if now < base_time:
            return base_time
            
        next_hour = now.hour + 1
        next_refresh = now.replace(hour=next_hour, minute=14, second=35, microsecond=0)
        
        if next_refresh.time() > time(15, 30):
            next_refresh = get_next_trading_day(now).replace(hour=10, minute=14, second=35, microsecond=0)
            
    elif timeframe == '1d':
        if now.time() < time(15, 29, 35):
            next_refresh = now.replace(hour=15, minute=29, second=35, microsecond=0)
        else:
            next_refresh = get_next_trading_day(now).replace(hour=15, minute=29, second=35, microsecond=0)
    
    return next_refresh

def get_waiting_list_exact_refresh_time(timeframe, now=None):
    """Returns next refresh time for waiting list at PRECISE times (XX:XX:00)"""
    now = now or datetime.now()
    market_open = time(9, 15)
    market_close = time(15, 30)
    current_time = now.time()
    
    if timeframe == '5m':
        if current_time < time(9, 18, 0):
            next_refresh = now.replace(hour=9, minute=18, second=0, microsecond=0)
        else:
            current_minute = now.minute
            next_minute = ((current_minute - 18) // 5 * 5) + 23
            
            # Calculate next hour properly
            hour_adjustment = next_minute // 60
            next_minute = next_minute % 60
            next_hour = now.hour + hour_adjustment
            
            # Handle day rollover if needed
            if next_hour >= 24:
                next_hour -= 24
                next_day = now + timedelta(days=1)
                next_refresh = next_day.replace(hour=next_hour, minute=next_minute, second=0, microsecond=0)
            else:
                next_refresh = now.replace(hour=next_hour, minute=next_minute, second=0, microsecond=0)
            
            if next_refresh.time() > market_close:
                next_refresh = get_next_trading_day(now).replace(hour=9, minute=18, second=0, microsecond=0)
    
    elif timeframe == '15m':
        if current_time < time(9, 28, 0):
            next_refresh = now.replace(hour=9, minute=28, second=0, microsecond=0)
        else:
            current_minute = now.minute
            next_minute = ((current_minute - 28) // 15 * 15) + 43
            
            # Calculate next hour properly
            hour_adjustment = next_minute // 60
            next_minute = next_minute % 60
            next_hour = now.hour + hour_adjustment
            
            # Handle day rollover if needed
            if next_hour >= 24:
                next_hour -= 24
                next_day = now + timedelta(days=1)
                next_refresh = next_day.replace(hour=next_hour, minute=next_minute, second=0, microsecond=0)
            else:
                next_refresh = now.replace(hour=next_hour, minute=next_minute, second=0, microsecond=0)
            
            if next_refresh.time() > market_close:
                next_refresh = get_next_trading_day(now).replace(hour=9, minute=28, second=0, microsecond=0)
    
    elif timeframe == '1h':
        if current_time < time(10, 13, 0):
            next_refresh = now.replace(hour=10, minute=13, second=0, microsecond=0)
        else:
            next_hour = now.hour + 1
            
            # Handle day rollover if needed
            if next_hour >= 24:
                next_hour -= 24
                next_day = now + timedelta(days=1)
                next_refresh = next_day.replace(hour=next_hour, minute=13, second=0, microsecond=0)
            else:
                next_refresh = now.replace(hour=next_hour, minute=13, second=0, microsecond=0)
            
            if next_refresh.time() > market_close:
                next_refresh = get_next_trading_day(now).replace(hour=10, minute=13, second=0, microsecond=0)
    
    elif timeframe == '1d':
        if current_time < time(15, 28, 0):
            next_refresh = now.replace(hour=15, minute=28, second=0, microsecond=0)
        else:
            next_refresh = get_next_trading_day(now).replace(hour=15, minute=28, second=0, microsecond=0)
    
    return next_refresh

def analyze_trend(df, last_idx=-1, timeframe="15m"):
    """Analyze trend based on EMA, candle color and RSI"""
    last_candle = df.iloc[last_idx]
    
    open_price = float(last_candle['Open'])
    close_price = float(last_candle['Close'])
    last_low = float(last_candle['Low'])
    last_high = float(last_candle['High'])
    last_volume = float(last_candle['Volume'])
    last_ema9 = float(df["EMA_9"].iloc[last_idx])
    last_ema20 = float(df["EMA_20"].iloc[last_idx])
    last_rsi = float(df["RSI_14"].iloc[last_idx])
    last_rsi_ema20 = float(df["RSI_EMA_20"].iloc[last_idx])
    avg_volume = float(df["Volume"].rolling(20).mean().iloc[last_idx])

    candle_color = "green" if close_price > open_price else "red"
    
    if timeframe == "5m":
        # Golden Webinar conditions for 5-minute timeframe
        bull_condition_golden = (
            (open_price < last_ema9 and close_price > last_ema9 and last_rsi > last_rsi_ema20) or
            (open_price < last_ema20 and close_price > last_ema20 and last_rsi > last_rsi_ema20)
        )
        
        bear_condition_golden = (
            (open_price > last_ema9 and close_price < last_ema9 and last_rsi < last_rsi_ema20) or
            (open_price > last_ema20 and close_price < last_ema20 and last_rsi < last_rsi_ema20)
        )
        
        if bull_condition_golden:
            return "Bullish"
        elif bear_condition_golden:
            return "Bearish"
        else:
            return "Neutral"
    else:
        # Original conditions for other timeframes
        ema_touch = (
            (min(open_price, close_price) <= last_ema9 <= max(open_price, close_price)) or
            (last_low <= last_ema9 <= last_high) or
            (min(open_price, close_price) <= last_ema20 <= max(open_price, close_price)) or
            (last_low <= last_ema20 <= last_high)
        )
        
        if (last_ema9 > last_ema20 and 
            candle_color == "green" and 
            ema_touch and 
            last_rsi > last_rsi_ema20):
            return "Bullish"
        elif (last_ema9 < last_ema20 and 
              candle_color == "red" and 
              ema_touch and 
              last_rsi < last_rsi_ema20):
            return "Bearish"
        else:
            return "Neutral"
# In app.py, modify the get_live_data function (remove all option-related code):
def get_live_data(timeframe="15m", live_mode=False):
    data = []
    # Use only indices for 5m timeframe
    stocks_to_analyze = [
        "^NSEI",  # Nifty 50
        "^NSEBANK",  # Nifty Bank
        "^CNXMIDCAP",  # Nifty Midcap
        "^BSESN"  # SENSEX
    ] if timeframe == "5m" else stock_list
    
    for stock in stocks_to_analyze:
        try:
            if timeframe == '15m':
                period = "10d"
            elif timeframe == '1h':
                period = "20d"
            elif timeframe == '1d':
                period = "3mo"
            else:
                period = "2d"
            
            df = yf.download(stock, period=period, interval=timeframe, auto_adjust=False, timeout=10)
            if df.empty or len(df) < 2:
                print(f"No data or not enough candles for {stock}")
                continue

            df = df.copy()
            df["EMA_9"] = df["Close"].ewm(span=9, adjust=False).mean()
            df["EMA_20"] = df["Close"].ewm(span=20, adjust=False).mean()
            
            # Calculate RSI
            delta = df["Close"].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
            rs = avg_gain / avg_loss
            rs.replace([np.inf, -np.inf], np.nan, inplace=True)
            df["RSI_14"] = 100 - (100 / (1 + rs))
            df["RSI_EMA_20"] = df["RSI_14"].ewm(span=20, min_periods=20, adjust=False).mean()
            
            # Calculate volume average
            df["Volume_Avg_20"] = df["Volume"].rolling(20).mean()

            df["RSI_14"] = df["RSI_14"].ffill()
            df["RSI_EMA_20"] = df["RSI_EMA_20"].ffill()
            df["Volume_Avg_20"] = df["Volume_Avg_20"].ffill()
            
            last_idx = -1 if live_mode else -2
            trend = analyze_trend(df, last_idx, timeframe)
            
            # Fix chart URLs for indices
            timeframe_map = {
                '5m': '5',
                '15m': '15', 
                '1h': '60',
                '1d': '1D'
            }
            tv_timeframe = timeframe_map.get(timeframe, '15')
            
            if stock == "^NSEI":
                chart_url = f"https://www.tradingview.com/chart/?symbol=NSE:NIFTY&interval={tv_timeframe}"
            elif stock == "^NSEBANK":
                chart_url = f"https://www.tradingview.com/chart/?symbol=NSE:BANKNIFTY&interval={tv_timeframe}"
            elif stock == "^CNXMIDCAP":
                chart_url = f"https://www.tradingview.com/chart/?symbol=NSE:CNXMIDCAP&interval={tv_timeframe}"
            elif stock == "^BSESN":
                chart_url = f"https://www.tradingview.com/chart/?symbol=BSE:SENSEX&interval={tv_timeframe}"
            else:
                chart_url = f"https://www.tradingview.com/chart/?symbol=NSE:{stock.replace('.NS', '')}&interval={tv_timeframe}"

            data.append({
                "symbol": stock.replace(".NS", "").replace("^", ""),
                "open": round(float(df.iloc[last_idx]['Open']), 2),
                "close": round(float(df.iloc[last_idx]['Close']), 2),
                "ema9": round(float(df["EMA_9"].iloc[last_idx]), 2),
                "ema20": round(float(df["EMA_20"].iloc[last_idx]), 2),
                "rsi": round(float(df["RSI_14"].iloc[last_idx]), 2),
                "rsi_ema20": round(float(df["RSI_EMA_20"].iloc[last_idx]), 2),
                "volume": int(df.iloc[last_idx]['Volume']),
                "volume_avg": round(float(df["Volume_Avg_20"].iloc[last_idx]), 2),
                "trend": trend,
                "chart_url": chart_url,
                "timestamp": df.index[last_idx].strftime('%Y-%m-%d %H:%M:%S')
            })
        
        except Exception as e:
            print(f"Error processing {stock}: {str(e)}")
            continue

    return data


def get_cached_data(timeframe):
    """Return cached data, updating at scheduled times but using live data"""
    now = datetime.now()
    
    with data_cache[timeframe]['lock']:
        if data_cache[timeframe]['data'] is None:
            data_cache[timeframe]['data'] = get_live_data(timeframe, live_mode=True)
            data_cache[timeframe]['last_refresh'] = now
            next_refresh = get_next_refresh_time(timeframe, now)
            data_cache[timeframe]['next_refresh'] = next_refresh
            return data_cache[timeframe]['data']
            
        if (data_cache[timeframe]['next_refresh'] and 
            now >= data_cache[timeframe]['next_refresh']):
            
            new_data = get_live_data(timeframe, live_mode=True)
            data_cache[timeframe]['data'] = new_data
            data_cache[timeframe]['last_refresh'] = now
            next_refresh = get_next_refresh_time(timeframe, now)
            data_cache[timeframe]['next_refresh'] = next_refresh
            
            refresh_delay = (next_refresh - now).total_seconds()
            if refresh_delay > 0:
                Thread(target=schedule_refresh, args=(timeframe, refresh_delay)).start()
        
        return data_cache[timeframe]['data']

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/screener")
def screener():
    timeframe = request.args.get('timeframe', '15m')
    stocks = get_cached_data(timeframe)
    
    with data_cache[timeframe]['lock']:
        next_refresh = data_cache[timeframe]['next_refresh']
        last_refresh = data_cache[timeframe]['last_refresh'] or datetime.now()
        
        now = datetime.now()
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if now.time() > time(15, 30):
            last_refresh = market_close
    
    return render_template("screener.html", 
                         stocks=stocks,
                         current_timeframe=timeframe,
                         next_refresh=next_refresh.timestamp(),
                         last_refresh=last_refresh.timestamp())

@app.route("/live_screener")
def live_screener():
    timeframe = request.args.get('timeframe', '15m')
    stocks = get_live_data(timeframe, live_mode=True)
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    return render_template(
        "live_screener.html",
        stocks=stocks,
        current_timeframe=timeframe,
        current_time=current_time,
        market_open=is_market_open()
    )

@app.route("/waiting_list")
def waiting_list():
    timeframe = request.args.get('timeframe', '15m')
    
    with waiting_list_cache[timeframe]['lock']:
        now = datetime.now()
        
        if (waiting_list_cache[timeframe]['data'] is None or 
            (waiting_list_cache[timeframe]['next_refresh'] and 
             now >= waiting_list_cache[timeframe]['next_refresh'])):
            
            stocks = get_live_data(timeframe, live_mode=True)
            waiting_list_cache[timeframe]['data'] = stocks
            waiting_list_cache[timeframe]['last_refresh'] = now
            waiting_list_cache[timeframe]['next_refresh'] = get_waiting_list_exact_refresh_time(timeframe, now)
            
          
            
            refresh_delay = (waiting_list_cache[timeframe]['next_refresh'] - now).total_seconds()
            if refresh_delay > 0:
                Thread(target=schedule_waiting_list_refresh, args=(timeframe, refresh_delay)).start()
        
        return render_template(
            "waiting_list.html",
            stocks=waiting_list_cache[timeframe]['data'],
            current_timeframe=timeframe,
            next_refresh=waiting_list_cache[timeframe]['next_refresh'].timestamp(),
            last_refresh=waiting_list_cache[timeframe]['last_refresh'].timestamp(),
            market_open=is_market_open()
        )

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route('/manage_stocks', methods=['GET', 'POST'])
def manage_stocks(): 
    global stock_list
    show_list = request.args.get('show', 'false') == 'true'
    
    if request.method == 'POST':
        action = request.form.get('action')
        stock = request.form.get('stock', '').strip().upper()
        
        if not stock.endswith('.NS'):
            stock += '.NS'
            
        if action == 'add':
            if stock not in stock_list:
                stock_list.append(stock)
        elif action == 'remove':
            if stock in stock_list:
                stock_list.remove(stock)
                
        return redirect(url_for('manage_stocks', show='true'))
    
    return render_template('manage_stocks.html', 
                         stock_list=sorted(stock_list),
                         show_list=show_list)

@app.route("/create_scan")
def create_scan():
    return render_template("create_scan.html")


if __name__ == "__main__":
    # Initialize cache for all timeframes
    for tf in ['5m', '15m', '1h', '1d']:
        get_cached_data(tf)
        with waiting_list_cache[tf]['lock']:
            waiting_list_cache[tf]['data'] = get_live_data(tf, live_mode=True)
            waiting_list_cache[tf]['last_refresh'] = datetime.now()
            waiting_list_cache[tf]['next_refresh'] = get_waiting_list_exact_refresh_time(tf, datetime.now())
            
            refresh_delay = (waiting_list_cache[tf]['next_refresh'] - datetime.now()).total_seconds()
            if refresh_delay > 0:
                Thread(target=schedule_waiting_list_refresh, args=(tf, refresh_delay)).start()
    
    app.run(host='0.0.0.0', debug=True)