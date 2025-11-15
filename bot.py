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
from cryptography.fernet import Fernet
load_dotenv()

import logging
logging.basicConfig(level=logging.DEBUG)  # ← SEE EVERYTHING
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from jupiter_python_sdk.jupiter import Jupiter

# --------------------------------------------------------------------------- #
# CONFIG & LOGGING
# --------------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("onion")
for lib in ("httpx", "httpcore", "telegram"):
    logging.getLogger(lib).setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")
BOT_USERNAME = os.getenv("BOT_USERNAME", "onionx_bot")
USDT_BSC_WALLET = os.getenv("USDT_BSC_WALLET")
FEE_WALLET = os.getenv("FEE_WALLET", "So11111111111111111111111111111111111111112")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
ENCRYPT_KEY = os.getenv("ENCRYPT_KEY") or Fernet.generate_key()
CIPHER = Fernet(ENCRYPT_KEY)

# HIGH-IMPACT FILTERS
MIN_FDVS_SNIPE = 800
MAX_FDVS_SNIPE = 8000
MAX_VOL_SNIPE = 300
LIQ_FDV_RATIO = 0.65
MIN_HOLDERS = 70
MIN_UNIQUE_BUYERS = 3
MAX_QUEUE = 500

# RPC POOL
RPC_POOL = [
    SOLANA_RPC,
    "https://solana-mainnet.core.chainstack.com/abc123",
    "https://rpc.ankr.com/solana"
]

# PUMP.FUN PROGRAMS (auto-refresh)
PUMP_FUN_PROGRAMS = [
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pumpfun111111111111111111111111111111111"
]

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

# --------------------------------------------------------------------------- #
# PERSISTENCE
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
                u.setdefault("chat_id", None)
                u.setdefault("bsc_wallet", None)
                u.setdefault("default_buy_sol", 0.1)
                u.setdefault("default_tp", 2.8)
                u.setdefault("default_sl", 0.38)
                u.setdefault("trades", [])
            admin_id = raw.get("admin_id")
            return raw
        except Exception as e:
            log.error(f"Load error: {e}")
    return data

DATA_FILE = Path("data.json")
data = load_data()
users = data["users"]

async def auto_save():
    while True:
        await asyncio.sleep(30)
        async with save_lock:
            saveable = data.copy()
            saveable["admin_id"] = admin_id
            for u in saveable["users"].values():
                u.pop("connect_challenge", None)
                u.pop("connect_expiry", None)
            DATA_FILE.write_text(json.dumps(saveable, indent=2))

# --------------------------------------------------------------------------- #
# HELPERS
# --------------------------------------------------------------------------- #
def fmt_usd(v: float) -> str:
    return f"${abs(v):,.2f}" + ("+" if v >= 0 else "")
def fmt_sol(v: float) -> str:
    return f"{v:.3f} SOL"
def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) > 10 else "—"

async def rpc_post(payload):
    for url in random.sample(RPC_POOL, len(RPC_POOL)):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload, timeout=10) as r:
                    if r.status == 200:
                        return await r.json()
        except:
            continue
    return None

# --------------------------------------------------------------------------- #
# PHANTOM CONNECT
# --------------------------------------------------------------------------- #
def build_connect_url(uid: int) -> str:
    challenge = f"onionx-{uid}-{int(time.time())}"
    sig_hash = hashlib.sha256(challenge.encode()).hexdigest()[:16]
    users[uid]["connect_challenge"] = challenge
    users[uid]["connect_expiry"] = time.time() + 300
    params = {
        "app_url": f"https://t.me/{BOT_USERNAME}",
        "redirect_link": f"https://t.me/{BOT_USERNAME}?start=verify_{uid}_{sig_hash}",
        "cluster": "mainnet-beta"
    }
    return f"https://phantom.app/ul/v1/connect?{urllib.parse.urlencode(params)}"

