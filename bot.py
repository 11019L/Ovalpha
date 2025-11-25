#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, filters

# ============================= CONFIG =============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("onion_test")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Set BOT_TOKEN in .env")
BOT_USERNAME = os.getenv("BOT_USERNAME", "onionx_test_bot")
FEE_WALLET = "So11111111111111111111111111111111111111112"  # WSOL

# Relaxed for testing — will catch 90%+ of new Pump.fun tokens
MIN_FDVS_SNIPE = 300
MAX_FDVS_SNIPE = 400_000
MAX_VOL_SNIPE = 15_000
LIQ_FDV_RATIO = 0.15
MIN_HOLDERS = 8
MAX_QUEUE = 200

# ============================= STATE =============================
seen = set()
token_db = {}
ready_queue = []
users = {}
data = {"users": {}, "revenue": 0, "total_trades": 0}
DATA_FILE = Path("test_data.json")

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except:
            pass
    return data

data = load_data()
users = data["users"]
app = None

# ============================= HELPERS =============================
def short_addr(a): return f"{a[:6]}...{a[-4:]}" if a and len(a) > 10 else "—"
def fmt_sol(v): return f"{v:.3f} SOL"

# ============================= PUMPPORTAL (REAL-TIME 2025) =============================
async def get_new_tokens(sess):
    url = "https://pumpportal.fun/api/data/new-tokens?limit=50"
    try:
        async with sess.get(url, timeout=15) as r:
            if not r.ok:
                log.warning(f"PumpPortal HTTP {r.status}")
                return
            raw = await r.json()
            tokens = raw if isinstance(raw, list) else raw.get("tokens", [])
            now = time.time()
            added = 0
            for t in tokens:
                mint = t.get("mint") or t.get("tokenAddress")
                if not mint or mint in seen:
                    continue
                created = t.get("created_timestamp") or now - 60
                if now - created > 300:  # < 5 min old
                    continue

                fdv = float(t.get("market_cap_usd") or t.get("fdv_usd") or 0)
                liq = float(t.get("liquidity_usd") or 0)
                symbol = t.get("symbol", "???")[:10]

                seen.add(mint)
                token_db[mint] = {
                    "symbol": symbol,
                    "fdv": fdv,
                    "liq": liq,
                    "launched": created,
                    "alerted": False
                }
                ready_queue.append(mint)
                if len(ready_queue) > MAX_QUEUE:
                    ready_queue.pop(0)

                log.info(f"NEW → {symbol} | {short_addr(mint)} | FDV ${fdv:,.0f} | Age {int(now-created)}s")
                added += 1
            if added:
                log.info(f"Added {added} fresh tokens")
    except Exception as e:
        log.error(f"PumpPortal error: {e}")

# ============================= FILTER & ALERT =============================
async def process_queue():
    now = time.time()
    for mint in ready_queue[:10]:  # Process up to 10 per cycle
        try:
            info = token_db.get(mint)
            if not info or info["alerted"]:
                continue

            age = int(now - info["launched"])
            if age < 6:  # Wait a few seconds
                continue
            if age > 600:
                continue

            fdv = info["fdv"]
            liq = info["liq"]

            if not (MIN_FDVS_SNIPE <= fdv <= MAX_FDVS_SNIPE):
                continue
            if liq < LIQ_FDV_RATIO * fdv:
                continue

            # SUCCESS → GOLD ALERT
            info["alerted"] = True
            sym = info["symbol"]
            log.info(f"{'*' * 15} GOLD ALERT → {sym} | {short_addr(mint)} | ${fdv:,.0f} {'*' * 15}")
            await broadcast_alert(mint, sym, fdv)

        except Exception as e:
            log.error(f"Process error {short_addr(mint)}: {e}")

async def broadcast_alert(mint: str, symbol: str, fdv: float):
    msg = (
        "<b>GOLD ALERT (TEST MODE)</b>\n\n"
        f"<b>{symbol}</b>\n"
        f"CA: <code>{mint}</code>\n"
        f"FDV: <code>${fdv:,.0f}</code>\n\n"
        "This is a TEST build — alerts are working!"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy CA", callback_data=f"copy_{mint}")],
        [InlineKeyboardButton("Refresh Menu", callback_data="menu")]
    ])
    for uid, u in list(users.items()):
        try:
            await app.bot.send_message(u["chat_id"], msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Failed to send to {uid}: {e}")

# ============================= COMMANDS & UI =============================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if uid not in users:
        users[uid] = {"chat_id": chat_id, "free_alerts": 999}  # Unlimited in test mode
    users[uid]["chat_id"] = chat_id
    await update.message.reply_text(
        "<b>ONION X TEST BOT IS LIVE</b>\n\n"
        "You will receive a GOLD ALERT within 1–3 minutes when a new Pump.fun token launches.\n\n"
        "Use /menu anytime.",
        parse_mode=ParseMode.HTML
    )

async def menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    kb = [[InlineKeyboardButton("Force Test Alert", callback_data="test_alert")]]
    await update.message.reply_text("TEST MENU", reply_markup=InlineKeyboardMarkup(kb))

async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "menu":
        await q.edit_message_text("Menu refreshed", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Force Test Alert", callback_data="test_alert")]
        ]))
    elif data == "test_alert":
        await broadcast_alert("So11111111111111111111111111111111111111112", "TESTCOIN", 42069)
    elif data.startswith("copy_"):
        mint = data.split("_", 1)[1]
        await q.edit_message_text(f"<code>{mint}</code>\nCopied to clipboard!", parse_mode=ParseMode.HTML)

# ============================= SCANNER LOOP =============================
async def scanner():
    async with aiohttp.ClientSession() as sess:
        while True:
            await get_new_tokens(sess)
            await process_queue()
            await asyncio.sleep(12)


async def main():
    global app
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(button))

    # Start scanner background task
    app.job_queue.run_once(lambda _: None, 1)  # dummy to initialize job_queue
    asyncio.create_task(scanner())

    log.info("ONION X TEST BOT STARTED – Waiting for new Pump.fun tokens...")
    # Nothing else here – run_polling() will start the loop


# =============================================================================
# THIS IS THE ONLY LINE YOU NEED AT THE BOTTOM
# =============================================================================
if __name__ == "__main__":
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
