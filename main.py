#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import aiohttp
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# --------------------------------------------------------------------------- #
#                               LOGGING & CONFIG
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("onion")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in .env")


WALLET_BSC = os.getenv("WALLET_BSC", "0xYourWallet")
FEE_WALLET = os.getenv("FEE_WALLET")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PRICE_PREMIUM = 29.99
ETHERSCAN_KEY = os.getenv("ETHERSCAN_KEY", "")

DATA_FILE = Path("data.json")          # saved next to main.py
SAVE_INTERVAL = 30
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# --------------------------------------------------------------------------- #
#                               MARKDOWN ESCAPER
# --------------------------------------------------------------------------- #
def md(text: str) -> str:
    """Escape every character that MarkdownV2 treats specially."""
    escape = r'\_*[]()~`>#+-=|{}.!'
    for c in escape:
        text = text.replace(c, f'\\{c}')
    return text

# --------------------------------------------------------------------------- #
#                               PERSISTENCE
# --------------------------------------------------------------------------- #
def load_data():
    if DATA_FILE.is_file():
        try:
            raw = json.loads(DATA_FILE.read_text())
            for u in raw.get("users", {}).values():
                u.setdefault("free_alerts", 3)
                u.setdefault("paid", False)
                u.setdefault("paid_until", None)
                u.setdefault("chat_id", None)
                u.setdefault("wallet", None)
                u.setdefault("pending_buy", None)
            return raw
        except Exception as e:
            log.error(f"Load error: {e}")
    return {"users": {}, "seen": {}, "token_state": {}, "tracker": {}, "revenue": 0.0}

def save_data(data):
    try:
        DATA_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.error(f"Save error: {e}")

data = load_data()
users = data["users"]
seen = data["seen"]
token_state = data["token_state"]
tracker = data["tracker"]
data["revenue"] = data.get("revenue", 0.0)
save_lock = asyncio.Lock()

# --------------------------------------------------------------------------- #
#                               HELPERS
# --------------------------------------------------------------------------- #
async def safe_send(app, chat_id, text, reply_markup=None):
    """Send a MarkdownV2 message, fall back to plain text on error."""
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except Exception as e:
        log.warning(f"Markdown failed ({e}), sending plain")
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except Exception as e2:
            log.error(f"Plain send also failed: {e2}")

def pump_url(ca): return f"https://pump.fun/{ca}"

def format_alert(sym, addr, liq, fdv, vol, level, extra=""):
    level_map = {
        "snipe": "SNIPE", "confirm": "CONFIRM", "pump": "PUMP",
        "whale": "WHALE", "market": "MARKET PUMP"
    }
    e = level_map.get(level, level.upper())
    short = addr[:8] + "..." + addr[-6:]
    base = (
        f"*{e} ALERT* [PUMP]\n"
        f"`{escape_markdown(sym[:20], 2)}`\n"
        f"*CA:* `{escape_markdown(short, 2)}`\n"
        f"Liq: \\${liq:,.0f} \\| FDV: \\${fdv:,.0f}\n"
        f"5m Vol: \\${vol:,.0f}\n"
        f"{extra}"
        f"[View]({pump_url(addr)})"
    )
    return md(base)

# --------------------------------------------------------------------------- #
#                             SAFETY + WHALE BUY
# --------------------------------------------------------------------------- #
async def is_safe_pump(mint: str, sess) -> bool:
    try:
        async with sess.get(f"https://public-api.solscan.io/token/meta?tokenAddress={mint}") as r:
            if r.status != 200: return False
            if (await r.json()).get("data", {}).get("mintAuthority"): return False
        async with sess.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}") as r:
            if r.status != 200: return False
            pair = (await r.json()).get("pairs", [{}])[0]
            if not pair.get("liquidity", {}).get("usd"): return False
            holders = pair.get("topHolders", [])
            return any(h.get("address") == "11111111111111111111111111111111" for h in holders)
    except Exception:
        return False

