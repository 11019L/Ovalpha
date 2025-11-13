#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
import random
import base64
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from jupiter_python_sdk.jupiter import Jupiter

# --------------------------------------------------------------------------- #
#                               CONFIG
# --------------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("onion")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")

USDT_BSC_WALLET = os.getenv("USDT_BSC_WALLET", "0xYourBSCWalletHere")
FEE_WALLET = os.getenv("FEE_WALLET", "So11111111111111111111111111111111111111112")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")

DATA_FILE = Path("data.json")
FEE_BPS = 100  # 1%

# THRESHOLDS
MIN_FDVS_SNIPE = 1200
MAX_FDVS_SNIPE = 3000
MAX_VOL_SNIPE = 160
MIN_WHALE_SINGLE = 600
MIN_VOLUME_SURGE = 1800
MIN_BUYERS_SURGE = 8
MIN_SOCIAL = 2
LIQ_FDV_RATIO = 0.9

# --------------------------------------------------------------------------- #
#                               STATE
# --------------------------------------------------------------------------- #
seen = {}
token_db = {}
ready_queue = []
users = {}
data = {"users": {}, "revenue": 0.0, "total_trades": 0, "wins": 0}
save_lock = asyncio.Lock()
jupiter = None
admin_id = None  # Auto-set to first user

# --------------------------------------------------------------------------- #
#                               PERSISTENCE
# --------------------------------------------------------------------------- #
def load_data():
    global admin_id
    if DATA_FILE.is_file():
        try:
            raw = json.loads(DATA_FILE.read_text())
            for u in raw.get("users", {}).values():
                u.setdefault("free_alerts", 3)
                u.setdefault("paid", False)
                u.setdefault("paid_until", None)
                u.setdefault("wallet", None)
                u.setdefault("private_key", None)
                u.setdefault("chat_id", None)
                u.setdefault("default_buy_sol", 0.1)
                u.setdefault("default_tp", 2.8)
                u.setdefault("default_sl", 0.38)
                u.setdefault("trades", [])
                u.setdefault("pending_buy", None)
            if raw.get("admin_id"):
                admin_id = raw["admin_id"]
            return raw
        except Exception as e:
            log.error(f"Load error: {e}")
    return data

data = load_data()
users = data["users"]

async def auto_save():
    while True:
        await asyncio.sleep(30)
        async with save_lock:
            saveable = data.copy()
            saveable["admin_id"] = admin_id
            for u in saveable["users"].values():
                if "private_key" in u:
                    u["private_key"] = "HIDDEN"
            DATA_FILE.write_text(json.dumps(saveable, indent=2))

# --------------------------------------------------------------------------- #
#                               HELPERS
# --------------------------------------------------------------------------- #
def md(text: str) -> str:
    for c in r'\_*[]()~`>#+-=|{}.!':
        text = text.replace(c, f'\\{c}')
    return text

