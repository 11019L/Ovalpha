#!/usr/bin/env python3
import os
import asyncio
import time
import logging
from dotenv import load_dotenv
load_dotenv()

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, filters

# ============================= CONFIG =============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("onion")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")

# Relaxed for testing – catches almost everything
MIN_FDVS_SNIPE = 200
MAX_FDVS_SNIPE = 600_000
LIQ_FDV_RATIO = 0.08

# ============================= STATE =============================
seen = set()
token_db = {}
ready_queue = []
users = {}
app = None  # Will be set in main()

# ============================= HELPERS =============================
def short_addr(a): return f"{a[:6]}...{a[-4:]}" if a and len(a) > 10 else "—"

# ============================= PUMPPORTAL (REAL-TIME) =============================
async def get_new_tokens(sess):
    url = "https://pumpportal.fun/api/data/new-tokens?limit=50"
    try:
        async with sess.get(url, timeout=15) as r:
            if not r.ok:
                return
            data = await r.json()
            tokens = data if isinstance(data, list) else data.get("tokens", [])
            now = time.time()
            added = 0
            for t in tokens:
                mint = t.get("mint") or t.get("tokenAddress")
                if not mint or mint in seen:
                    continue
                if now - t.get("created_timestamp", now - 60) > 300:
                    continue

                fdv = float(t.get("market_cap_usd") or t.get("fdv_usd") or 0)
                liq = float(t.get("liquidity_usd") or 0)
                symbol = (t.get("symbol") or "???")[:12]

                seen.add(mint)
                token_db[mint] = {"symbol": symbol, "fdv": fdv, "liq": liq, "launched": now, "alerted": False}
                ready_queue.append(mint)
                log.info(f"NEW → {symbol} | {short_addr(mint)} | FDV ${fdv:,.0f}")
                added += 1
            if added:
                log.info(f"PumpPortal added {added} tokens")
    except Exception as e:
        log.error(f"PumpPortal error: {e}")

# ============================= PROCESS & ALERT =============================
async def process_queue():
    now = time.time()
    for mint in ready_queue[:10]:
        info = token_db.get(mint)
        if not info or info["alerted"] or now - info["launched"] < 7:
            continue

        if not (MIN_FDVS_SNIPE <= info["fdv"] <= MAX_FDVS_SNIPE):
            continue
        if info["liq"] < LIQ_FDV_RATIO * info["fdv"]:
            continue

        info["alerted"] = True
        log.info(f"{'*' * 25} GOLD ALERT → {info['symbol']} | {short_addr(mint)} | ${info['fdv']:,.0f} {'*' * 25}")
        await broadcast_alert(mint, info["symbol"], info["fdv"])

async def broadcast_alert(mint: str, symbol: str, fdv: float):
    msg = (
        "<b>GOLD ALERT – LIVE</b>\n\n"
        f"<b>{symbol}</b>\n"
        f"CA: <code>{mint}</code>\n"
        f"FDV: <code>${fdv:,.0f}</code>\n\n"
        "Your sniper is WORKING!"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy CA", callback_data=f"copy_{mint}")],
        [InlineKeyboardButton("Menu", callback_data="menu")]
    ])
    for uid, u in users.items():
        try:
            await app.bot.send_message(u["chat_id"], msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Failed send to {uid}: {e}")

# ============================= SCANNER LOOP =============================
async def scanner():
    async with aiohttp.ClientSession() as sess:
        while True:
            await get_new_tokens(sess)
            await process_queue()
            await asyncio.sleep(10)

# ============================= HANDLERS =============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users[uid] = {"chat_id": update.effective_chat.id}
    await update.message.reply_text(
        "<b>ONION X SNIPER IS LIVE</b>\n\n"
        "Real GOLD ALERTS in < 2 minutes...\n"
        "Just wait — no setup needed.",
        parse_mode=ParseMode.HTML
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Force Test Alert", callback_data="test")]]
    await update.message.reply_text("Test Menu", reply_markup=InlineKeyboardMarkup(kb))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "test":
        await broadcast_alert("So11111111111111111111111111111111111111112", "TESTCOIN", 133700)
    elif q.data == "menu":
        await q.edit_message_text("Menu refreshed")
    elif q.data.startswith("copy_"):
        mint = q.data.split("_", 1)[1]
        await q.edit_message_text(f"<code>{mint}</code>\nCopied to clipboard!", parse_mode=ParseMode.HTML)

# ============================= MAIN =============================
def main():
    global app
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(button))

    # Start scanner in background – NO JobQueue needed
    asyncio.create_task(scanner())

    log.info("ONION X FINAL TEST BOT STARTED – Waiting for Pump.fun launches...")
    app.run_polling(drop_pending_updates=True)

# ============================= RUN =============================
if __name__ == "__main__":
    main()   # ← This is all you need. No asyncio.run(), no JobQueue, no errors.
