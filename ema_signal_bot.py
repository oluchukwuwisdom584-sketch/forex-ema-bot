import os
import time
import json
import logging
import threading
from datetime import datetime

import requests
import pandas as pd
from dotenv import load_dotenv

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import Updater, CallbackContext, CommandHandler, CallbackQueryHandler, Dispatcher

# -------------------
# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALPHA_VANTAGE_API_KEY = os.getenv("AV_API_KEY")

# -------------------
# Config
CHECK_INTERVAL_SECONDS = 60
PAIRS = ["EURUSD"]  # Forex pairs
TREND_EMA = 32
ENTRY_EXIT_EMA = 14
TIMEFRAME = "15min"  # Alpha Vantage allowed: 1min,5min,15min,30min,60min
STATE_FILE = "bot_state.json"
LOG_FILE = "ema_signal_bot.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
DEFAULT_CHAT_ID_KEY = "default_chat_id"

default_state = {
    "running": False,
    "pairs": PAIRS,
    "trend_ema": TREND_EMA,
    "entry_exit_ema": ENTRY_EXIT_EMA,
    "timeframe": TIMEFRAME,
    "per_pair": {},
    DEFAULT_CHAT_ID_KEY: None,
}

# -------------------
# State management
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
        for k in default_state:
            if k not in s:
                s[k] = default_state[k]
        return s
    s = default_state.copy()
    for p in s["pairs"]:
        s["per_pair"].setdefault(p, {"in_trade": False, "side": None})
    return s

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

state = load_state()

# -------------------
# Fetch Forex OHLC from Alpha Vantage
def fetch_ohlc_fx(pair, interval, outputsize="compact"):
    from_sym = pair[:3]
    to_sym = pair[3:]
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_INTRADAY",
        "from_symbol": from_sym,
        "to_symbol": to_sym,
        "interval": interval,
        "apikey": ALPHA_VANTAGE_API_KEY,
        "outputsize": outputsize,
        "datatype": "json",
    }
    r = requests.get(url, params=params)
    data = r.json()
    key = f"Time Series FX ({interval})"
    if key not in data:
        logger.warning("No data for %s %s: %s", pair, interval, data)
        return None
    df = pd.DataFrame.from_dict(data[key], orient="index")
    df = df.rename(columns={
        "1. open": "Open",
        "2. high": "High",
        "3. low": "Low",
        "4. close": "Close"
    })
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df

def add_ema(df, period, col="Close", name=None):
    if name is None:
        name = f"EMA_{period}"
    df[name] = df[col].astype(float).ewm(span=period, adjust=False).mean()
    return df

# -------------------
# Telegram UI
def control_keyboard(running):
    if running:
        btn = InlineKeyboardButton("🛑 Stop Bot", callback_data="stop")
    else:
        btn = InlineKeyboardButton("▶️ Start Bot", callback_data="start")
    keyboard = [[btn], [InlineKeyboardButton("🔁 Status", callback_data="status")]]
    return InlineKeyboardMarkup(keyboard)

def send_message(chat_id, text):
    bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

# -------------------
# EMA signal logic
def evaluate_pair(pair, chat_id):
    df = fetch_ohlc_fx(pair, state["timeframe"])
    if df is None or df.empty:
        return
    df = add_ema(df, state["trend_ema"], name="TrendEMA")
    df = add_ema(df, state["entry_exit_ema"], name="EntryEMA")
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df)>=2 else latest
    close = float(latest["Close"])
    trend = float(latest["TrendEMA"])
    entry = float(latest["EntryEMA"])
    pair_state = state["per_pair"].setdefault(pair, {"in_trade": False, "side": None})

    def alert(msg):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        send_message(chat_id, f"<b>{pair} — {state['timeframe']}</b>\n{msg}\n<i>{ts}</i>")

    # BUY
    if close > trend:
        if not pair_state["in_trade"]:
            if float(prev["Close"]) <= entry and close > entry:
                pair_state["in_trade"] = True
                pair_state["side"] = "BUY"
                alert("✅ <b>BUY NOW</b>")
        else:
            if pair_state["side"] == "BUY":
                if float(prev["Close"]) >= entry and close < entry:
                    pair_state["in_trade"] = False
                    pair_state["side"] = None
                    alert("❌ <b>EXIT BUY</b>")

    # SELL
    elif close < trend:
        if not pair_state["in_trade"]:
            if float(prev["Close"]) >= entry and close < entry:
                pair_state["in_trade"] = True
                pair_state["side"] = "SELL"
                alert("✅ <b>SELL NOW</b>")
        else:
            if pair_state["side"] == "SELL":
                if float(prev["Close"]) <= entry and close > entry:
                    pair_state["in_trade"] = False
                    pair_state["side"] = None
                    alert("❌ <b>EXIT SELL</b>")

    state["per_pair"][pair] = pair_state
    save_state(state)