async def detect_large_buy(mint: str, sess) -> float:
    try:
        async with sess.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}") as r:
            if r.status != 200: return 0
            pair = next((p for p in (await r.json()).get("pairs", []) if p.get("quoteToken", {}).get("symbol") == "SOL"), None)
            if not pair: return 0
            pair_addr = pair["pairAddress"]
            price = pair.get("priceUsd", 0)

        payload = {"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress","params":[pair_addr,{"limit":5}]}
        async with sess.post(SOLANA_RPC, json=payload) as r:
            sigs = (await r.json()).get("result", [])

        largest = 0
        for sig in sigs:
            async with sess.post(SOLANA_RPC, json={"jsonrpc":"2.0","id":1,
                "method":"getTransaction","params":[sig["signature"],{"encoding":"jsonParsed"}]}) as tx:
                tx_data = await tx.json()
                if not tx_data.get("result"): continue
                pre, post = tx_data["result"]["meta"].get("preTokenBalances", []), tx_data["result"]["meta"].get("postTokenBalances", [])
                for bal in pre:
                    if bal.get("mint") != mint: continue
                    post_bal = next((p for p in post if p.get("uiTokenAmount",{}).get("uiAmount")==bal["uiTokenAmount"]["uiAmount"]), None)
                    if not post_bal: continue
                    bought = bal["uiTokenAmount"]["uiAmount"] - post_bal["uiTokenAmount"]["uiAmount"]
                    if bought > 0 and bought * price > largest:
                        largest = bought * price
        return largest
    except Exception:
        return 0

# --------------------------------------------------------------------------- #
#                               SCANNERS (with debug logs)
# --------------------------------------------------------------------------- #
import random

async def premium_pump_scanner(app: Application):
    volume_hist = defaultdict(lambda: deque(maxlen=3))
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                # RANDOM DELAY: 8–15 seconds
                await asyncio.sleep(random.uniform(8, 15))

                # ROTATE ENDPOINTS
                endpoints = [
                    "https://api.dexscreener.com/latest/dex/search?q=pump.fun",
                    "https://api.dexscreener.com/latest/dex/tokens?pairs=pump.fun",
                ]
                url = random.choice(endpoints)

                async with sess.get(url, timeout=15) as r:
                    if r.status == 429:
                        log.warning("DexScreener 429 — backing off 30s")
                        await asyncio.sleep(30)
                        continue
                    if r.status != 200:
                        log.warning(f"DexScreener error: {r.status}")
                        continue

                    data = await r.json()
                    pairs = data.get("pairs", []) or data.get("tokens", [])

                tokens_processed = 0
                for pair in pairs:
                    if "pump.fun" not in pair.get("url", "") and pair.get("chainId") != "solana":
                        continue

                    mint = pair["baseToken"]["address"]
                    if mint in seen: continue
                    seen[mint] = time.time()

                    sym = pair["baseToken"]["symbol"][:20]
                    fdv = float(pair.get("fdv", 0) or 0)
                    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                    vol = float(pair.get("volume", {}).get("m5", 0) or 0)

                    log.info(f"SCAN: {sym} | FDV ${fdv:,.0f} | Vol ${vol:,.0f} | Liq ${liq:,.0f}")

                    if not await is_safe_pump(mint, sess): continue

                    # WHALE
                    large = await detect_large_buy(mint, sess)
                    if large >= 1000:
                        msg = format_alert(sym, mint, liq, fdv, vol, "whale", f"**\\${large:,.0f} BUY**\\n")
                        kb = [[InlineKeyboardButton("BUY NOW", callback_data=f"askbuy_{mint}")]]
                        await broadcast(msg, InlineKeyboardMarkup(kb))
                        continue

                    # SNIPE LOGIC
                    hist = volume_hist[mint]
                    hist.append(vol)
                    spike = len(hist) > 1 and vol >= sum(hist[:-1]) / len(hist[:-1]) * 2.0

                    level = None
                    if fdv >= 3000 and vol < 100 and liq > 100:
                        level = "snipe"
                    elif fdv >= 10000 and vol >= 300:
                        level = "confirm"
                    elif spike and vol >= 800:
                        level = "pump"

                    if level:
                        state = token_state.get(mint, {"sent": set()})
                        if level not in state["sent"]:
                            state["sent"].add(level)
                            token_state[mint] = state
                            msg = format_alert(sym, mint, liq, fdv, vol, level)
                            kb = [[InlineKeyboardButton("BUY NOW", callback_data=f"askbuy_{mint}")]] if level == "snipe" else None
                            await broadcast(msg, InlineKeyboardMarkup(kb) if kb else None)

                    tokens_processed += 1

                log.info(f"Scanned {tokens_processed} tokens | Next scan in {random.uniform(8,15):.1f}s")

            except Exception as e:
                log.error(f"Scanner error: {e}")
                await asyncio.sleep(20)