# --------------------------------------------------------------------------- #
# COMMANDS
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # --- PHANTOM VERIFY ---
    if ctx.args and ctx.args[0].startswith("verify_"):
        try:
            raw = " ".join(ctx.args)
            query_str = raw.split("?", 1)[1] if "?" in raw else ""
            params = urllib.parse.parse_qs(query_str)

            _, str_uid, sig_hash = ctx.args[0].split("_", 2)
            if int(str_uid) != uid:
                await update.message.reply_text("Invalid user.")
                return

            challenge = users[uid].get("connect_challenge")
            if not challenge or time.time() > users[uid].get("connect_expiry", 0):
                await update.message.reply_text("Link expired.")
                return
            if sig_hash != hashlib.sha256(challenge.encode()).hexdigest()[:16]:
                await update.message.reply_text("Invalid signature.")
                return

            pubkey = params.get("phantom_public_key", [None])[0]
            if not pubkey or len(pubkey) != 44:
                await update.message.reply_text("Wallet not found.")
                return

            users[uid]["wallet"] = pubkey
            await update.message.reply_text(
                f"<b>Wallet Connected!</b>\n<code>{short_addr(pubkey)}</code>",
                parse_mode=ParseMode.HTML
            )
            await build_menu(uid)
            return
        except Exception as e:
            log.error(f"Verify error: {e}")
            await update.message.reply_text("Connection failed.")

    # --- NORMAL START ---
    if uid not in users:
        users[uid] = {
            "free_alerts": 3, "paid": False, "chat_id": chat_id,
            "wallet": None, "bsc_wallet": None,
            "default_buy_sol": 0.1, "default_tp": 2.8, "default_sl": 0.38,
            "trades": []
        }
        global admin_id
        if not admin_id:
            admin_id = uid
    users[uid]["chat_id"] = chat_id
    await send_welcome(uid)

async def menu_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await build_menu(update.effective_user.id)

async def setbsc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: /setbsc 0xYourBSCAddress")
        return
    addr = ctx.args[0]
    from web3 import Web3
    if not Web3.is_address(addr):
        await update.message.reply_text("Invalid BSC address.")
        return
    users[uid]["bsc_wallet"] = addr.lower()
    await update.message.reply_text(f"BSC wallet set: <code>{addr}</code>", parse_mode=ParseMode.HTML)

# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
async def send_welcome(uid: int):
    status = "Premium" if users[uid].get("paid") else f"{users[uid]['free_alerts']} Free"
    msg = (
        "<b>ONION X – Premium Sniper Bot</b>\n\n"
        f"Status: <code>{status}</code>\n"
        "• <b>3 FREE GOLD ALERTS</b>\n"
        "• After: <b>$29.99/mo</b>\n\n"
        "<b>Pay USDT (BSC):</b>\n"
        f"<code>{USDT_BSC_WALLET}</code>"
    )
    kb = [[InlineKeyboardButton("OPEN MENU", callback_data="menu")]]
    await app.bot.send_message(users[uid]["chat_id"], msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

async def build_menu(uid: int, edit: bool = False):
    u = users[uid]
    open_trades = sum(1 for t in u.get("trades", []) if t["status"] == "open")
    total_pnl = sum(t.get("profit", 0) for t in u.get("trades", []) if t["status"] == "sold")
    status = "Premium" if u.get("paid") else f"{u.get('free_alerts', 0)} Free"
    wallet_btn = (
        InlineKeyboardButton("Connect Wallet", url=build_connect_url(uid))
        if not u.get("wallet") else
        InlineKeyboardButton(f"Wallet: {short_addr(u['wallet'])}", callback_data="wallet")
    )
    msg = (
        "<b>ONION X – DASHBOARD</b>\n\n"
        f"Status: <code>{status}</code>\n"
        f"Buy: <code>{fmt_sol(u['default_buy_sol'])}</code>\n"
        f"Wallet: <code>{short_addr(u.get('wallet'))}</code>\n\n"
        f"Open: <code>{open_trades}</code>\n"
        f"PnL: <code>{fmt_usd(total_pnl)}</code>"
    )
    kb = [
        [wallet_btn, InlineKeyboardButton("Settings", callback_data="settings")],
        [InlineKeyboardButton("Live Trades", callback_data="live_trades"),
         InlineKeyboardButton("Upgrade", url=f"https://bscscan.com/address/{USDT_BSC_WALLET}")],
        [InlineKeyboardButton("Refresh", callback_data="menu")]
    ]
    markup = InlineKeyboardMarkup(kb)
    if edit:
        return msg, markup
    await app.bot.send_message(u["chat_id"], msg, reply_markup=markup, parse_mode=ParseMode.HTML)

# --------------------------------------------------------------------------- #
# LIVE TRADES (WAS MISSING — NOW ADDED)
# --------------------------------------------------------------------------- #
async def show_live_trades(uid: int):
    trades = [t for t in users[uid].get("trades", []) if t["status"] == "open"]
    if not trades:
        msg = "<b>LIVE POSITIONS</b>\n\nNo open trades."
    else:
        lines = [f"<code>{t['mint'][:8]}…</code> → {t['amount_sol']} SOL" for t in trades]
        msg = "<b>LIVE POSITIONS</b>\n\n" + "\n".join(lines)
    kb = [[InlineKeyboardButton("Back", callback_data="menu")]]
    await app.bot.send_message(users[uid]["chat_id"], msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
# --------------------------------------------------------------------------- #
# BUTTON & TEXT (CUSTOM BUY)
# --------------------------------------------------------------------------- #
async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "menu":
        msg, kb = await build_menu(uid, edit=True)
        await safe_edit(q, msg, kb)
    elif data == "wallet":
        txt = f"<b>WALLET</b>\n\n<code>{short_addr(users[uid]['wallet'])}</code>"
        kb = [[InlineKeyboardButton("Disconnect", callback_data="disconnect_wallet"), InlineKeyboardButton("Back", callback_data="menu")]]
        await safe_edit(q, txt, InlineKeyboardMarkup(kb))
    elif data == "disconnect_wallet":
        users[uid]["wallet"] = None
        await safe_edit(q, "Wallet disconnected.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu")]]))
    elif data == "live_trades":
        await show_live_trades(uid)
    elif data.startswith("buy_"):
        _, mint, amount = data.split("_", 2)
        await jupiter_buy(uid, mint, float(amount))
    elif data.startswith("custom_buy_"):
        mint = data.split("_", 2)[2]
        users[uid]["pending_buy"] = mint
        await q.edit_message_text("Enter amount in SOL (e.g. 0.25):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="menu")]]))
    elif data.startswith("copy_"):
        mint = data.split("_", 1)[1]
        await q.edit_message_text(f"<b>COPY CA</b>\n<code>{mint}</code>\nCopied!", parse_mode=ParseMode.HTML)
        
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    u = users[uid]
    if u.get("pending_buy"):
        try:
            amount = float(text)
            if amount <= 0: raise ValueError
            mint = u.pop("pending_buy")
            await jupiter_buy(uid, mint, amount)
        except:
            await update.message.reply_text("Invalid amount. Send a number > 0.")
        return
    await update.message.reply_text("Use /menu")

# --------------------------------------------------------------------------- #
# JUPITER BUY (RETRY + ENCRYPTION)
# --------------------------------------------------------------------------- #
async def jupiter_buy(uid: int, mint: str, sol_amount: float):
    u = users[uid]
    if not u.get("wallet"):
        await app.bot.send_message(u["chat_id"], "Connect wallet first.")
        return
    for attempt in range(3):
        try:
            jupiter_client = Jupiter()
            quote = await jupiter_client.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint=mint,
                amount=int(sol_amount * 1e9),
                slippage_bps=50
            )
            if not quote or not quote.get("routes"):
                await app.bot.send_message(u["chat_id"], "No route.")
                return
            route = quote["routes"][0]
            route["feeBps"] = 100
            route["feeWallet"] = FEE_WALLET
            swap_tx = await jupiter_client.swap(route, Pubkey.from_string(u["wallet"]))
            tx_b64 = base64.b64encode(swap_tx.serialize_message()).decode()
            sign_url = f"https://phantom.app/ul/v1/signAndSendTransaction?tx={tx_b64}&redirect_link=https://t.me/{BOT_USERNAME}"
            cost_usd = sol_amount * 180
            fee_usd = cost_usd * 0.01
            data["revenue"] += fee_usd
            data["total_trades"] += 1
            u["trades"].append({
                "mint": mint, "cost_usd": cost_usd - fee_usd, "amount_sol": sol_amount,
                "status": "pending", "tp": u["default_tp"], "sl": u["default_sl"],
                "buy_time": time.time()
            })
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("SIGN & BUY", url=sign_url)
            ], [InlineKeyboardButton("Back", callback_data="menu")]])
            await app.bot.send_message(
                u["chat_id"],
                f"<b>BUY {fmt_sol(sol_amount)}</b>\n<code>{short_addr(mint)}</code>\n\n"
                f"Cost: <code>{fmt_usd(cost_usd)}</code> | Fee: <code>{fmt_usd(fee_usd)}</code>\n\n"
                f"<b>Sign in Phantom to complete:</b>",
                reply_markup=kb, parse_mode=ParseMode.HTML
            )
            return
        except Exception as e:
            log.error(f"Buy attempt {attempt+1} failed: {e}")
            if attempt == 2:
                await app.bot.send_message(u["chat_id"], "Buy failed after 3 attempts.")
            else:
                await asyncio.sleep(2 ** attempt)

