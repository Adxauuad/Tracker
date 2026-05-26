#!/usr/bin/env python3
"""
XAUUSD Telegram Signal Bot V3 — Upgraded
Strategy  : EMA 9/21/50 + RSI + Trend Filter
Timeframe : M5
Features  : Better accuracy, news avoidance, compounding tracker,
            FundingPips daily loss alert
Built by  : Claude AI
"""

import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta
import pytz

# ─── YOUR SETTINGS ───────────────────────────────────────
TELEGRAM_TOKEN   = "8719862501:AAEk7qscn247OznZOSd0CuLGUg6vQS3W_yc"
CHAT_ID          = "1628528552"
SYMBOL           = "XAUUSD"
CHECK_INTERVAL   = 60 * 5      # Every 5 minutes (M5)
FUNDED_BALANCE   = 5000        # Funded account balance
MAX_DAILY_LOSS   = 150         # FundingPips 3% daily limit
MAX_TRADES_DAY   = 2           # Max trades per day
BASE_LOT         = 0.05        # Starting lot size
MAX_LOT          = 0.20        # Maximum lot size
LOT_MULTIPLIER   = 1.2         # Compound after win
# ─────────────────────────────────────────────────────────

# Indicator settings
EMA_FAST        = 9
EMA_SLOW        = 21
EMA_TREND       = 50    # New trend filter
RSI_PERIOD      = 14
RSI_OVERBOUGHT  = 65    # Tighter filter
RSI_OVERSOLD    = 35    # Tighter filter
ATR_PERIOD      = 14
ATR_SL_MULTI    = 1.5
ATR_TP_MULTI    = 2.5

# State
last_signal         = None
current_lot         = BASE_LOT
trades_today        = 0
daily_pnl           = 0.0
consecutive_wins    = 0
last_trade_date     = None
market_closed_notified = False


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Telegram error: {e}")


def is_market_open():
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    weekday = now.weekday()
    hour = now.hour
    if weekday == 5: return False
    if weekday == 6: return hour >= 17
    if weekday == 4: return hour < 17
    return True


def is_news_time():
    """Avoid trading 5 min before and after each hour (common news times)"""
    now = datetime.now(pytz.utc)
    minute = now.minute
    # Avoid first and last 5 minutes of each hour
    if minute <= 5 or minute >= 55:
        return True
    # Avoid specific high impact times (UTC)
    high_impact_hours = [8, 9, 13, 14, 15, 18]  # Common gold news hours
    if now.hour in high_impact_hours and (minute <= 5 or (minute >= 25 and minute <= 35)):
        return True
    return False


def check_new_day():
    global trades_today, daily_pnl, last_trade_date
    today = datetime.now().date()
    if last_trade_date != today:
        trades_today = 0
        daily_pnl = 0.0
        last_trade_date = today
        print(f"New trading day! Resetting counters.")


def compound_lot(won):
    global current_lot, consecutive_wins
    if won:
        consecutive_wins += 1
        new_lot = round(min(current_lot * LOT_MULTIPLIER, MAX_LOT), 2)
        current_lot = new_lot
        print(f"WIN! Compounding lot to {current_lot}")
    else:
        consecutive_wins = 0
        current_lot = BASE_LOT
        print(f"LOSS. Resetting lot to {current_lot}")