async def market_pump_scanner(app: Application):
    volume_hist = defaultdict(lambda: deque(maxlen=3))
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                await asyncio.sleep(random.uniform(12, 20))  # SLOWER

                async with sess.get(
                    "https://api.dexscreener.com/latest/dex/pairs/solana?rankBy=volume&order=desc&minLiquidity=10000",
                    timeout=15
                ) as r:
                    if r.status == 429:
                        await asyncio.sleep(60)
                        continue
                    if r.status != 200: continue
                    pairs = (await r.json()).get("pairs", [])

                for pair in pairs:
                    if "pump.fun" not in pair.get("url", ""): continue
                    mint = pair["baseToken"]["address"]
                    if mint in seen: continue
                    seen[mint] = time.time()

                    vol = float(pair.get("volume", {}).get("h1", 0) or 0)
                    log.info(f"MARKET: {pair['baseToken']['symbol']} | Vol ${vol:,.0f}")

                    hist = volume_hist[mint]
                    hist.append(vol)
                    if len(hist) < 2: continue
                    avg = sum(hist[:-1]) / len(hist[:-1])
                    if vol >= avg * 2.5 and vol >= 1000:
                        fdv = float(pair.get("fdv", 0) or 0)
                        msg = format_alert(pair["baseToken"]["symbol"], mint, 0, fdv, vol,
                                          "market", f"**{vol/avg:.1f}x SPIKE**\\n")
                        await broadcast(msg)

            except Exception as e:
                log.error(f"Market scanner error: {e}")
                await asyncio.sleep(30)

# --------------------------------------------------------------------------- #
#                               COMMANDS
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    source = ctx.args[0] if ctx.args and ctx.args[0].startswith("track_") else "organic"
    influencer = source.split("_", 1)[1] if "_" in source else None
    if influencer:
        tracker.setdefault(influencer, {"joins":0,"subs":0,"rev":0.0})["joins"] += 1

    if uid not in users:
        users[uid] = {"free_alerts": 3, "paid": False, "chat_id": chat_id,
                     "wallet": None, "pending_buy": None}
    users[uid]["chat_id"] = chat_id

    await update.message.reply_text(
        "<b>ONION PREMIUM</b>\n\n"
        "3 free SNIPE alerts\n"
        f"Premium: <code>\\${PRICE_PREMIUM}/mo</code>\n"
        f"Pay: <code>{WALLET_BSC}</code>\n"
        "<code>/pay TXID</code> | <code>/wallet YOUR_SOL_ADDRESS</code>",
        parse_mode="HTML"
    )

async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /pay TXID")
    txid = ctx.args[0]
    uid = update.effective_user.id
    url = f"https://api.etherscan.io/v2/api?module=account&action=tokentx&address={WALLET_BSC}&txhash={txid}&chainid=56&apikey={ETHERSCAN_KEY}"
    try:
        resp = requests.get(url).json()
        tx = resp.get("result", [{}])[0]
        if tx.get("tokenSymbol") == "USDT" and float(tx.get("value",0))/1e6 >= PRICE_PREMIUM:
            users[uid]["paid"] = True
            users[uid]["paid_until"] = (datetime.utcnow() + timedelta(days=30)).isoformat()
            users[uid]["free_alerts"] = 0
            data["revenue"] += PRICE_PREMIUM
            await update.message.reply_text(md("*PREMIUM ACTIVATED*"), parse_mode="MarkdownV2")
        else:
            await update.message.reply_text("Invalid TX or amount.")
    except Exception as e:
        log.error(f"pay error: {e}")
        await update.message.reply_text("TX verification failed.")

