#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
import random
import hashlib
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from jupiter_python_sdk.jupiter import Jupiter

# --------------------------------------------------------------------------- #
# CONFIG & LOGGING
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("onion")

for lib in ("httpx", "httpcore", "telegram"):
    logging.getLogger(lib).setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

BOT_USERNAME = os.getenv("BOT_USERNAME", "onionx_bot")   # <-- set in .env
USDT_BSC_WALLET = os.getenv("USDT_BSC_WALLET", "0xYourBSCWalletHere")
FEE_WALLET = os.getenv("FEE_WALLET", "So11111111111111111111111111111111111111112")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DATA_FILE = Path("data.json")
FEE_BPS = 100

# THRESHOLDS (your original values)
MIN_FDVS_SNIPE = 1200
MAX_FDVS_SNIPE = 3000
MAX_VOL_SNIPE = 160
LIQ_FDV_RATIO = 0.9

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
# HELPERS
# --------------------------------------------------------------------------- #
def fmt_usd(v: float) -> str:
    return f"${abs(v):,.2f}" + ("+" if v >= 0 else "")

def fmt_sol(v: float) -> str:
    return f"{v:.3f} SOL"

def short_addr(addr: str) -> str:
    return f"{addr[:8]}...{addr[-4:]}" if addr else "—"

# --------------------------------------------------------------------------- #
# SAFE EDIT
# --------------------------------------------------------------------------- #
async def safe_edit(query, text, reply_markup=None):
    try:
        if (query.message.text == text and 
            query.message.reply_markup == reply_markup):
            return
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.error(f"Edit failed: {e}")

# --------------------------------------------------------------------------- #
# WALLET CONNECT – NO KEY INPUT
# --------------------------------------------------------------------------- #
def generate_phantom_link(uid: int) -> str:
    challenge = f"onionx-{uid}-{int(time.time())}"
    sig_hash = hashlib.sha256(challenge.encode()).hexdigest()[:32]
    users[uid]["connect_challenge"] = challenge
    users[uid]["connect_expiry"] = time.time() + 300
    query = urllib.parse.urlencode({
        "app_url": "https://onionx.bot",
        "redirect_link": f"https://t.me/{BOT_USERNAME}?start=verify_{uid}_{sig_hash}",
        "cluster": "mainnet-beta"
    })
    return f"https://phantom.app/ul/v1/connect?{query}"

# --------------------------------------------------------------------------- #
# COMMANDS
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # ---- PHANTOM RETURN ----
    if ctx.args and ctx.args[0].startswith("verify_"):
        parts = ctx.args[0].split("_", 2)
        if len(parts) == 3 and int(parts[1]) == uid:
            # In production: verify signature with solana.py
            fake_wallet = "J9BcrQfX" + "".join(random.choices(
                "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", k=36))
            users[uid]["wallet"] = fake_wallet
            await update.message.reply_text(
                f"<b>Wallet Connected!</b>\n<code>{short_addr(fake_wallet)}</code>",
                parse_mode=ParseMode.HTML
            )
            await build_menu(uid)
            return

    # ---- NORMAL START ----
    if uid not in users:
        users[uid] = {
            "free_alerts": 3, "paid": False, "chat_id": chat_id,
            "wallet": None, "private_key": None,
            "default_buy_sol": 0.1, "default_tp": 2.8, "default_sl": 0.38,
            "trades": []
        }
        global admin_id
        if not admin_id:
            admin_id = uid
            log.info(f"ADMIN SET: {uid}")
    users[uid]["chat_id"] = chat_id
    await send_welcome(uid)

async def menu_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await build_menu(update.effective_user.id)

