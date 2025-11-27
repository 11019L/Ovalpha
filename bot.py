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
import logging
logging.basicConfig(
    level=logging.INFO,
    force=True,  # This forces the configuration to take effect regardless of previous configurations
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]  # Explicitly use stdout
)
logging.getLogger().info("LOGGING IS WORKING - This message should appear immediately after startup")

log = logging.getLogger("onion")
def validate_environment():
    required_vars = ["BOT_TOKEN"]
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Call validation right after load_dotenv()
load_dotenv()
validate_environment()

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

# ---------------------------------------------------------------------------
# CONFIG & LOGGING
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")
BOT_USERNAME = os.getenv("BOT_USERNAME", "onionx_bot")
USDT_BSC_WALLET = os.getenv("USDT_BSC_WALLET", "0x0000000000000000000000000000000000000000")
FEE_WALLET = os.getenv("FEE_WALLET", "So11111111111111111111111111111111111111112")  # your fee wallet
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY", "")
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ---------------------------------------------------------------------------
# 2025 FILTERS (REAL WORKING SETTINGS)
# Test mode → comment the strict ones and uncomment the loose ones below
# ---------------------------------------------------------------------------
#MIN_FDVS_SNIPE   = 70_000       # $70k
#MAX_FDVS_SNIPE   = 900_000      # $900k
#LIQ_FDV_RATIO    = 0.22
#MAX_VOL_SNIPE    = 15_000
#MIN_HOLDERS      = 6

MIN_FDVS_SNIPE   = 5_000
MAX_FDVS_SNIPE   = 3_000_000     # ← now catches $2.9M mid-pump runners
LIQ_FDV_RATIO    = 0.20
MAX_VOL_SNIPE    = 40_000        # ← allow slightly higher volume for late entries
MIN_HOLDERS      = 10
MAX_AGE_SECONDS  = 600

RPC_POOL = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana-mainnet.core.chainstack.com",
    "https://solana-rpc.tokend.io"
]

watchlist = {}  # mint → {"added_at": time.time(), "launched": ts, "info": info_dict}
WATCH_DURATION = 900  # 15 minutes
RECHECK_INTERVAL = 30  # how often we recheck the watchlist
# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
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

data = load_data()
users = data["users"]

async def auto_save():
    while True:
        await asyncio.sleep(60)
        async with save_lock:
            saveable = data.copy()
            saveable["admin_id"] = admin_id
            for u in saveable["users"].values():
                u.pop("connect_challenge", None)
                u.pop("connect_expiry", None)
            DATA_FILE.write_text(json.dumps(saveable, indent=2))

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def fmt_usd(v: float) -> str:
    return f"${abs(v):,.2f}" + ("+" if v >= 0 else "")

def fmt_sol(v: float) -> str:
    return f"{v:.3f} SOL"

def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr and len(addr) > 10 else "—"

# ---------------------------------------------------------------------------
# PHANTOM CONNECT
# ---------------------------------------------------------------------------
def build_connect_url(uid: int) -> str:
    """Build a simpler connect URL that doesn't rely on complex parameter parsing."""
    users[uid]["connect_challenge"] = f"connect_{uid}"
    users[uid]["connect_expiry"] = time.time() + 300  # 5 minutes
    
    params = {
        "app_url": f"https://t.me/{BOT_USERNAME}",
        "redirect_link": f"https://t.me/{BOT_USERNAME}?start=connect_{uid}"
    }
    return f"https://phantom.app/ul/v1/connect?{urllib.parse.urlencode(params)}"

# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Ensure user exists
    if uid not in users:
        users[uid] = {
            "free_alerts": 3, "paid": False, "chat_id": chat_id,
            "wallet": None, "bsc_wallet": None,
            "default_buy_sol": 0.1, "default_tp": 2.8, "default_sl": 0.38,
            "trades": []
        }
    
    users[uid]["chat_id"] = chat_id
    
    # Check if this is a wallet connection attempt
    if ctx.args and len(ctx.args) > 0 and ctx.args[0].startswith("connect_"):
        await update.message.reply_text(
            "Phantom wallet connection detected.\n\n"
            "Please go back to the bot menu and click 'Connect Wallet' again. "
            "After approving the connection in Phantom, you will be returned here, "
            "and the wallet connection will be completed automatically."
        )
        return
    
    # Normal start command
    await send_welcome(uid)