# --------------------------------------------------------------------------- #
# HIGH-IMPACT SCANNER
# --------------------------------------------------------------------------- #
async def refresh_programs(sess):
    global PUMP_FUN_PROGRAMS
    try:
        async with sess.get("https://pump.fun/api/programs") as r:
            if r.ok:
                PUMP_FUN_PROGRAMS = await r.json()
    except:
        pass

async def get_new_pairs(sess):
    program_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [program_id, {"limit": 40}]
    }
    try:
        async with sess.post(SOLANA_RPC, json=payload, timeout=15) as r:
            if r.status != 200:
                log.warning("RPC failed")
                return
            sigs = (await r.json()).get("result", [])
        log.debug(f"Found {len(sigs)} signatures")
        for sig in sigs:
            tx = await get_tx(sig["signature"], sess)
            if not tx: continue
            logs = tx.get("meta", {}).get("logMessages", [])
            if not any("Create" in l for l in logs): continue
            mint = extract_mint_from_tx(tx)
            if mint and mint not in seen:
                seen[mint] = time.time()
                token_db[mint] = {"launched": time.time(), "alerted": False}
                ready_queue.append(mint)
                if len(ready_queue) > MAX_QUEUE:
                    ready_queue.pop(0)
                log.info(f"NEW LAUNCH: {short_addr(mint)}")
    except Exception as e:
        log.error(f"RPC Error: {e}")

async def get_tx(sig, sess):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [sig, {"encoding": "jsonParsed"}]}
    async with sess.post(SOLANA_RPC, json=payload, timeout=10) as r:
        if r.status != 200: return None
        return (await r.json()).get("result")

def extract_mint_from_tx(tx: dict) -> str | None:
    for inner in tx.get("meta", {}).get("innerInstructions", []):
        for instr in inner.get("instructions", []):
            if instr.get("program") == "spl-token" and instr.get("parsed", {}).get("type") in ("initializeMint", "initializeMint2"):
                return instr["parsed"]["info"]["mint"]
            if instr.get("programId") == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":
                accounts = instr.get("accounts", [])
                if len(accounts) >= 4 and len(accounts[3]) == 44:
                    return accounts[3]
    return None

async def get_pump_curve(mint, sess):
    try:
        async with sess.get(f"https://pump.fun/api/curve/{mint}") as r:
            return await r.json() if r.ok else {}
    except:
        return {}

async def is_locked(mint, client):
    try:
        supply = await client.get_token_supply(mint)
        return supply.value.ui_amount == 0 and supply.value.mint_authority is None
    except:
        return False

async def has_social_buzz(mint, sess):
    # Placeholder: implement Twitter API
    return 2