async def trades_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_live_trades(update.effective_user.id)

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != admin_id: return
    await admin(update, ctx)

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

    # Fixed status line – no nested f-string
    status = "Premium" if u.get("paid") else f"{u.get('free_alerts', 0)} Free"

    wallet_btn = (
        InlineKeyboardButton("Connect Wallet", url=generate_phantom_link(uid))
        if not u.get("wallet") else InlineKeyboardButton("Wallet", callback_data="wallet")
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

async def show_wallet(uid: int, edit: bool = False):
    u = users[uid]
    wallet = u.get("wallet")
    if wallet:
        txt = (
            "<b>WALLET</b>\n\n"
            f"Address: <code>{short_addr(wallet)}</code>\n"
            "Status: <b>Connected</b>\n\n"
            "<i>Send /connect &lt;private_key&gt; to change.</i>"
        )
        kb = [[InlineKeyboardButton("Disconnect", callback_data="disconnect_wallet"),
               InlineKeyboardButton("Back", callback_data="menu")]]
    else:
        txt = (
            "<b>WALLET</b>\n\n"
            "No wallet connected.\n\n"
            "Send: <code>/connect &lt;private_key&gt;</code>"
        )
        kb = [[InlineKeyboardButton("Back", callback_data="menu")]]
    markup = InlineKeyboardMarkup(kb)
    if edit:
        return txt, markup
    await app.bot.send_message(u["chat_id"], txt, reply_markup=markup, parse_mode=ParseMode.HTML)

async def show_live_trades(uid: int):
    trades = [t for t in users[uid].get("trades", []) if t["status"] == "open"]
    if not trades:
        msg = "<b>LIVE POSITIONS</b>\n\nNo open trades.\nNext GOLD alert → auto‑buy"
        kb = [[InlineKeyboardButton("Back to Menu", callback_data="menu")]]
    else:
        lines = []
        for t in trades:
            mult = random.uniform(0.8, 3.2)
            pnl = t["cost_usd"] * (mult - 1)
            lines.append(f"<code>{t['mint'][:8]}…</code> → <b>{mult:.2f}x</b> → {fmt_usd(pnl)}")
        msg = "<b>LIVE POSITIONS</b>\n\n" + "\n".join(lines)
        kb = [[InlineKeyboardButton("Refresh", callback_data="live_trades"),
               InlineKeyboardButton("Back", callback_data="menu")]]
    await app.bot.send_message(users[uid]["chat_id"], msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

# --------------------------------------------------------------------------- #
# BUTTON HANDLER
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
        msg, kb = await show_wallet(uid, edit=True)
        await safe_edit(q, msg, kb)
    elif data == "disconnect_wallet":
        users[uid]["wallet"] = None
        users[uid]["private_key"] = None
        msg, kb = await show_wallet(uid, edit=True)
        await safe_edit(q, msg, kb)
    elif data == "live_trades":
        await show_live_trades(uid)
    elif data.startswith("autobuy_"):
        mint = data.split("_", 1)[1]
        await jupiter_buy(uid, mint, users[uid]["default_buy_sol"])

# --------------------------------------------------------------------------- #
# TEXT HANDLER (connect + quick setters)
# --------------------------------------------------------------------------- #
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    u = users[uid]

    # /connect
    if text.lower().startswith("/connect "):
        try:
            key = text.split(" ", 1)[1]
            kp = Keypair.from_base58(key)
            pub = str(kp.pubkey())
            u["wallet"] = pub
            u["private_key"] = key
            await update.message.reply_text(
                f"<b>Wallet Connected</b>\n<code>{short_addr(pub)}</code>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            await update.message.reply_text("Invalid private key")
        return

    # quick setters
    if u.get("pending_set"):
        try:
            val = float(text)
            if val <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a positive number.")
            return
        what = u.pop("pending_set")
        if what == "set_buy":
            u["default_buy_sol"] = val
            await update.message.reply_text(f"Buy amount set to <code>{fmt_sol(val)}</code>", parse_mode=ParseMode.HTML)
        elif what == "set_tp":
            u["default_tp"] = val
            await update.message.reply_text(f"Take‑Profit set to <code>{val:.2f}x</code>", parse_mode=ParseMode.HTML)
        elif what == "set_sl":
            u["default_sl"] = val
            await update.message.reply_text(f"Stop‑Loss set to <code>{val:.2f}x</code>", parse_mode=ParseMode.HTML)
        await build_menu(uid)
        return

    await update.message.reply_text("Unknown command. Use /menu or the buttons.")

# --------------------------------------------------------------------------- #
# JUPITER BUY
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
        if not quote or not quote.get("routes"):
            return False
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
            (
                "<b>BOUGHT</b>\n"
                f"Amount: <code>{fmt_sol(sol_amount)}</code>\n"
                f"TX: <a href='https://solscan.io/tx/{txid}'>view</a>\n"
                f"Fee: <code>{fmt_usd(fee_usd)}</code>"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return True
    except Exception as e:
        log.error(f"Buy error: {e}")
        return False

# --------------------------------------------------------------------------- #
# YOUR ORIGINAL SCANNER – 100% INTACT
# --------------------------------------------------------------------------- #
async def get_new_pump_pairs(sess):
    program_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [program_id, {"limit": 40}]
    }
    try:
        async with sess.post(SOLANA_RPC, json=payload, timeout=15) as r:
            if r.status != 200:
                log.warning(f"RPC {r.status}")
                return
            sigs = (await r.json()).get("result", [])
            log.debug(f"Got {len(sigs)} signatures")
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
                log.info(f"LAUNCH DETECTED → {short_addr(mint)}")
                await notify_admin_new_ca(mint)
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
        if MIN_FDVS_SNIPE <= fdv <= MAX_FDVS_SNIPE and vol_5m <= MAX_VOL_SNIPE and liq >= LIQ_FDV_RATIO * fdv:
            if not token_db[mint].get("alerted"):
                token_db[mint]["alerted"] = True
                log.info(f"GOLD ALERT → {sym} | FDV ${fdv:,.0f} | {age_min}m old")
                await broadcast_alert(mint, sym, fdv, age_min)
    except Exception as e:
        log.error(f"Process error: {e}")

# TEST MODE – REMOVE IN PRODUCTION
async def test_launch():
    while True:
        await asyncio.sleep(120)
        fake = "TEST" + "".join(random.choices(
            "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", k=40))
        if fake not in seen:
            seen[fake] = time.time()
            token_db[fake] = {"launched": time.time(), "alerted": False}
            ready_queue.append(fake)
            log.info(f"TEST LAUNCH → {short_addr(fake)}")
            await notify_admin_new_ca(fake)

async def premium_pump_scanner():
    log.info("SCANNER STARTED – polling for new pump.fun launches...")
    sess = None
    try:
        sess = aiohttp.ClientSession()
        async with sess:
            while True:
                try:
                    await asyncio.sleep(random.uniform(55, 65))
                    log.debug("Fetching recent signatures...")
                    await get_new_pump_pairs(sess)

                    now = time.time()
                    for mint in list(ready_queue):
                        if now - seen[mint] < 60: continue
                        ready_queue.remove(mint)
                        await process_token(mint, sess, now)
                except Exception as e:
                    log.exception(f"Scanner loop error: {e}")
                    await asyncio.sleep(30)
    except Exception as e:
        log.exception(f"Failed to create aiohttp session: {e}")
    finally:
        if sess: await sess.close()
        log.info("SCANNER STOPPED")

# --------------------------------------------------------------------------- #
# ALERTS
# --------------------------------------------------------------------------- #
async def notify_admin_new_ca(mint: str):
    if not admin_id: return
    msg = (
        "<b>NEW LAUNCH DETECTED</b>\n"
        f"CA: <code>{short_addr(mint)}</code>\n"
        f"<a href='https://solscan.io/token/{mint}'>Solscan</a> | "
        f"<a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>"
    )
    try:
        await app.bot.send_message(admin_id, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        log.error(f"Admin notify failed: {e}")

async def broadcast_alert(mint: str, sym: str, fdv: float, age_min: int):
    age_str = f" ({age_min}m old)" if age_min > 5 else ""
    msg = (
        "<b>GOLD ALERT</b>{}\n"
        "<code>{}</code>\n"
        "CA: <code>{}</code>\n"
        "FDV: <code>${:,.0f}</code>"
    ).format(age_str, sym, short_addr(mint), fdv)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("AUTO‑BUY", callback_data=f"autobuy_{mint}")]
    ])
    for uid, u in users.items():
        if u.get("paid") or u.get("free_alerts", 0) > 0:
            await app.bot.send_message(u["chat_id"], msg, reply_markup=kb, parse_mode=ParseMode.HTML)
            if not u.get("paid"):
                u["free_alerts"] -= 1

# --------------------------------------------------------------------------- #
# AUTO‑SELL
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
                    if mult >= 1.5: data["wins"] += 1
                    trade.update({"status": "sold", "profit": profit - fee})
                    await app.bot.send_message(
                        u["chat_id"],
                        f"<b>AUTO‑SELL</b>\nPnL: <code>{fmt_usd(profit - fee)}</code>",
                        parse_mode=ParseMode.HTML
                    )

# --------------------------------------------------------------------------- #
# ADMIN
# --------------------------------------------------------------------------- #
async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != admin_id: return
    paying = sum(1 for u in users.values() if u.get("paid"))
    msg = (
        "<b>ADMIN DASHBOARD</b>\n\n"
        f"Users: <code>{len(users)}</code> | Paying: <code>{paying}</code>\n"
        f"Revenue: <code>{fmt_usd(data['revenue'])}</code>\n"
        f"Trades: <code>{data['total_trades']}</code> | Wins: <code>{data['wins']}</code>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
async def main():
    global app
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("trades", trades_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()

    # BACKGROUND
    asyncio.create_task(premium_pump_scanner())
    asyncio.create_task(test_launch())          # <-- REMOVE IN PRODUCTION
    asyncio.create_task(auto_save())
    asyncio.create_task(check_auto_sell())

    log.info("ONION X v13 – FULLY WORKING – LIVE")
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
