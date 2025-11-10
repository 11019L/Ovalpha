#!/usr/bin/env python3
import os
import asyncio
import json
import time
import logging
import random
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
PUMPFUN_API = "https://frontend-api.pump.fun"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"
DEXSCREENER_TRENDING_URL = "https://api.dexscreener.com/latest/dex/tokens/trending"
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
            for mint, state in raw.get("token_state", {}).items():
                if "sent" in state:
                    state["sent"] = set(state["sent"])
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
        # CONVERT SETS TO LISTS
        saveable = data.copy()
        for mint, state in saveable.get("token_state", {}).items():
            if "sent" in state:
                state["sent"] = list(state["sent"])
        DATA_FILE.write_text(json.dumps(saveable, indent=2))
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
        # DexScreener LP check (only reliable method now)
        async with sess.get(f"{DEXSCREENER_TOKENS_URL}/{mint}", timeout=10) as r:
            if r.status == 200:
                data = await r.json()
                pairs = data.get("pairs", [])
                for p in pairs:
                    liq = p.get("liquidity", {}).get("usd", 0)
                    if liq > 100:
                        # Bonus: Check if LP is locked/burned (via tx count or metadata)
                        txs = p.get("txns", {}).get("h24", 0) or 0
                        if txs > 10:  # Some activity = safer
                            return True
        return False
    except Exception as e:
        log.debug(f"is_safe_pump failed for {mint}: {e}")
        return False