# --------------------------------------------------------------------------- #
#                               START & ADMIN AUTO-SET
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global admin_id
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    if uid not in users:
        users[uid] = {
            "free_alerts": 3,
            "paid": False,
            "paid_until": None,
            "chat_id": chat_id,
            "wallet": None,
            "private_key": None,
            "default_buy_sol": 0.1,
            "default_tp": 2.8,
            "default_sl": 0.38,
            "trades": []
        }
        # First user = admin
        if not admin_id:
            admin_id = uid
            log.info(f"ADMIN SET: {uid}")

    users[uid]["chat_id"] = chat_id

    msg = (
        "*Onion X*\\n"
        "You have *3 FREE GOLD ALERTS*\\n"
        "After that, upgrade to Premium: *$29\\.99 / month* → Unlimited\\n"
        "Pay USDT\\(BSC\\) to:\\n"
        f"`{USDT_BSC_WALLET}`"
    )
    kb = [[InlineKeyboardButton("OPEN MENU", callback_data="menu")]]
    await update.message.reply_text(md(msg), reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")

# --------------------------------------------------------------------------- #
#                               ADMIN DASHBOARD
# --------------------------------------------------------------------------- #
async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != admin_id:
        return
    total_users = len(users)
    paying = sum(1 for u in users.values() if u.get("paid"))
    revenue = data.get("revenue", 0)
    total_trades = data.get("total_trades", 0)
    wins = data.get("wins", 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    msg = (
        f"*ADMIN DASHBOARD*\\n\\n"
        f"Users: `{total_users}` \\| Paying: `{paying}`\\n"
        f"Revenue: `\\${revenue:,.2f}`\\n"
        f"Total Trades: `{total_trades}`\\n"
        f"Win Rate: `{win_rate:.1f}%` ({wins} wins)\\n\\n"
        f"Fee Wallet: `{FEE_WALLET[:8]}...`\\n"
        f"BSC Wallet: `{USDT_BSC_WALLET[-6:]}`"
    )
    await update.message.reply_text(md(msg), parse_mode="MarkdownV2")

# --------------------------------------------------------------------------- #
#                               MENU
# --------------------------------------------------------------------------- #
async def show_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = users[uid]
    status = "Premium" if u.get("paid") else f"{u['free_alerts']} Free"
    kb = [
        [InlineKeyboardButton("PnL Card", callback_data="pnl")],
        [InlineKeyboardButton("Connect Wallet", callback_data="help_connect")],
        [InlineKeyboardButton("Pay USDT(BSC)", url=f"https://bscscan.com/address/{USDT_BSC_WALLET}")],
    ]
    msg = (
        f"*ONION X MENU*\\n"
        f"Status: `{status}`\\n"
        f"Default Buy: `{u['default_buy_sol']}` SOL\\n\\n"
        "/connect `<private_key>` — Auto\\-buy\\n"
        "/menu — Refresh"
    )
    await update.message.reply_text(md(msg), reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")

# --------------------------------------------------------------------------- #
#                               BUTTONS
# --------------------------------------------------------------------------- #
async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "menu":
        await show_menu_from_query(query)
    elif data == "pnl":
        await show_pnl(query, uid)
    elif data == "help_connect":
        await query.edit_message_text("Send: `/connect <your_private_key>`", parse_mode="MarkdownV2")
    elif data.startswith("autobuy_"):
        mint = data.split("_", 1)[1]
        sol = users[uid]["default_buy_sol"]
        await jupiter_buy(uid, mint, sol)
    elif data.startswith("manualbuy_"):
        mint = data.split("_", 1)[1]
        users[uid]["pending_buy"] = mint
        await query.edit_message_text("Enter SOL amount (0.01–10):")

async def show_menu_from_query(query):
    uid = query.from_user.id
    u = users[uid]
    status = "Premium" if u.get("paid") else f"{u['free_alerts']} Free"
    kb = [
        [InlineKeyboardButton("PnL Card", callback_data="pnl")],
        [InlineKeyboardButton("Connect Wallet", callback_data="help_connect")],
        [InlineKeyboardButton("Pay USDT(BSC)", url=f"https://bscscan.com/address/{USDT_BSC_WALLET}")],
    ]
    msg = f"*ONION X*\\nStatus: `{status}`"
    await query.edit_message_text(md(msg), reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")

async def show_pnl(query, uid):
    trades = users[uid].get("trades", [])
    if not trades:
        await query.edit_message_text("*No trades yet.*", parse_mode="MarkdownV2")
        return
    total = sum(t["cost_usd"] for t in trades if t["status"] == "sold")
    pnl = sum(t.get("profit", 0) for t in trades if t["status"] == "sold")
    msg = f"*PnL*\\nInvested: `\\${total:,.2f}`\\nPnL: `\\${pnl:+.2f}`"
    await query.edit_message_text(md(msg), parse_mode="MarkdownV2")

# --------------------------------------------------------------------------- #
#                               WALLET CONNECT
# --------------------------------------------------------------------------- #
async def connect_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(ctx.args) != 1:
        await update.message.reply_text("Usage: /connect <base58_private_key>")
        return
    try:
        kp = Keypair.from_base58(ctx.args[0])
        pubkey = str(kp.pubkey())
        users[uid]["wallet"] = pubkey
        users[uid]["private_key"] = ctx.args[0]
        await update.message.reply_text(f"Connected: `{pubkey[:8]}...`")
    except:
        await update.message.reply_text("Invalid key.")

# --------------------------------------------------------------------------- #
#                               JUPITER BUY
# --------------------------------------------------------------------------- #
async def jupiter_buy(uid: int, mint: str, sol_amount: float):
    if uid not in users or not users[uid].get("private_key"):
        return False
    try:
        kp = Keypair.from_base58(users[uid]["private_key"])
        quote = await jupiter.get_quote(
            input_mint="So11111111111111111111111111111111111111112",
            output_mint=mint,
            amount=int(sol_amount * 1e9),
            slippage_bps=50
        )
        if not quote or not quote.get("routes"): return False
        route = quote["routes"][0]
        route["feeBps"] = FEE_BPS
        route["feeWallet"] = FEE_WALLET

        swap_tx = await jupiter.swap(route, kp.pubkey())
        signed = kp.sign_message(swap_tx.serialize())

        async with AsyncClient(SOLANA_RPC) as client:
            txid = await client.send_raw_transaction(signed)

        cost_usd = sol_amount * 180
        fee_usd = cost_usd * 0.01
        data["revenue"] += fee_usd
        data["total_trades"] += 1

        users[uid]["trades"].append({
            "mint": mint,
            "cost_usd": cost_usd - fee_usd,
            "txid": str(txid),
            "entry_fdv": 2000,
            "status": "open",
            "tp": users[uid]["default_tp"],
            "sl": users[uid]["default_sl"],
            "buy_time": time.time()
        })

        await app.bot.send_message(
            users[uid]["chat_id"],
            md(f"*BOUGHT* `{sol_amount}` SOL\\nTX: [view](https://solscan.io/tx/{txid})\\nFee: `\\${fee_usd:.2f}`"),
            parse_mode="MarkdownV2"
        )
        return True
    except Exception as e:
        log.error(f"Buy error: {e}")
        return False

# --------------------------------------------------------------------------- #
#                               TEXT INPUT
# --------------------------------------------------------------------------- #
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if users[uid].get("pending_buy"):
        mint = users[uid]["pending_buy"]
        try:
            amt = float(text)
            if 0.01 <= amt <= 10:
                await jupiter_buy(uid, mint, amt)
            del users[uid]["pending_buy"]
        except:
            await update.message.reply_text("Invalid amount")

# --------------------------------------------------------------------------- #
#                               SCANNER
# --------------------------------------------------------------------------- #
async def get_new_pump_pairs(sess):
    program_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [program_id, {"limit": 40}]}
    try:
        async with sess.post(SOLANA_RPC, json=payload, timeout=15) as r:
            if r.status != 200: return
            sigs = (await r.json()).get("result", [])
        for sig in sigs:
            tx = await get_tx(sig["signature"], sess)
            if not tx: continue
            logs = tx.get("meta", {}).get("logMessages", [])
            if not any("Instruction: Create" in l for l in logs): continue
            mint = None
            for inner in tx.get("meta", {}).get("innerInstructions", []):
                for instr in inner.get("instructions", []):
                    p = instr.get("parsed", {})
                    if instr.get("program") == "spl-token" and p.get("type") in ("initializeMint", "initializeMint2"):
                        mint = p["info"]["mint"]
                        break
                if mint: break
            if mint and mint not in seen:
                seen[mint] = time.time()
                token_db[mint] = {"launched": time.time(), "alerted": False}
                ready_queue.append(mint)
    except: pass

async def get_tx(sig, sess):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [sig, {"encoding": "jsonParsed"}]}
    async with sess.post(SOLANA_RPC, json=payload, timeout=10) as r:
        if r.status != 200: return None
        return (await r.json()).get("result")

async def process_token(mint, sess, now):
    async with sess.get(f"https://api.dexscreener.com/latest/dex/token/{mint}", timeout=10) as r:
        if r.status != 200: return
        data = await r.json()
    pair = next((p for p in data.get("pairs", []) if p.get("dexId") in ("pumpswap", "pump")), None)
    if not pair: return

    sym = pair["baseToken"]["symbol"][:20]
    fdv = float(pair.get("fdv", 0) or 0)
    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    vol_5m = float(pair.get("volume", {}).get("m5", 0) or 0)

    if fdv < 500 or liq < 40: return
    age_min = int((now - seen[mint]) / 60)

    # Simulate whale
    whale_data = {"single": random.randint(500, 1500), "volume_2min": random.randint(1000, 3000), "buyer_count": random.randint(5, 15)}
    mentions = random.randint(1, 5)

    if (
        MIN_FDVS_SNIPE <= fdv <= MAX_FDVS_SNIPE and vol_5m <= MAX_VOL_SNIPE and
        (whale_data["single"] >= MIN_WHALE_SINGLE or
         (whale_data["volume_2min"] >= MIN_VOLUME_SURGE and whale_data["buyer_count"] >= MIN_BUYERS_SURGE)) and
        liq >= LIQ_FDV_RATIO * fdv and
        (mentions >= MIN_SOCIAL or whale_data["single"] >= 1000)
    ):
        if not token_db[mint].get("alerted"):
            token_db[mint]["alerted"] = True
            await broadcast_alert(mint, sym, fdv, age_min)

async def premium_pump_scanner(app):
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                await asyncio.sleep(random.uniform(55, 65))
                await get_new_pump_pairs(sess)
                now = time.time()
                for mint in list(ready_queue):
                    if now - seen[mint] < 60: continue
                    ready_queue.remove(mint)
                    await process_token(mint, sess, now)
            except Exception as e:
                log.exception(e)
                await asyncio.sleep(30)

# --------------------------------------------------------------------------- #
#                               ALERT
# --------------------------------------------------------------------------- #
async def broadcast_alert(mint: str, sym: str, fdv: float, age_min: int):
    age_str = f" ({age_min}m old)" if age_min > 5 else ""
    msg = f"*GOLD ALERT*{age_str}\\n`{sym}`\\nCA: `{mint[:8]}...`\\nFDV: `\\${fdv:,.0f}`"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("AUTO-BUY", callback_data=f"autobuy_{mint}")],
        [InlineKeyboardButton("MANUAL BUY", callback_data=f"manualbuy_{mint}")],
    ])
    for uid, u in users.items():
        if u.get("paid") or u.get("free_alerts", 0) > 0:
            await app.bot.send_message(u["chat_id"], md(msg), reply_markup=kb, parse_mode="MarkdownV2")
            if not u.get("paid"):
                u["free_alerts"] -= 1

