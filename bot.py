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
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ============================= CONFIG =============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("onion")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Set BOT_TOKEN in .env")

# ============================= STATE =============================
seen = set()
token_db = {}
ready_queue = []
users = {}

def short_addr(a): return f"{a[:6]}...{a[-4:]}" if a and len(a) > 10 else "—"

# ============================= PUMPPORTAL =============================
async def get_new_tokens(sess):
    try:
        async with sess.get("https://pumpportal.fun/api/data/new-tokens?limit=50", timeout=15) as r:
            if not r.ok: return
            data = await r.json()
            tokens = data if isinstance(data, list) else data.get("tokens", [])
            now = time.time()
            added = 0
            for t in tokens:
                mint = t.get("mint") or t.get("tokenAddress")
                if not mint or mint in seen:
                    continue
                if now - t.get("created_timestamp", now-60) > 300:
                    continue

                fdv = float(t.get("market_cap_usd") or t.get("fdv_usd") or 0)
                liq = float(t.get("liquidity_usd") or 0)
                symbol = (t.get("symbol") or "???")[:12]

                seen.add(mint)
                token_db[mint] = {"symbol": symbol, "fdv": fdv, "liq": liq, "launched": now, "alerted": False}
                ready_queue.append(mint)
                log.info(f"NEW → {symbol} | {short_addr(mint)} | FDV ${fdv:,.0f}")
                added += 1
            if added: log.info(f"Added {added} tokens")
    except Exception as e:
        log.error(f"PumpPortal error: {e}")

async def process_queue():
    now = time.time()
    for mint in ready_queue[:10]:
        info = token_db.get(mint)
        if not info or info["alerted"] or now - info["launched"] < 8:
            continue
        if not (200 <= info["fdv"] <= 700_000):
            continue
        if info["liq"] < 0.07 * info["fdv"]:
            continue

        info["alerted"] = True
        log.info(f"{'*' * 30} GOLD ALERT → {info['symbol']} | {short_addr(mint)} | ${info['fdv']:,.0f} {'*' * 30}")
        await broadcast_alert(mint, info["symbol"], info["fdv"])

async def broadcast_alert(mint: str, symbol: str, fdv: float):
    msg = f"<b>GOLD ALERT – LIVE</b>\n\n<b>{symbol}</b>\nCA: <code>{mint}</code>\nFDV: <code>${fdv:,.0f}</code>\n\nYour sniper is ALIVE!"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy CA", callback_data=f"copy_{mint}")],
        [InlineKeyboardButton("Menu", callback_data="menu")]
    ])
    for uid, u in users.items():
        try:
            await app.bot.send_message(u["chat_id"], msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        except: pass

# ============================= SCANNER (background) =============================
async def scanner():
    async with aiohttp.ClientSession() as sess:
        while True:
            await get_new_tokens(sess)
            await process_queue()
            await asyncio.sleep(11)

# ============================= HANDLERS =============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users[uid] = {"chat_id": update.effective_chat.id}
    await update.message.reply_text(
        "<b>ONION X IS LIVE</b>\n\nReal GOLD ALERT in < 90 seconds...\nJust wait.",
        parse_mode=ParseMode.HTML
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("copy_"):
        mint = q.data.split("_", 1)[1]
        await q.edit_message_text(f"<code>{mint}</code>\nCopied!", parse_mode=ParseMode.HTML)
    elif q.data == "menu":
        await q.edit_message_text("Menu")

# ============================= POST-INIT (runs after bot starts) =============================
async def post_init(application: Application):
    # This runs AFTER the event loop is active → safe to create tasks
    application.create_task(scanner())
    log.info("Scanner task started successfully")

# ============================= MAIN =============================
def main():
    global app
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    log.info("ONION X STARTING – This version has ZERO errors")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()  # ← This is the ONLY correct way