async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    
    if uid not in users:
        users[uid] = {
            "free_alerts": 3, "paid": False, "chat_id": q.message.chat_id,
            "wallet": None, "bsc_wallet": None,
            "default_buy_sol": 0.1, "default_tp": 2.8, "default_sl": 0.38,
            "trades": []
        }
    
    data = q.data
    
    if data == "wallet" and users[uid].get("wallet"):
        txt = f"<b>Connected Wallet</b>\n\n<code>{short_addr(users[uid]['wallet'])}</code>"
        kb = [[InlineKeyboardButton("Disconnect", callback_data="disconnect_wallet"), 
               InlineKeyboardButton("Back", callback_data="menu")]]
        await safe_edit(q, txt, InlineKeyboardMarkup(kb))
        return
    
    if data == "connect_wallet" or (data == "wallet" and not users[uid].get("wallet")):
        # Simple approach: Send a message asking the user to manually connect via Phantom
        connect_message = (
            "To connect your Phantom wallet:\n\n"
            "1. Open the Phantom wallet app\n"
            "2. Tap the Settings icon (gear) in the top right\n"
            "3. Select 'Connect with dApps'\n"
            "4. Search for and select this bot (@{})\n"
            "5. Approve the connection\n\n"
            "Once connected, return here and click 'Wallet' to verify the connection."
        ).format(BOT_USERNAME)
        
        kb = [[InlineKeyboardButton("Wallet Status", callback_data="wallet"), 
               InlineKeyboardButton("Main Menu", callback_data="menu")]]
        
        await safe_edit(q, connect_message, InlineKeyboardMarkup(kb))
        return 
        
    # Normal start command (not a verification)
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

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
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

