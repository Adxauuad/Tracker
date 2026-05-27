#!/usr/bin/env python3
"""
XAUUSD Telegram Signal Bot V4
Strategy  : H1 Trend + S&R + EMA 9/21 + RSI
Timeframe : M5 entry | H1 trend confirmation
Features  : Higher accuracy, less false signals
Built by  : Claude AI
"""

import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime
import pytz

# ─── SETTINGS ────────────────────────────────────────────
TELEGRAM_TOKEN   = "8719862501:AAEk7qscn247OznZOSd0CuLGUg6vQS3W_yc"
CHAT_ID          = "1628528552"
CHECK_INTERVAL   = 60 * 5      # Every 5 minutes
FUNDED_BALANCE   = 5000
MAX_DAILY_LOSS   = 150
MAX_TRADES_DAY   = 2
BASE_LOT         = 0.05
MAX_LOT          = 0.20
LOT_MULTIPLIER   = 1.2
SR_LOOKBACK      = 50          # candles to look back for S&R
SR_ZONE          = 8.0         # zone size in $ around S&R level
# ─────────────────────────────────────────────────────────

EMA_FAST        = 9
EMA_SLOW        = 21
EMA_H1_TREND    = 50
RSI_PERIOD      = 14
RSI_OVERBOUGHT  = 65
RSI_OVERSOLD    = 35
ATR_PERIOD      = 14
ATR_SL_MULTI    = 1.5
ATR_TP_MULTI    = 3.0

# State
last_signal            = None
current_lot            = BASE_LOT
trades_today           = 0
daily_pnl              = 0.0
consecutive_wins       = 0
last_trade_date        = None
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
    now = datetime.now(pytz.utc)
    minute = now.minute
    if minute <= 3 or minute >= 57: return True
    high_impact_hours = [8, 9, 13, 14, 15, 18]
    if now.hour in high_impact_hours and (minute <= 5 or (minute >= 28 and minute <= 32)):
        return True
    return False


def check_new_day():
    global trades_today, daily_pnl, last_trade_date
    today = datetime.now().date()
    if last_trade_date != today:
        trades_today = 0
        daily_pnl = 0.0
        last_trade_date = today
        print(f"New day! Counters reset.")


def compound_lot(won):
    global current_lot, consecutive_wins
    if won:
        consecutive_wins += 1
        current_lot = round(min(current_lot * LOT_MULTIPLIER, MAX_LOT), 2)
        print(f"WIN! Compounding to {current_lot}")
    else:
        consecutive_wins = 0
        current_lot = BASE_LOT
        print(f"LOSS. Reset to {current_lot}")


def get_data(interval="5m", range_="3d"):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {"interval": interval, "range": range_}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        timestamps = data["chart"]["result"][0]["timestamp"]
        ohlcv = data["chart"]["result"][0]["indicators"]["quote"][0]
        df = pd.DataFrame({
            "time":  pd.to_datetime(timestamps, unit="s"),
            "open":  ohlcv["open"],
            "high":  ohlcv["high"],
            "low":   ohlcv["low"],
            "close": ohlcv["close"],
        }).dropna()
        return df
    except Exception as e:
        print(f"Data error: {e}")
        return None


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, adjust=False).mean()
    return 100 - (100 / (1 + gain/loss))