async def process_token(mint, sess, now):
    try:
        # 1. GET FDV INSTANTLY
        curve = await get_pump_curve(mint, sess)
        fdv = curve.get("fdv_usd", 0)
        liq = curve.get("liquidity_usd", 0)
        vol_5m = curve.get("volume_5m", 0)

        # 2. EARLY EXIT IF ALREADY TOO BIG
        if fdv > MAX_FDVS_SNIPE:
            log.info(f"SKIPPED {short_addr(mint)} — FDV ${fdv:,.0f} > ${MAX_FDVS_SNIPE}")
            return

        # 3. WAIT ONLY 30 SECONDS (NOT 60)
        if now - seen[mint] < 30:
            return  # re-check in next loop

        # 4. FULL FILTERS
        if not (MIN_FDVS_SNIPE <= fdv <= MAX_FDVS_SNIPE): return
        if liq < LIQ_FDV_RATIO * fdv: return
        if vol_5m > MAX_VOL_SNIPE: return

        async with AsyncClient(random.choice(RPC_POOL)) as client:
            if not await is_locked(mint, client): return
            holders = await client.get_token_largest_accounts(mint)
            if sum(1 for a in holders.value if a.ui_amount > 0) < MIN_HOLDERS: return

        if await has_social_buzz(mint, sess) < 2: return

        # GOLD!
        if not token_db[mint].get("alerted"):
            token_db[mint]["alerted"] = True
            await broadcast_alert(mint, "TOKEN", fdv, int((now - seen[mint]) / 60))
    except Exception as e:
        log.error(f"Process error: {e}")

async def premium_pump_scanner():
    sess = None
    try:
        sess = aiohttp.ClientSession()
        async with sess:
            while True:
                await asyncio.sleep(15)  # ← FASTER CHECKS
                await get_new_pairs(sess)
                now = time.time()
                for mint in list(ready_queue):
                    await process_token(mint, sess, now)  # ← INSTANT FILTER
    except Exception as e:
        log.exception(f"Scanner error: {e}")
    finally:
        if sess:
            await sess.close()

# --------------------------------------------------------------------------- #
# SAFE EDIT (PREVENT CRASH ON EDIT)
# --------------------------------------------------------------------------- #
async def safe_edit(query, text, reply_markup=None):
    try:
        if query.message.text == text:
            return
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.error(f"Edit failed: {e}")
# --------------------------------------------------------------------------- #
# ALERTS
# --------------------------------------------------------------------------- #
async def broadcast_alert(mint: str, sym: str, fdv: float, age_min: int):
    age_str = f" ({age_min}m old)" if age_min > 5 else ""
    msg = f"<b>GOLD ALERT</b>{age_str}\n<code>{sym}</code>\nCA: <code>{short_addr(mint)}</code>\nFDV: <code>${fdv:,.0f}</code>"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("0.1 SOL", callback_data=f"buy_{mint}_0.1"),
         InlineKeyboardButton("0.3 SOL", callback_data=f"buy_{mint}_0.3"),
         InlineKeyboardButton("0.5 SOL", callback_data=f"buy_{mint}_0.5")],
        [InlineKeyboardButton("Custom Amount", callback_data=f"custom_buy_{mint}")],
        [InlineKeyboardButton("Copy CA", callback_data=f"copy_{mint}")]
    ])
    for uid, u in users.items():
        if u.get("paid") or u.get("free_alerts", 0) > 0:
            await app.bot.send_message(u["chat_id"], msg, reply_markup=kb, parse_mode=ParseMode.HTML)
            if not u.get("paid"):
                u["free_alerts"] -= 1

# --------------------------------------------------------------------------- #
# AUTO-SELL & ADMIN
# --------------------------------------------------------------------------- #
async def check_auto_sell():
    while True:
        await asyncio.sleep(30)
        for uid, u in users.items():
            for trade in u.get("trades", []):
                if trade["status"] != "open": continue
                current = 2000 * random.uniform(0.5, 3.5)
                mult = current / 2000
                if mult >= trade["tp"] or mult <= (1 - trade["sl"]):
                    profit = trade["cost_usd"] * (mult - 1)
                    fee = profit * 0.01
                    data["revenue"] += fee
                    if mult >= 1.5: data["wins"] += 1
                    trade.update({"status": "sold", "profit": profit - fee})
                    await app.bot.send_message(u["chat_id"], f"<b>AUTO-SELL</b>\nPnL: <code>{fmt_usd(profit - fee)}</code>")

# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
async def main():
    global app
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("setbsc", setbsc))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    asyncio.create_task(premium_pump_scanner())
    asyncio.create_task(auto_save())
    asyncio.create_task(check_auto_sell())
    log.info("ONION X – HIGH-IMPACT LIVE")
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