async def wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not users[uid].get("paid"):
        return await update.message.reply_text("Premium only.")
    if not ctx.args:
        return await update.message.reply_text("Usage: /wallet <your_solana_address>")
    addr = ctx.args[0].strip()
    if not (32 <= len(addr) <= 44):
        return await update.message.reply_text("Invalid address.")
    users[uid]["wallet"] = addr
    await update.message.reply_text(
        md(f"Wallet linked: `{addr[:8]}...{addr[-6:]}`"),
        parse_mode="MarkdownV2"
    )

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    total = len(users)
    premium = sum(1 for u in users.values() if u.get("paid"))
    msg = (
        f"*ADMIN DASHBOARD*\n"
        f"Users: {total}\n"
        f"Premium: {premium}\n"
        f"Revenue: \\${data['revenue']:.2f}"
    )
    await update.message.reply_text(md(msg), parse_mode="MarkdownV2")

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if users[uid].get("pending_buy"):
        del users[uid]["pending_buy"]
        await update.message.reply_text(md("Cancelled."))

# --------------------------------------------------------------------------- #
#                               AUTO-BUY
# --------------------------------------------------------------------------- #
async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("askbuy_"): return
    mint = query.data.split("_", 1)[1]
    uid = query.from_user.id
    if not users[uid].get("paid") or not users[uid].get("wallet"):
        return await query.edit_message_text(md("Link wallet: /wallet <addr>"))

    users[uid]["pending_buy"] = {"mint": mint, "time": time.time()}
    await query.edit_message_text(
        md(f"Enter amount in **\\$USD** (e.g. 50):\n"
           f"`{mint[:8]}...{mint[-6:]}`\n"
           f"Cancel: /cancel"),
        parse_mode="MarkdownV2"
    )

async def handle_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if uid not in users or "pending_buy" not in users[uid]: return

    pending = users[uid]["pending_buy"]
    if time.time() - pending["time"] > 60:
        del users[uid]["pending_buy"]
        return await update.message.reply_text(md("Expired."))

    try:
        usd = float(text)
        if usd < 1: raise ValueError
    except Exception:
        return await update.message.reply_text(md("Enter a number (e.g. 50)"))

    mint = pending["mint"]
    del users[uid]["pending_buy"]

    async with aiohttp.ClientSession() as sess:
        async with sess.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd") as r:
            sol_price = (await r.json())["solana"]["usd"]
        sol_amount = usd / sol_price

        quote_url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": mint,
            "amount": int(sol_amount * 1e9),
            "slippageBps": 50,
            "feeBps": 100,
        }
        async with sess.get(quote_url, params=params) as r:
            quote = await r.json()

        swap_url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "quoteResponse": quote,
            "userPublicKey": users[uid]["wallet"],
            "wrapAndUnwrapSol": True,
            "feeAccount": FEE_WALLET,
        }
        async with sess.post(swap_url, json=payload) as r:
            swap = await r.json()
            tx = swap.get("swapTransaction")
            if tx:
                await update.message.reply_text(
                    md(f"BOUGHT **\\${usd}** worth\\!\n"
                       f"~{sol_amount:.4f} SOL → `{mint[:8]}...`\n"
                       f"[View Tx](https://solscan.io/tx/{tx})"),
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(md("Swap failed."))

# --------------------------------------------------------------------------- #
#                               BROADCAST
# --------------------------------------------------------------------------- #
async def broadcast(msg, reply_markup=None):
    async with save_lock:
        for uid, u in users.items():
            if u.get("chat_id") and (u.get("paid") or u.get("free_alerts", 0) > 0):
                await safe_send(app, u["chat_id"], msg, reply_markup)
                if not u.get("paid") and u.get("free_alerts", 0) > 0:
                    u["free_alerts"] -= 1

# --------------------------------------------------------------------------- #
#                               MAIN
# --------------------------------------------------------------------------- #
async def main():
    global app
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount))

    await app.initialize()
    await app.start()

    asyncio.create_task(premium_pump_scanner(app))
    asyncio.create_task(market_pump_scanner(app))
    asyncio.create_task(auto_save())

    log.info("Bot is running... Waiting for messages...")
    await app.updater.start_polling()
    await asyncio.Event().wait()   # keep alive

# --------------------------------------------------------------------------- #
#                               AUTO-SAVE
# --------------------------------------------------------------------------- #
async def auto_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        async with save_lock:
            save_data(data)

# --------------------------------------------------------------------------- #
#                               RUN
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped by user.")
    except Exception as e:
        log.exception(f"Bot crashed: {e}")