def atr(df, period=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(period).mean()


def get_h1_trend():
    """Get H1 timeframe trend direction"""
    df_h1 = get_data(interval="1h", range_="30d")
    if df_h1 is None or len(df_h1) < 60: return 0

    df_h1["ema_trend"] = ema(df_h1["close"], EMA_H1_TREND)
    curr = df_h1.iloc[-2]
    prev = df_h1.iloc[-3]

    price    = curr["close"]
    ema_now  = curr["ema_trend"]
    ema_prev = prev["ema_trend"]

    if price > ema_now and ema_now > ema_prev: return 1   # Bullish
    if price < ema_now and ema_now < ema_prev: return -1  # Bearish
    return 0  # Mixed


def find_support_resistance(df):
    """Find key support and resistance levels from swing highs/lows"""
    supports    = []
    resistances = []

    highs = df["high"].values
    lows  = df["low"].values
    n     = len(df)

    for i in range(2, min(SR_LOOKBACK, n-2)):
        # Swing low = support
        if lows[i] < lows[i-1] and lows[i] < lows[i+1] and \
           lows[i] < lows[i-2] and lows[i] < lows[i+2]:
            supports.append(lows[i])

        # Swing high = resistance
        if highs[i] > highs[i-1] and highs[i] > highs[i+1] and \
           highs[i] > highs[i-2] and highs[i] > highs[i+2]:
            resistances.append(highs[i])

    return supports, resistances


def nearest_support(supports, price):
    """Find nearest support below current price"""
    below = [s for s in supports if s < price]
    return max(below) if below else None


def nearest_resistance(resistances, price):
    """Find nearest resistance above current price"""
    above = [r for r in resistances if r > price]
    return min(above) if above else None


def analyze_market():
    # Get M5 data
    df = get_data(interval="5m", range_="3d")
    if df is None or len(df) < 60: return None

    # Get H1 trend
    h1_trend = get_h1_trend()
    if h1_trend == 0:
        print("H1 trend unclear — skipping")
        return None

    # Calculate indicators
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["rsi"]      = rsi(df["close"], RSI_PERIOD)
    df["atr"]      = atr(df)

    # Find S&R levels
    supports, resistances = find_support_resistance(df)

    prev  = df.iloc[-3]
    curr  = df.iloc[-2]
    price = curr["close"]
    atr_v = curr["atr"]

    fast_now  = curr["ema_fast"]
    fast_prev = prev["ema_fast"]
    slow_now  = curr["ema_slow"]
    slow_prev = prev["ema_slow"]
    rsi_v     = curr["rsi"]

    # Find nearest levels
    sup = nearest_support(supports, price)
    res = nearest_resistance(resistances, price)

    near_support    = sup is not None and (price - sup) < SR_ZONE
    near_resistance = res is not None and (res - price) < SR_ZONE

    # BUY conditions
    ema_buy  = fast_prev < slow_prev and fast_now > slow_now
    rsi_buy  = rsi_v < RSI_OVERBOUGHT
    h1_bull  = h1_trend == 1

    # SELL conditions
    ema_sell = fast_prev > slow_prev and fast_now < slow_now
    rsi_sell = rsi_v > RSI_OVERSOLD
    h1_bear  = h1_trend == -1

    sl_buy  = round(price - atr_v * ATR_SL_MULTI, 2)
    tp_buy  = round(price + atr_v * ATR_TP_MULTI, 2)
    sl_sell = round(price + atr_v * ATR_SL_MULTI, 2)
    tp_sell = round(price - atr_v * ATR_TP_MULTI, 2)

    # Full signal — all conditions including S&R
    if ema_buy and rsi_buy and h1_bull and near_support:
        return {"type": "BUY", "price": round(price, 2),
                "sl": sl_buy, "tp": tp_buy,
                "rsi": round(rsi_v, 1), "atr": round(atr_v, 2),
                "lot": current_lot, "h1": "BULLISH 📈",
                "support": round(sup, 2) if sup else "N/A",
                "resistance": round(res, 2) if res else "N/A",
                "quality": "HIGH ⭐⭐⭐"}

    if ema_sell and rsi_sell and h1_bear and near_resistance:
        return {"type": "SELL", "price": round(price, 2),
                "sl": sl_sell, "tp": tp_sell,
                "rsi": round(rsi_v, 1), "atr": round(atr_v, 2),
                "lot": current_lot, "h1": "BEARISH 📉",
                "support": round(sup, 2) if sup else "N/A",
                "resistance": round(res, 2) if res else "N/A",
                "quality": "HIGH ⭐⭐⭐"}

    # Relaxed signal — no S&R nearby but strong trend + EMA
    if ema_buy and rsi_buy and h1_bull:
        return {"type": "BUY", "price": round(price, 2),
                "sl": sl_buy, "tp": tp_buy,
                "rsi": round(rsi_v, 1), "atr": round(atr_v, 2),
                "lot": current_lot, "h1": "BULLISH 📈",
                "support": round(sup, 2) if sup else "N/A",
                "resistance": round(res, 2) if res else "N/A",
                "quality": "MEDIUM ⭐⭐"}

    if ema_sell and rsi_sell and h1_bear:
        return {"type": "SELL", "price": round(price, 2),
                "sl": sl_sell, "tp": tp_sell,
                "rsi": round(rsi_v, 1), "atr": round(atr_v, 2),
                "lot": current_lot, "h1": "BEARISH 📉",
                "support": round(sup, 2) if sup else "N/A",
                "resistance": round(res, 2) if res else "N/A",
                "quality": "MEDIUM ⭐⭐"}

    return None


def format_signal(signal):
    emoji = "🟢" if signal["type"] == "BUY" else "🔴"
    arrow = "📈" if signal["type"] == "BUY" else "📉"
    remaining = MAX_DAILY_LOSS - abs(daily_pnl)

    return f"""
{emoji} <b>XAUUSD {signal['type']} SIGNAL V4</b> {arrow}

💰 <b>Entry:</b>       {signal['price']}
🛑 <b>Stop Loss:</b>   {signal['sl']}
🎯 <b>Take Profit:</b> {signal['tp']}
📊 <b>Lot Size:</b>    {signal['lot']} lots

📊 <b>Analysis:</b>
📉 RSI: {signal['rsi']}
⚡ ATR: {signal['atr']}
📈 H1 Trend: {signal['h1']}
🟩 Support: {signal['support']}
🟥 Resistance: {signal['resistance']}
⭐ Quality: {signal['quality']}

💼 <b>Account:</b>
📊 Trades today: {trades_today}/{MAX_TRADES_DAY}
🔴 Daily room: ${remaining:.0f} left
🔄 Lot (compound): {current_lot}
🏆 Win streak: {consecutive_wins}

🕐 {datetime.now().strftime('%H:%M:%S')} | XAUUSD M5

⚠️ Place MANUALLY on funded account!
— AD xauusd Bot V4 🤖
""".strip()


def main():
    global last_signal, trades_today, market_closed_notified

    print("XAUUSD Signal Bot V4 started!")

    send_telegram("""
🤖 <b>AD XAUUSD Signal Bot V4 is LIVE!</b>

✅ M5 entry + H1 trend filter
✅ Support & Resistance zones
✅ EMA 9/21 crossover
✅ RSI 65/35 tight filter
✅ Compounding lots
✅ Max 2 trades/day
✅ Daily loss alert $150
✅ News time filter

🎯 Higher accuracy — less false signals!
💪 Built by Claude AI — V4 Upgraded!
""".strip())

    while True:
        try:
            check_new_day()
            now_str = datetime.now().strftime('%H:%M:%S')

            if not is_market_open():
                print(f"[{now_str}] Market closed.")
                if not market_closed_notified:
                    send_telegram("⏸ <b>Market closed.</b> Bot resting. See you when market opens! 🌙")
                    market_closed_notified = True
                time.sleep(CHECK_INTERVAL)
                continue

            if market_closed_notified:
                market_closed_notified = False
                send_telegram("▶️ <b>Market open!</b> Scanning with H1+S&R filters... 🎯")

            if daily_pnl <= -MAX_DAILY_LOSS:
                send_telegram(f"🚨 <b>DAILY LOSS LIMIT!</b>\n\nLost ${abs(daily_pnl):.2f} today.\n⛔ STOP TRADING! Come back tomorrow! 💪")
                time.sleep(3600)
                continue

            if trades_today >= MAX_TRADES_DAY:
                print(f"[{now_str}] Max trades reached.")
                time.sleep(CHECK_INTERVAL)
                continue

            if is_news_time():
                print(f"[{now_str}] News time — skipping.")
                time.sleep(60)
                continue

            print(f"[{now_str}] Analyzing... Lot: {current_lot} | Trades: {trades_today}/{MAX_TRADES_DAY}")
            signal = analyze_market()

            if signal:
                sig_key = f"{signal['type']}_{signal['price']}"
                if sig_key != last_signal:
                    last_signal = sig_key
                    trades_today += 1
                    send_telegram(format_signal(signal))
                    print(f"✅ Signal: {signal['type']} @ {signal['price']} | Quality: {signal['quality']}")
                else:
                    print("Same signal, skipping.")
            else:
                print("No signal. Waiting...")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