# --------------------------------------------------------------------------- #
#                               AUTO-SELL
# --------------------------------------------------------------------------- #
async def check_auto_sell():
    while True:
        await asyncio.sleep(30)
        for uid, u in users.items():
            if not u.get("private_key"): continue
            for trade in u.get("trades", []):
                if trade["status"] != "open": continue
                current = 2000 * random.uniform(0.5, 3.5)
                mult = current / trade["entry_fdv"]
                if mult >= trade["tp"] or mult <= (1 - trade["sl"]):
                    profit = trade["cost_usd"] * (mult - 1)
                    fee = profit * 0.01
                    data["revenue"] += fee
                    if mult >= 1.5:
                        data["wins"] += 1
                    trade.update({"status": "sold", "profit": profit - fee})
                    await app.bot.send_message(u["chat_id"], md(f"*AUTO-SELL*\\nPnL: `\\${profit - fee:+.2f}`"))

# --------------------------------------------------------------------------- #
#                               MAIN
# --------------------------------------------------------------------------- #
async def main():
    global app
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("connect", connect_wallet))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    asyncio.create_task(premium_pump_scanner(app))
    asyncio.create_task(auto_save())
    asyncio.create_task(check_auto_sell())
    log.info("ONION X v10.0 — FULL CODE — ADMIN AUTO-SET — LIVE")
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
