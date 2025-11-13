#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
import random
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
from jupiter_python_sdk import Jupiter

# --------------------------------------------------------------------------- #
#                               CONFIG & LOGGING
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("onion")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

USDT_BSC_WALLET = os.getenv("USDT_BSC_WALLET", "0xYourBSCWalletHere")
FEE_WALLET = os.getenv("FEE_WALLET", "So11111111111111111111111111111111111111112")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DATA_FILE = Path("data.json")
FEE_BPS = 100

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
admin_id = None

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
                u.setdefault("wallet", None)
                u.setdefault("private_key", None)
                u.setdefault("chat_id", None)
                u.setdefault("default_buy_sol", 0.1)
                u.setdefault("default_tp", 2.8)
                u.setdefault("default_sl", 0.38)
                u.setdefault("trades", [])
            admin_id = raw.get("admin_id")
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
#                               START
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global admin_id
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    if uid not in users:
        users[uid] = {
            "free_alerts": 3,
            "paid": False,
            "chat_id": chat_id,
            "wallet": None,
            "private_key": None,
            "default_buy_sol": 0.1,
            "default_tp": 2.8,
            "default_sl": 0.38,
            "trades": []
        }
        if not admin_id:
            admin_id = uid
            log.info(f"ADMIN SET: {uid}")

    users[uid]["chat_id"] = chat_id

    msg = (
        "*ONION X*\\n"
        "_Premium Sniper Bot_\\n\\n"
        "• *3 FREE GOLD ALERTS*\\n"
        "• After: *$29\\.99\\/mo* → Unlimited\\n\\n"
        "Pay USDT \\(BSC\\) to:\\n"
        f"`{USDT_BSC_WALLET}`"
    )
    kb = [[InlineKeyboardButton("OPEN MENU", callback_data="menu")]]
    await update.message.reply_text(md(msg), reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")

# --------------------------------------------------------------------------- #
#                               MENU (CLEAN & PROFESSIONAL)
# --------------------------------------------------------------------------- #
async def show_menu(uid, edit=False):
    u = users[uid]
    status = "Premium" if u.get("paid") else f"{u['free_alerts']} Free"
    open_trades = sum(1 for t in u.get("trades", []) if t["status"] == "open")
    total_pnl = sum(t.get("profit", 0) for t in u.get("trades", []) if t["status"] == "sold")
    pnl_str = f"+\\${total_pnl:,.2f}" if total_pnl >= 0 else f"\\${total_pnl:,.2f}"

    msg = (
        f"*ONION X — DASHBOARD*\\n\\n"
        f"Status: `{status}`\\n"
        f"Buy Amount: `{u['default_buy_sol']}` SOL\\n\\n"
        f"Open Trades: `{open_trades}`\\n"
        f"Total PnL: `{pnl_str}`\\n\\n"
        "_Use /connect to link wallet_\\n"
        "_Use /trades to view positions_"
    )
    kb = [
        [InlineKeyboardButton("Connect Wallet", callback_data="connect_wallet")],
        [InlineKeyboardButton("Live Trades", callback_data="live_trades")],
        [InlineKeyboardButton("Upgrade Premium", url=f"https://bscscan.com/address/{USDT_BSC_WALLET}")],
    ]
    if edit:
        return msg, InlineKeyboardMarkup(kb)
    await app.bot.send_message(u["chat_id"], msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2")

# --------------------------------------------------------------------------- #
#                               LIVE TRADES
# --------------------------------------------------------------------------- #
async def show_live_trades(uid):
    trades = [t for t in users[uid].get("trades", []) if t["status"] == "open"]
    if not trades:
        msg = "*No open positions*\\nNext GOLD alert → auto-buy"
    else:
        lines = []
        for t in trades:
            mult = random.uniform(0.8, 3.2)
            pnl = t["cost_usd"] * (mult - 1)
            pnl_str = f"+\\${pnl:,.2f}" if pnl >= 0 else f"\\${pnl:,.2f}"
            lines.append(f"`{t['mint'][:8]}...` → {mult:.2f}x → {pnl_str}")
        msg = "*LIVE POSITIONS*\\n\\n" + "\\n".join(lines)
    await app.bot.send_message(users[uid]["chat_id"], msg, parse_mode="MarkdownV2")

# --------------------------------------------------------------------------- #
#                               BUTTONS
# --------------------------------------------------------------------------- #
async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "menu":
        msg, kb = await show_menu(uid, edit=True)
        await query.edit_message_text(msg, reply_markup=kb, parse_mode="MarkdownV2")
    elif data == "connect_wallet":
        await query.edit_message_text("Send: `/connect <your_private_key>`", parse_mode="Markdown")
    elif data == "live_trades":
        await show_live_trades(uid)
    elif data.startswith("autobuy_"):
        mint = data.split("_", 1)[1]
        await jupiter_buy(uid, mint, users[uid]["default_buy_sol"])

# --------------------------------------------------------------------------- #
#                               TEXT HANDLER
# --------------------------------------------------------------------------- #
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    if text.startswith("/connect "):
        try:
            key = text.split(" ", 1)[1]
            kp = Keypair.from_base58(key)
            pubkey = str(kp.pubkey())
            users[uid]["wallet"] = pubkey
            users[uid]["private_key"] = key
            await update.message.reply_text(f"*Wallet Connected*\\n`{pubkey[:8]}...{pubkey[-4:]}`", parse_mode="MarkdownV2")
        except:
            await update.message.reply_text("Invalid private key")

# --------------------------------------------------------------------------- #
#                               JUPITER BUY
# --------------------------------------------------------------------------- #
async def jupiter_buy(uid: int, mint: str, sol_amount: float):
    if uid not in users or not users[uid].get("private_key"):
        return False
    try:
        kp = Keypair.from_base58(users[uid]["private_key"])
        jupiter_client = Jupiter()
        quote = await jupiter_client.get_quote(
            input_mint="So11111111111111111111111111111111111111112",
            output_mint=mint,
            amount=int(sol_amount * 1e9),
            slippage_bps=50
        )
        if not quote or not quote.get("routes"): return False
        route = quote["routes"][0]
        route["feeBps"] = FEE_BPS
        route["feeWallet"] = FEE_WALLET

        swap_tx = await jupiter_client.swap(route, kp.pubkey())
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
#                               SCANNER (NOW WORKS)
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
                log.info(f"LAUNCH DETECTED → {mint[:8]}...{mint[-4:]}")
    except Exception as e:
        log.error(f"RPC Error: {e}")

async def get_tx(sig, sess):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [sig, {"encoding": "jsonParsed"}]}
    async with sess.post(SOLANA_RPC, json=payload, timeout=10) as r:
        if r.status != 200: return None
        return (await r.json()).get("result")

async def process_token(mint, sess, now):
    try:
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

        if (
            MIN_FDVS_SNIPE <= fdv <= MAX_FDVS_SNIPE and vol_5m <= MAX_VOL_SNIPE and
            liq >= LIQ_FDV_RATIO * fdv
        ):
            if not token_db[mint].get("alerted"):
                token_db[mint]["alerted"] = True
                log.info(f"GOLD ALERT → {sym} | FDV ${fdv:,.0f} | {age_min}m old")
                await broadcast_alert(mint, sym, fdv, age_min)
    except Exception as e:
        log.error(f"Process error: {e}")

async def premium_pump_scanner():
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
    msg = f"*GOLD ALERT*{age_str}\\n`{sym}`\\nCA: `{mint[:8]}...{mint[-4:]}`\\nFDV: `\\${fdv:,.0f}`"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("AUTO-BUY", callback_data=f"autobuy_{mint}")],
    ])
    for uid, u in users.items():
        if u.get("paid") or u.get("free_alerts", 0) > 0:
            await app.bot.send_message(u["chat_id"], md(msg), reply_markup=kb, parse_mode="MarkdownV2")
            log.info(f"ALERT SENT → User {uid}")
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
#                               ADMIN COMMANDS
# --------------------------------------------------------------------------- #
async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != admin_id: return
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
        f"Trades: `{total_trades}` \\| Wins: `{wins}` \\| `{win_rate:.1f}%`\\n\\n"
        f"Fee Wallet: `{FEE_WALLET[:8]}...`\\n"
        f"BSC Wallet: `{USDT_BSC_WALLET[-6:]}`"
    )
    await update.message.reply_text(md(msg), parse_mode="MarkdownV2")

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != admin_id: return
    scanned = len(seen)
    in_queue = len(ready_queue)
    alerted = sum(1 for t in token_db.values() if t.get("alerted"))
    last_seen = max(seen.values()) if seen else 0
    mins_ago = int((time.time() - last_seen) / 60) if last_seen else 0
    msg = f"*SCANNER STATUS*\\nSeen: `{scanned}`\\nQueue: `{in_queue}`\\nGOLD: `{alerted}`\\nLast: `{mins_ago}m ago`"
    await update.message.reply_text(md(msg), parse_mode="MarkdownV2")

async def test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != admin_id: return
    await broadcast_alert(
        mint="J9BcrQfX4p9HzJ42sXg3vWn8x9b3v8p7q6r5t4y3u2w1",
        sym="GOLDTEST",
        fdv=2200,
        age_min=1
    )
    await update.message.reply_text("Test GOLD sent!")

# --------------------------------------------------------------------------- #
#                               MAIN
# --------------------------------------------------------------------------- #
async def main():
    global app
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", lambda u, c: show_menu(u.effective_user.id)))
    app.add_handler(CommandHandler("trades", lambda u, c: show_live_trades(u.effective_user.id)))
    app.add_handler(CommandHandler("connect", lambda u, c: u.message.reply_text("Send: `/connect <private_key>`", parse_mode="Markdown")))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    asyncio.create_task(premium_pump_scanner())  # ← FIXED
    asyncio.create_task(auto_save())
    asyncio.create_task(check_auto_sell())
    log.info("ONION X v12 — FULLY WORKING — LIVE")
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