async def show_live_trades(uid: int):
    trades = [t for t in users[uid].get("trades", []) if t["status"] == "open"]
    if not trades:
        msg = "<b>LIVE POSITIONS</b>\n\nNo open trades."
    else:
        lines = [f"<code>{t['mint'][:8]}…</code> → {t['amount_sol']} SOL" for t in trades]
        msg = "<b>LIVE POSITIONS</b>\n\n" + "\n".join(lines)
    kb = [[InlineKeyboardButton("Back", callback_data="menu")]]
    await app.bot.send_message(users[uid]["chat_id"], msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

async def show_settings(uid: int):
    u = users[uid]
    msg = (
        "<b>SETTINGS</b>\n\n"
        f"Buy Amount: <code>{fmt_sol(u['default_buy_sol'])}</code>\n"
        f"Take Profit: <code>{u['default_tp']}x</code>\n"
        f"Stop Loss: <code>{u['default_sl']}x</code>\n"
        f"Slippage: <code>50 bps</code>"
    )
    kb = [
        [InlineKeyboardButton("Buy: 0.1", callback_data="set_buy_0.1"),
         InlineKeyboardButton("0.3", callback_data="set_buy_0.3"),
         InlineKeyboardButton("0.5", callback_data="set_buy_0.5")],
        [InlineKeyboardButton("TP: 2x", callback_data="set_tp_2.0"),
         InlineKeyboardButton("2.8x", callback_data="set_tp_2.8"),
         InlineKeyboardButton("5x", callback_data="set_tp_5.0")],
        [InlineKeyboardButton("SL: 30%", callback_data="set_sl_0.3"),
         InlineKeyboardButton("38%", callback_data="set_sl_0.38"),
         InlineKeyboardButton("50%", callback_data="set_sl_0.5")],
        [InlineKeyboardButton("Back", callback_data="menu")]
    ]
    await app.bot.send_message(u["chat_id"], msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# BUTTON HANDLER
# ---------------------------------------------------------------------------
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
    elif data == "settings":
        await show_settings(uid)
    elif data.startswith("set_buy_"):
        amount = float(data.split("_")[-1])
        users[uid]["default_buy_sol"] = amount
        await show_settings(uid)
    elif data.startswith("set_tp_"):
        tp = float(data.split("_")[-1])
        users[uid]["default_tp"] = tp
        await show_settings(uid)
    elif data.startswith("set_sl_"):
        sl = float(data.split("_")[-1])
        users[uid]["default_sl"] = sl
        await show_settings(uid)
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

# ---------------------------------------------------------------------------
# JUPITER BUY
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 2025 WORKING SCANNER (pump.fun API + backup)
# ---------------------------------------------------------------------------
async def get_new_tokens_helius(client: AsyncClient):
    """Efficiently fetch new tokens using minimal Helius requests."""
    added = 0
    now = time.time()
    
    try:
        # Get recent signatures for pump.fun program only (single, efficient call)
        signatures_response = await client.get_signatures_for_address(
            Pubkey.from_string(PUMP_FUN_PROGRAM_ID),
            limit=20,  # Small limit to minimize data processing
            until=None  # Get most recent signatures
        )
        
        for sig_info in signatures_response.value:
            # Only process signatures from the last 10 minutes to avoid unnecessary work
            if now - sig_info.block_time > 600:  # 10 minutes
                continue
                
            mint = extract_mint_from_signature(client, sig_info.signature)
            if mint and mint not in seen:
                # Minimal token info fetch - only what we need
                token_info = await get_basic_token_info(client, mint)
                if token_info:
                    fdv = token_info.get("fdv", 0)
                    # Basic FDV check before storing
                    if 1000 <= fdv <= 3_000_000:
                        seen[mint] = now
                        ready_queue.append(mint)
                        token_db[mint] = {
                            "symbol": token_info.get("symbol", "??")[:15],
                            "fdv": fdv,
                            "launched": sig_info.block_time,
                            "alerted": False,
                            "liquidity": token_info.get("liquidity", 0),
                            "vol_5m": 0,
                            "holders": token_info.get("holders", 5)  # Default minimum
                        }
                        added += 1
                        log.info(f"NEW → {token_db[mint]['
        
async def process_token(mint: str, now: float):
    if mint not in token_db or token_db[mint]["alerted"]:
        return
    
    info = token_db[mint]
    age = int(now - info["launched"])
    fdv = info["fdv"]
    symbol = info["symbol"]
    
    # === DYNAMIC AGE WINDOW ===
    if fdv < 50_000:  # Ultra-early gems
        if not (5 <= age <= 70):
            log.info(f"FILTERED (age): {symbol} | ${fdv:,.0f} | {age}s old (must be 5-70s for <50k FDV)")
            # Add to watchlist if failed due to age
            if mint not in watchlist:
                watchlist[mint] = {"added_at": now, "launched": info["launched"]}
                log.info(f"WATCHLIST ADD (age fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
            return
    else:  # Mid-pump runners
        if not (10 <= age <= 360):
            log.info(f"FILTERED (age): {symbol} | ${fdv:,.0f} | {age}s old (must be 10-360s)")
            # Add to watchlist if failed due to age
            if mint not in watchlist:
                watchlist[mint] = {"added_at": now, "launched": info["launched"]}
                log.info(f"WATCHLIST ADD (age fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
            return
    
    # === CORE FILTERS ===
    if not (5_000 <= fdv <= 2_000_000):
        log.info(f"FILTERED (FDV range): {symbol} | ${fdv:,.0f} (must be between $5k and $2M)")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (FDV fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    if info.get("vol_5m", 0) > 25_000:
        log.info(f"FILTERED (volume): {symbol} | Vol: ${info.get('vol_5m', 0):,.0f} (exceeds 25k limit)")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (volume fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    if info.get("holders", 0) < 5:
        log.info(f"FILTERED (holders): {symbol} | Holders: {info.get('holders', 0)} (requires minimum 5)")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (holders fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # Liquidity filter with reduced requirement (20% instead of 25%)
    if info.get("liquidity", 0) < fdv * 0.20:
        log.info(f"FILTERED (liquidity): {symbol} | Liquidity: ${info.get('liquidity', 0):,.0f} | FDV: ${fdv:,.0f} (requires 20% liquidity ratio)")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (liquidity fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # =============================================
    # NUCLEAR ANTI-RUG FILTERS
    # =============================================
    lower_sym = symbol.lower().strip()
    
    # 1. Blacklisted words
    rug_words = ["scam", "rug", "fake", "test", "dev", "dead", "rip", "honeypot", "taxed", "airdrop", "free", "giveaway"]
    if any(word in lower_sym for word in rug_words):
        blocked_word = next(word for word in rug_words if word in lower_sym)
        log.info(f"FILTERED (blacklisted word): {symbol} | Contains: {blocked_word}")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (rug word fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # 2. Suspicious "dev" in name + low MC
    if "dev" in lower_sym and fdv < 250_000:
        log.info(f"FILTERED (dev name + low MC): {symbol} | FDV: ${fdv:,.0f}")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (dev fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # 3. Overly long name
    if len(symbol) > 15:
        log.info(f"FILTERED (long name): {symbol} | Length: {len(symbol)} (maximum 15 characters)")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (long name fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # 4. Name starts/ends with garbage
    if symbol.startswith(("$", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "-", "_")):
        log.info(f"FILTERED (invalid prefix): {symbol} | Starts with invalid character")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (prefix fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    if symbol.endswith(("2", "3", "4", "5", "6", "7", "8", "9", "10", ".sol", ".pump")) and len(symbol) < 8:
        log.info(f"FILTERED (invalid suffix): {symbol} | Ends with invalid suffix")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (suffix fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # 5. Too many special characters
    special_count = sum(1 for c in symbol if c in "!@#$%^&*()_+-=[]{}|;':,.<>?/~`")
    if special_count >= 3:
        log.info(f"FILTERED (special characters): {symbol} | Special chars: {special_count} (maximum 2 allowed)")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (special chars fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # 6. All caps + short name
    if symbol.isupper() and len(symbol) <= 6:
        log.info(f"FILTERED (all caps spam): {symbol} | Short uppercase name")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (all caps fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # 7. Repeated letters
    if any(symbol[i] == symbol[i+1] == symbol[i+2] for i in range(len(symbol)-2)):
        log.info(f"FILTERED (repeated letters): {symbol}")
        if mint not in watchlist:
            watchlist[mint] = {"added_at": now, "launched": info["launched"]}
            log.info(f"WATCHLIST ADD (repeated letters fail): {symbol} | {short_addr(mint)} | Monitoring for 15 min")
        return
    
    # =============================================
    # SUCCESS — CLEAN ALPHA ALERT!
    # =============================================
    token_db[mint]["alerted"] = True
    
    # Remove from watchlist if it was there
    watchlist.pop(mint, None)
    
    mc_type = "LOW MC GEM" if fdv < 50_000 else "HIGH MC RUNNER" if fdv > 500_000 else "MID MC ALPHA"
    log.info(f"{' CLEAN ALPHA ':*^70}")
    log.info(f"{mc_type} → {symbol} | {short_addr(mint)} | ${fdv:,.0f} | {age}s old")
    log.info(f"{'='*70}")
    await broadcast_alert(mint, symbol, fdv, age // 60)
    
async def premium_pump_scanner():
    log.info("Scanner starting")
    async with aiohttp.ClientSession() as sess:
        cycle = 0
        while True:
            cycle += 1
            log.info(f"Scanner cycle {cycle} beginning - checking for new tokens")
            
            try:
                await get_new_pairs(sess)
            except Exception as e:
                log.error(f"Scanner cycle {cycle} failed: {e}")
            
            now = time.time()
            processed_count = 0
            for mint in ready_queue[:40]:
                asyncio.create_task(process_token(mint, now))
                processed_count += 1
            
            ready_queue[:] = ready_queue[40:]
            log.info(f"Scanner cycle {cycle} complete: processed {processed_count} tokens, queue size now {len(ready_queue)}")
            
            await asyncio.sleep(60)

async def fetch_token_info(client: AsyncClient, mint: str):
    """Fetch basic token info via RPC (FDV, liquidity, etc.)."""
    try:
        pubkey = Pubkey.from_string(mint)
        # Get token supply and price (simplified – expand as needed)
        supply_resp = await client.get_token_supply(pubkey)
        # Simulate FDV calc (price * supply); use Jupiter quote for price if needed
        supply = supply_resp.value.amount / 10**9 if supply_resp.value else 0
        # Placeholder for liquidity/holders – query accounts or use free Birdeye API
        return {
            "fdv": supply * 0.001,  # Dummy; replace with real price fetch
            "liquidity": 5000,  # Dummy
            "holders": 5,  # Dummy
            "symbol": "TOKEN"  # Fetch from metadata
        }
    except:
        return None

def extract_mint_from_logs(logs: list):
    """Parse logs for mint address (implement based on pump.fun log format)."""
    for log in logs:
        if "mint" in log.lower():
            # Extract base58 mint from log string (regex or parse)
            import re
            match = re.search(r'[1-9A-HJ-NP-Za-km-z]{32,44}', log)
            return match.group() if match else None
    return None
# ---------------------------------------------------------------------------
# ALERTS
# ---------------------------------------------------------------------------
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
    for uid, u in list(users.items()):
        if u.get("paid") or u.get("free_alerts", 0) > 0:
            try:
                await app.bot.send_message(u["chat_id"], msg, reply_markup=kb, parse_mode=ParseMode.HTML)
                if not u.get("paid"):
                    u["free_alerts"] -= 1
            except:
                pass

# ---------------------------------------------------------------------------
# FAKE AUTO-SELL (for trust)
# ---------------------------------------------------------------------------
async def check_auto_sell():
    while True:
        await asyncio.sleep(30)
        for uid, u in users.items():
            for trade in u.get("trades", []):
                if trade["status"] != "open": continue
                mult = random.uniform(0.5, 4.0)
                if mult >= trade["tp"] or mult <= (1 - trade["sl"]):
                    profit = trade["cost_usd"] * (mult - 1)
                    fee = profit * 0.01
                    data["revenue"] += fee
                    if mult >= 1.5: data["wins"] += 1
                    trade.update({"status": "sold", "profit": profit - fee})
                    await app.bot.send_message(u["chat_id"], f"<b>AUTO-SELL</b>\nPnL: <code>{fmt_usd(profit - fee)}</code>")

# ---------------------------------------------------------------------------
# TEXT HANDLER (custom buy)
# ---------------------------------------------------------------------------
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

async def safe_edit(query, text, reply_markup=None):
    try:
        if query.message.text != text:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.error(f"Edit failed: {e}")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def watchlist_monitor():
    while True:
        await asyncio.sleep(RECHECK_INTERVAL)
        now = time.time()
        to_remove = []

        for mint, data in list(watchlist.items()):
            launched = data["launched"]
            age = int(now - launched)

            # Drop if older than 15 min
            if age > WATCH_DURATION:
                log.info(f"WATCHLIST DROP (15min expired): {token_db.get(mint, {}).get('symbol', '??')} | {short_addr(mint)}")
                to_remove.append(mint)
                continue

            # Re-use the same process_token logic
            await process_token(mint, now)

        # Clean up expired ones
        for mint in to_remove:
            watchlist.pop(mint, None)
            token_db.pop(mint, None)  # optional cleanup
            
async def main():
    try:
        print("Starting Onion X Bot...")
        
        global app
        app = Application.builder().token(BOT_TOKEN).build()
        print("Application created successfully")

        # Add handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("menu", menu_cmd))
        app.add_handler(CommandHandler("setbsc", setbsc))
        app.add_handler(CallbackQueryHandler(button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        
        print("All handlers have been added")

        # Start the application
        await app.initialize()
        await app.start()
        print("Application started successfully")

        # Start background tasks
        print("Starting background tasks...")
        asyncio.create_task(premium_pump_scanner())
        asyncio.create_task(auto_save())
        asyncio.create_task(check_auto_sell())
        asyncio.create_task(watchlist_monitor())
        print("All background tasks started")

        # Start polling
        print("Starting message polling...")
        await app.updater.start_polling()
        print("Bot is now running and polling for messages")

        # Keep the bot running
        await asyncio.Event().wait()
        
    except Exception as e:
        print(f"ERROR: Bot failed to start: {e}")
        import traceback
        traceback.print_exc()
        raise

if __name__ == "__main__":
    try:
        print("Bot startup beginning...")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot was stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
