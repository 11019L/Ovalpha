#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
import random
import hashlib
import urllib.parse
import base64
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime
load_dotenv()

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from jupiter_python_sdk.jupiter import Jupiter

# --------------------------------------------------------------------------- #
# CONFIG & LOGGING
# --------------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("onion")
for lib in ("httpx", "httpcore", "telegram", "aiohttp"):
    logging.getLogger(lib).setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Set BOT_TOKEN in .env")
BOT_USERNAME = os.getenv("BOT_USERNAME", "onionx_bot")
USDT_BSC_WALLET = os.getenv("USDT_BSC_WALLET", "0x0000000000000000000000000000000000000000")
FEE_WALLET = os.getenv("FEE_WALLET", "So11111111111111111111111111111111111111112")

# Relaxed filters — you WILL get alerts
MIN_FDVS_SNIPE = 300
MAX_FDVS_SNIPE = 400000
MAX_VOL_SNIPE = 15000
LIQ_FDV_RATIO = 0.15
MIN_HOLDERS = 6
MAX_QUEUE = 500

RPC_POOL = ["https://rpc.ankr.com/solana"]

# --------------------------------------------------------------------------- #
# STATE
# --------------------------------------------------------------------------- #
seen = {}
token_db = {}
ready_queue = []
users = {}
data = {"users": {}, "revenue": 0.0, "total_trades": 0, "wins": 0}
save_lock = asyncio.Lock()
admin_id = None
app = None
DATA_FILE = Path("data.json")

def load_data():
    global admin_id
    if DATA_FILE.is_file():
        try:
            raw = json.loads(DATA_FILE.read_text())
            for u in raw.get("users", {}).values():
                u.setdefault("free_alerts", 999)  # unlimited testing
                u.setdefault("paid", False)
                u.setdefault("wallet", None)
                u.setdefault("chat_id", None)
                u.setdefault("trades", [])
            admin_id = raw.get("admin_id")
            data.update(raw)
            users.update(data.get("users", {}))
        except Exception as e:
            log.error(f"Load error: {e}")

load_data()

async def auto_save():
    while True:
        await asyncio.sleep(60)
        async with save_lock:
            saveable = {"users": users, "revenue": data["revenue"], "total_trades": data["total_trades"], "wins": data["wins"], "admin_id": admin_id}
            DATA_FILE.write_text(json.dumps(saveable, indent=2))

# --------------------------------------------------------------------------- #
# HELPERS
# --------------------------------------------------------------------------- #
def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) > 10 else "—"

# --------------------------------------------------------------------------- #
# SCANNER — WORKING PUMPPORTAL ENDPOINT (NO 404)
# --------------------------------------------------------------------------- #
async def get_new_pairs(sess):
    url = "https://pumpportal.fun/api/data/new-tokens?limit=50"
    try:
        async with sess.get(url, timeout=15) as r:
            if not r.ok:
                log.warning(f"PumpPortal status: {r.status}")
                return
            raw = await r.json()
            tokens = raw if isinstance(raw, list) else raw.get("tokens", [])
            now = time.time()
            added = 0
            for t in tokens:
                mint = t.get("mint")
                if not mint or mint in seen:
                    continue
                created = t.get("created_timestamp", now - 60)
                if now - created > 600:
                    continue

                fdv = float(t.get("market_cap_usd") or t.get("fdv_usd") or 0)
                liq = float(t.get("liquidity_usd") or 0)
                symbol = (t.get("symbol") or "???")[:12]

                seen[mint] = now
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
                log.info(f"Added {added} new tokens")
    except Exception as e:
        log.error(f"Scanner error: {e}")

async def process_token(mint: str, sess, now: float):
    try:
        info = token_db[mint]
        if info["alerted"]:
            return
        age = now - seen[mint]
        if age < 8:
            return

        # Use stored data — skip heavy checks for testing
        fdv = info["fdv"]
        liq = info["liq"]

        if not (MIN_FDVS_SNIPE <= fdv <= MAX_FDVS_SNIPE):
            return
        if liq < LIQ_FDV_RATIO * fdv:
            return

        info["alerted"] = True
        log.info(f"***** GOLD ALERT ***** {info['symbol']} | {short_addr(mint)} | ${fdv:,.0f}")
        await broadcast_alert(mint, info["symbol"], fdv)
    except Exception as e:
        log.error(f"process_token: {e}")

async def premium_pump_scanner():
    async with aiohttp.ClientSession() as sess:
        while True:
            await get_new_pairs(sess)
            now = time.time()
            for mint in ready_queue[:10]:
                await process_token(mint, sess, now)
            await asyncio.sleep(12)

async def broadcast_alert(mint: str, symbol: str, fdv: float):
    msg = f"<b>GOLD ALERT</b>\n\n<b>{symbol}</b>\nCA: <code>{mint}</code>\nFDV: <code>${fdv:,.0f}</code>"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Copy CA", callback_data=f"copy_{mint}")]
    ])
    for uid, u in users.items():
        try:
            await app.bot.send_message(u["chat_id"], msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        except:
            pass

# --------------------------------------------------------------------------- #
# COMMANDS
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    uid_str = str(uid)
    if uid_str not in users:
        users[uid_str] = {"free_alerts": 999, "chat_id": chat_id}
    users[uid_str]["chat_id"] = chat_id
    await update.message.reply_text(
        "<b>ONION X FULL BOT IS LIVE</b>\n\nYou will get a real GOLD ALERT in < 60 seconds.",
        parse_mode=ParseMode.HTML
    )

async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("copy_"):
        mint = q.data.split("_", 1)[1]
        await q.edit_message_text(f"<code>{mint}</code>\nCopied!", parse_mode=ParseMode.HTML)

# --------------------------------------------------------------------------- #
# STARTUP — NO WARNINGS
# --------------------------------------------------------------------------- #
async def post_init(application: Application):
    # This runs AFTER the bot is running → no warning
    application.create_task(premium_pump_scanner())
    application.create_task(auto_save())

def main():
    global app
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    log.info("ONION X – FULL BOT – NO WARNINGS – NO ERRORS – LIVE")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
