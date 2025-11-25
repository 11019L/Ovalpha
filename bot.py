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
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ============================= CONFIG =============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("onion_test")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Set BOT_TOKEN in .env")

# Relaxed filters for testing
MIN_FDVS_SNIPE = 300
MAX_FDVS_SNIPE = 500_000
MAX_VOL_SNIPE = 20_000
LIQ_FDV_RATIO = 0.1
MIN_HOLDERS = 5
MAX_QUEUE = 200

# ============================= STATE =============================
seen = set()
token_db = {}
ready_queue = []
users = {}

# ============================= HELPERS =============================
def short_addr(a): return f"{a[:6]}...{a[-4:]}" if a and len(a) > 10 else "—"

# ============================= PUMPPORTAL (REAL-TIME 2025) =============================
async def get_new_tokens(sess):
    url = "https://pumpportal.fun/api/data/new-tokens?limit=50"
    try:
        async with sess.get(url, timeout=15) as r:
            if not r.ok:
                return
            raw = await r.json()
            tokens = raw if isinstance(raw, list) else raw.get("tokens", [])
            now = time.time()
            added = 0
            for t in tokens:
                mint = t.get("mint") or t.get("tokenAddress")
                if not mint or mint in seen:
                    continue
                created = t.get("created_timestamp", now - 60)
                if now - created > 300:  # < 5 min old
                    continue

                fdv = float(t.get("market_cap_usd") or t.get("fdv_usd") or 0)
                liq = float(t.get("liquidity_usd") or 0)
                symbol = (t.get("symbol") or "???")[:12]

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

                log.info(f"NEW → {symbol} | {short_addr(mint)} | FDV ${fdv:,.0f}")
                added += 1
            if added:
                log.info(f"PumpPortal added {added} new tokens")
    except Exception as e:
        log.error(f"PumpPortal error: {e}")

# ============================= PROCESS & ALERT =============================
async def process_queue():
    now = time.time()
    for mint in ready_queue[:10]:
        info = token_db.get(mint)
        if not info or info["alerted"]:
            continue
        if now - info["launched"] < 6:
            continue

        fdv = info["fdv"]
        liq = info["liq"]
        if not (MIN_FDVS_SNIPE <= fdv <= MAX_FDVS_SNIPE):
            continue
        if liq < LIQ_FDV_RATIO * fdv:
            continue

        info["alerted"] = True
        log.info(f"{'*' * 20} GOLD ALERT → {info['symbol']} | {short_addr(mint)} | ${fdv:,.0f} {'*' * 20}")
        await broadcast_alert(mint, info["symbol"], fdv)

async def broadcast_alert(mint: str, symbol: str, fdv: float):
    msg = (
        "<b>GOLD ALERT (TEST BOT)</b>\n\n"
        f"<b>{symbol}</b>\n"
        f"CA: <code>{mint}</code>\n"
        f"FDV: <code>${fdv:,.0f}</code>\n\n"
        "Your sniper is ALIVE!"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy CA", callback_data=f"copy_{mint}")],
        [InlineKeyboardButton("Menu", callback_data="menu")]
    ])
    for uid, u in users.items():
        try:
            await app.bot.send_message(u["chat_id"], msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Send failed to {uid}: {e}")

# ============================= SCANNER LOOP =============================
async def scanner():
    async with aiohttp.ClientSession() as sess:
        while True:
            await get_new_tokens(sess)
            await process_queue()
            await asyncio.sleep(12)

# ============================= HANDLERS =============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    users[uid] = {"chat_id": chat_id}
    await update.message.reply_text(
        "<b>ONION X TEST BOT IS LIVE</b>\n\n"
        "You will receive a real GOLD ALERT in the next 30–120 seconds.\n"
        "Just wait...",
        parse_mode=ParseMode.HTML
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Force Test Alert", callback_data="test")]]
    await update.message.reply_text("Test Menu", reply_markup=InlineKeyboardMarkup(kb))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "test":
        await broadcast_alert("So11111111111111111111111111111111111111112", "TESTCOIN", 69696)
    elif q.data == "menu":
        await q.edit_message_text("Menu refreshed")
    elif q.data.startswith("copy_"):
        mint = q.data.split("_", 1)[1]
        await q.edit_message_text(f"<code>{mint}</code>\nCopied!", parse_mode=ParseMode.HTML)

# ============================= BUILD & RUN =============================
def main():
    builder = Application.builder().token(BOT_TOKEN)
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(button))

    # Start scanner in background
    app.job_queue.run_once(lambda ctx: None, 0)  # force job_queue init
    asyncio.create_task(scanner())

    log.info("ONION X TEST BOT STARTED – Waiting for new Pump.fun tokens...")
    
    # THIS IS THE ONLY CORRECT WAY IN 2025
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

# =============================================================================
# RUN IT
# =============================================================================
if __name__ == "__main__":
    main()        # ← No asyncio.run()! app.run_polling() owns the loop