async def detect_large_buy(mint: str, sess) -> float:
    # Birdeye (fast, if key set)
    if BIRDEYE_KEY and BIRDEYE_KEY != "YOUR_BIRDEYE_KEY":
        try:
            url = "https://public-api.birdeye.so/defi/history_transactions"
            params = {"address": mint, "type": "buy", "limit": 5}
            headers = {"X-API-KEY": BIRDEYE_KEY}
            async with sess.get(url, params=params, headers=headers, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    amounts = [t.get("usdAmount", 0) for t in data.get("data", []) if t.get("usdAmount", 0) > 0]
                    return max(amounts) if amounts else 0
        except Exception as e:
            log.debug(f"Birdeye failed: {e}")

    # Fallback: Solana RPC (slow but free)
    try:
        # Get pair & price
        async with sess.get(f"{DEXSCREENER_TOKENS_URL}/{mint}", timeout=10) as r:
            if r.status != 200:
                return 0
            data = await r.json()
            pair = next((p for p in data.get("pairs", []) if p.get("quoteToken", {}).get("symbol") == "SOL"), None)
            if not pair:
                return 0
            pair_addr = pair["pairAddress"]
            price = float(pair.get("priceUsd", 0) or 0)
            if price <= 0:
                return 0

        # Recent sigs
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [pair_addr, {"limit": 5}]}
        async with sess.post(SOLANA_RPC, json=payload, timeout=15) as r:
            if r.status != 200:
                return 0
            sigs = (await r.json()).get("result", [])
            if not sigs:  # ← FIX: Handle empty
                return 0

        largest = 0.0
        for sig in sigs:
            tx_payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [sig["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]}
            async with sess.post(SOLANA_RPC, json=tx_payload, timeout=15) as tx_r:
                if tx_r.status != 200:
                    continue
                tx_data = await tx_r.json()
                result = tx_data.get("result")
                if not result:
                    continue
                pre = result["meta"].get("preTokenBalances", [])
                post = result["meta"].get("postTokenBalances", [])
                for bal in pre:
                    if bal.get("mint") != mint:
                        continue
                    owner = bal.get("owner")
                    if not owner:
                        continue
                    post_bal = next((p for p in post if p.get("mint") == mint and p.get("owner") == owner), None)
                    if not post_bal:
                        continue
                    pre_amt = bal["uiTokenAmount"].get("uiAmount", 0)
                    post_amt = post_bal["uiTokenAmount"].get("uiAmount", 0)
                    bought = pre_amt - post_amt
                    if bought > 0:
                        usd_value = bought * price
                        if usd_value > largest:
                            largest = usd_value
        return largest
    except Exception as e:
        log.debug(f"Solana whale detect failed: {e}")
        return 0
# --------------------------------------------------------------------------- #
#                               SCANNERS (with debug logs)
# --------------------------------------------------------------------------- #
async def premium_pump_scanner(app: Application):
    volume_hist = defaultdict(lambda: deque(maxlen=3))
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                await asyncio.sleep(random.uniform(8, 15))

                tokens = []
                page = 0
                total_fetched = 0

                # Fetch multiple pages of pump.fun tokens via DexScreener search
                while page < 3:  # 3 pages = ~150 tokens
                    params = {
                        "q": "pump.fun",  # Filters to pump.fun tokens
                        "chainId": "solana",
                        "limit": 50
                    }
                    async with sess.get(DEXSCREENER_SEARCH_URL, params=params, timeout=15) as r:
                        if r.status == 429:
                            await asyncio.sleep(60)
                            continue
                        if r.status != 200:
                            break
                        data = await r.json()
                        pairs = data.get("pairs", [])  # Safe: [] if None
                        if not pairs:
                            break

                        for p in pairs:
                            if "pump.fun" not in p.get("url", ""):
                                continue
                            if p.get("baseToken") is None:
                                continue
                            mint = p["baseToken"]["address"]
                            if not mint or t.get("dev_bought") is True:  # Skip if dev bought (from DexScreener metadata if available)
                                continue

                            tokens.append({
                                "mint": mint,
                                "symbol": p["baseToken"]["symbol"][:20],
                                "fdv": float(p.get("fdv", 0) or 0),
                                "liquidity": float(p.get("liquidity", {}).get("usd", 0) or 0),
                                "volume_5m": float(p.get("volume", {}).get("m5", 0) or 0),
                                "is_pumpfun": True
                            })
                        fetched = len(pairs)
                        total_fetched += fetched
                        page += 1
                        if fetched < 50:
                            break

                log.info(f"Fetched {total_fetched} pump.fun tokens from DexScreener")

                tokens_processed = 0
                for token in tokens:
                    mint = token["mint"]
                    if mint in seen:
                        continue
                    seen[mint] = time.time()

                    sym = token["symbol"]
                    fdv = token["fdv"]
                    liq = token["liquidity"]
                    vol = token["volume_5m"]

                    log.info(f"SCAN: {sym} | FDV ${fdv:,.0f} | Vol ${vol:,.0f} | Liq ${liq:,.0f}")

                    if not await is_safe_pump(mint, sess):
                        log.info(f"  → Unsafe, skipping {sym}")
                        continue

                    # WHALE DETECT
                    large = await detect_large_buy(mint, sess)
                    if large >= 1000:
                        extra = f"**\\${large:,.0f} WHALE BUY**\\n"
                        msg = format_alert(sym, mint, liq, fdv, vol, "whale", extra)
                        kb = [[InlineKeyboardButton("BUY NOW", callback_data=f"askbuy_{mint}")]]
                        await broadcast(msg, InlineKeyboardMarkup(kb))
                        continue

                    # VOLUME SPIKE & LEVELS
                    hist = volume_hist[mint]
                    hist.append(vol)
                    spike = len(hist) > 1 and vol >= (sum(hist[:-1]) / len(hist[:-1])) * 2.0

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

                log.info(f"Processed {tokens_processed} new tokens | Next scan in ~{random.uniform(8,15):.1f}s")

            except Exception as e:
                log.error(f"Premium scanner error: {e}")
                await asyncio.sleep(20)
                
async def market_pump_scanner(app: Application):
    volume_hist = defaultdict(lambda: deque(maxlen=3))
    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                await asyncio.sleep(random.uniform(15, 25))

                # Use DexScreener TRENDING (safe, no NoneType)
                async with sess.get(DEXSCREENER_TRENDING_URL, timeout=15) as r:
                    if r.status == 429:
                        await asyncio.sleep(60)
                        continue
                    if r.status != 200:
                        await asyncio.sleep(30)
                        continue
                    data = await r.json()
                    pairs = data.get("pairs")  # Can be None!
                    if not pairs:  # ← FIX: Check for None
                        log.warning("No trending pairs data")
                        await asyncio.sleep(30)
                        continue
                    pairs = pairs if isinstance(pairs, list) else []  # Ensure list

                processed = 0
                for pair in pairs:
                    if pair.get("chainId") != "solana":
                        continue
                    if "pump.fun" not in pair.get("url", ""):
                        continue
                    mint = pair["baseToken"]["address"]
                    if mint in seen:
                        continue
                    seen[mint] = time.time()

                    vol = float(pair.get("volume", {}).get("h1", 0) or 0)
                    if vol < 1000:
                        continue

                    log.info(f"MARKET: {pair['baseToken']['symbol']} | 1h Vol ${vol:,.0f}")

                    hist = volume_hist[mint]
                    hist.append(vol)
                    if len(hist) < 2:
                        continue
                    avg = sum(hist[:-1]) / len(hist[:-1])
                    if vol >= avg * 2.5:
                        fdv = float(pair.get("fdv", 0) or 0)
                        liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                        extra = f"**{vol/avg:.1f}x 1h SPIKE**\\n"
                        msg = format_alert(pair["baseToken"]["symbol"][:20], mint, liq, fdv, vol, "market", extra)
                        await broadcast(msg)
                        processed += 1

                log.info(f"Market scan: {processed} trending alerts")

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