# -------------------
# Monitoring loop
def monitoring_loop(dispatcher: Dispatcher):
    while True:
        try:
            if not state.get("running", False):
                time.sleep(5)
                continue
            chat_id = state.get(DEFAULT_CHAT_ID_KEY)
            if not chat_id:
                time.sleep(5)
                continue
            for p in state.get("pairs", []):
                try:
                    evaluate_pair(p, chat_id)
                except Exception:
                    logger.exception("Error evaluating %s", p)
            time.sleep(CHECK_INTERVAL_SECONDS)
        except Exception:
            logger.exception("Unexpected error in monitoring loop")
            time.sleep(5)

# -------------------
# Telegram Handlers
def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    state[DEFAULT_CHAT_ID_KEY] = chat_id
    save_state(state)
    update.message.reply_text(
        "Hello! EMA‑Signal Bot ready.\nUse buttons below.",
        reply_markup=control_keyboard(state.get("running", False))
    )

def button_cb(update: Update, context: CallbackContext):
    q = update.callback_query
    q.answer()
    if q.data == "start":
        state["running"] = True
        save_state(state)
        q.edit_message_text("▶️ Bot started.", reply_markup=control_keyboard(True))
    elif q.data == "stop":
        state["running"] = False
        save_state(state)
        q.edit_message_text("🛑 Bot stopped.", reply_markup=control_keyboard(False))
    elif q.data == "status":
        running = state.get("running", False)
        text = (
            f"Pairs: {', '.join(state.get('pairs', []))}\n"
            f"Trend EMA: {state.get('trend_ema')}\n"
            f"Entry/Exit EMA: {state.get('entry_exit_ema')}\n"
            f"Timeframe: {state.get('timeframe')}\n"
            f"Running: {running}"
        )
        q.edit_message_text(text, reply_markup=control_keyboard(running))

def add_pair_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /add <PAIR> e.g. /add GBPUSD")
        return
    pair = context.args[0].upper()
    if pair in state["pairs"]:
        update.message.reply_text(f"{pair} already monitored.")
        return
    state["pairs"].append(pair)
    state["per_pair"].setdefault(pair, {"in_trade": False, "side": None})
    save_state(state)
    update.message.reply_text(f"Added {pair}.")

def remove_pair_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /remove <PAIR>")
        return
    pair = context.args[0].upper()
    if pair not in state["pairs"]:
        update.message.reply_text(f"{pair} not in watchlist.")
        return
    state["pairs"].remove(pair)
    state["per_pair"].pop(pair, None)
    save_state(state)
    update.message.reply_text(f"Removed {pair}.")

def set_ema_command(update: Update, context: CallbackContext):
    if len(context.args) < 2:
        update.message.reply_text("Usage: /setema <trend|entry> <period>")
        return
    which = context.args[0].lower()
    try:
        val = int(context.args[1])
    except:
        update.message.reply_text("Period must be integer.")
        return
    if which == "trend":
        state["trend_ema"] = val
    elif which in ("entry", "exit", "entry_exit"):
        state["entry_exit_ema"] = val
    else:
        update.message.reply_text("Which must be 'trend' or 'entry'.")
        return
    save_state(state)
    update.message.reply_text(f"Set {which} EMA to {val}.")

# -------------------
# Main
def main():
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add_pair_command))
    dp.add_handler(CommandHandler("remove", remove_pair_command))
    dp.add_handler(CommandHandler("setema", set_ema_command))
    dp.add_handler(CallbackQueryHandler(button_cb))
    updater.start_polling()
    monitor = threading.Thread(target=monitoring_loop, args=(dp,), daemon=True)
    monitor.start()
    updater.idle()

if __name__ == "__main__":
    main()