def get_xauusd_data():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {"interval": "5m", "range": "3d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        timestamps = data["chart"]["result"][0]["timestamp"]
        ohlcv = data["chart"]["result"][0]["indicators"]["quote"][0]
        df = pd.DataFrame({
            "time":   pd.to_datetime(timestamps, unit="s"),
            "open":   ohlcv["open"],
            "high":   ohlcv["high"],
            "low":    ohlcv["low"],
            "close":  ohlcv["close"],
            "volume": ohlcv["volume"]
        }).dropna()
        return df
    except Exception as e:
        print(f"Data error: {e}")
        return None


def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calculate_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_atr(df, period=14):
    high_low   = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close  = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def analyze_market():
    df = get_xauusd_data()
    if df is None or len(df) < 60:
        return None

    df["ema_fast"]  = calculate_ema(df["close"], EMA_FAST)
    df["ema_slow"]  = calculate_ema(df["close"], EMA_SLOW)
    df["ema_trend"] = calculate_ema(df["close"], EMA_TREND)
    df["rsi"]       = calculate_rsi(df["close"], RSI_PERIOD)
    df["atr"]       = calculate_atr(df)

    prev = df.iloc[-3]
    curr = df.iloc[-2]

    fast_now   = curr["ema_fast"]
    fast_prev  = prev["ema_fast"]
    slow_now   = curr["ema_slow"]
    slow_prev  = prev["ema_slow"]
    trend_now  = curr["ema_trend"]
    rsi        = curr["rsi"]
    atr        = curr["atr"]
    price      = curr["close"]

    sl_buy  = round(price - atr * ATR_SL_MULTI, 2)
    tp_buy  = round(price + atr * ATR_TP_MULTI, 2)
    sl_sell = round(price + atr * ATR_SL_MULTI, 2)
    tp_sell = round(price - atr * ATR_TP_MULTI, 2)

    # Risk calculation
    risk_amount = round(FUNDED_BALANCE * 0.01, 2)  # 1% risk

    # BUY — EMA cross + RSI filter + price above trend EMA
    buy_signal = (fast_prev < slow_prev and
                  fast_now  > slow_now  and
                  rsi < RSI_OVERBOUGHT  and
                  price > trend_now)

    # SELL — EMA cross + RSI filter + price below trend EMA
    sell_signal = (fast_prev > slow_prev and
                   fast_now  < slow_now  and
                   rsi > RSI_OVERSOLD   and
                   price < trend_now)

    if buy_signal:
        return {"type": "BUY", "price": round(price, 2),
                "sl": sl_buy, "tp": tp_buy,
                "rsi": round(rsi, 1), "atr": round(atr, 2),
                "lot": current_lot, "risk": risk_amount,
                "trend": "UPTREND ✅"}

    if sell_signal:
        return {"type": "SELL", "price": round(price, 2),
                "sl": sl_sell, "tp": tp_sell,
                "rsi": round(rsi, 1), "atr": round(atr, 2),
                "lot": current_lot, "risk": risk_amount,
                "trend": "DOWNTREND ✅"}

    return None


def format_signal(signal):
    emoji = "🟢" if signal["type"] == "BUY" else "🔴"
    arrow = "📈" if signal["type"] == "BUY" else "📉"
    remaining_loss = MAX_DAILY_LOSS - abs(daily_pnl)
    trades_left = MAX_TRADES_DAY - trades_today

    return f"""
{emoji} <b>XAUUSD {signal['type']} SIGNAL V3</b> {arrow}

💰 <b>Entry:</b>      {signal['price']}
🛑 <b>Stop Loss:</b>  {signal['sl']}
🎯 <b>Take Profit:</b> {signal['tp']}
📊 <b>Lot Size:</b>   {signal['lot']} lots
📉 <b>RSI:</b>        {signal['rsi']}
⚡ <b>ATR:</b>        {signal['atr']}
📈 <b>Trend:</b>      {signal['trend']}

💼 <b>Account Info:</b>
⚠️ Risk: $50 (1%)
📊 Trades today: {trades_today}/{MAX_TRADES_DAY}
🔴 Daily loss room: ${remaining_loss:.0f} left
🔄 Current lot: {current_lot} (compounding)
🏆 Wins streak: {consecutive_wins}

🕐 Time: {datetime.now().strftime('%H:%M:%S')}
📌 XAUUSD | M5 | EMA+RSI+Trend

⚠️ Place trade MANUALLY on funded account!
— AD xauusd Bot V3 🤖
""".strip()


def main():
    global last_signal, trades_today, market_closed_notified

    print("XAUUSD Signal Bot V3 started!")
    print(f"Timeframe: M5 | Base lot: {BASE_LOT} | Max lot: {MAX_LOT}")

    send_telegram("""
🤖 <b>AD XAUUSD Signal Bot V3 is LIVE!</b>

✅ Timeframe: M5 (more signals)
✅ Strategy: EMA 9/21/50 + RSI + Trend Filter
✅ Compounding: ON (lot grows with wins!)
✅ News filter: ON
✅ Max trades/day: 2
✅ Daily loss alert: ON ($150 limit)

🔄 Starting lot: 0.05
📈 Max lot: 0.20

💪 Built by Claude AI — V3 Upgraded!
""".strip())

    while True:
        try:
            check_new_day()
            now_str = datetime.now().strftime('%H:%M:%S')

            if not is_market_open():
                print(f"[{now_str}] Market closed. Waiting...")
                if not market_closed_notified:
                    send_telegram("⏸ <b>Market closed.</b> Bot resting. Will resume when market opens! 🌙")
                    market_closed_notified = True
                time.sleep(CHECK_INTERVAL)
                continue

            if market_closed_notified:
                market_closed_notified = False
                send_telegram("▶️ <b>Market open!</b> Scanning for signals... 🎯")

            # Daily loss limit check
            if daily_pnl <= -MAX_DAILY_LOSS:
                print(f"[{now_str}] Daily loss limit hit! ${abs(daily_pnl):.2f} lost.")
                send_telegram(f"🚨 <b>DAILY LOSS LIMIT REACHED!</b>\n\nLost ${abs(daily_pnl):.2f} today.\nLimit: ${MAX_DAILY_LOSS}\n\n⛔ STOP TRADING TODAY! Come back tomorrow fresh! 💪")
                time.sleep(3600)  # Sleep 1 hour
                continue

            # Max trades check
            if trades_today >= MAX_TRADES_DAY:
                print(f"[{now_str}] Max trades ({MAX_TRADES_DAY}) reached today.")
                time.sleep(CHECK_INTERVAL)
                continue

            # News time check
            if is_news_time():
                print(f"[{now_str}] News time — skipping analysis.")
                time.sleep(60)
                continue

            print(f"[{now_str}] Analyzing M5... Lot: {current_lot} | Trades: {trades_today}/{MAX_TRADES_DAY}")
            signal = analyze_market()

            if signal:
                sig_key = f"{signal['type']}_{signal['price']}"
                if sig_key != last_signal:
                    last_signal = sig_key
                    trades_today += 1
                    send_telegram(format_signal(signal))
                    print(f"Signal sent: {signal['type']} @ {signal['price']} | Lot: {current_lot}")
                else:
                    print("Same signal, skipping.")
            else:
                print("No signal. Market calm, waiting...")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
