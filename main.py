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
from urllib.parse import quote

from dotenv import load_dotenv
load_dotenv()
from dotenv import load_dotenv
load_dotenv()

# ADD THIS:
import os
print("DEBUG .env:")
print("BOT_TOKEN:", bool(os.getenv("BOT_TOKEN")))
print("MORALIS_API_KEY:", os.getenv("MORALIS_API_KEY")[:10] + "..." if os.getenv("MORALIS_API_KEY") else None)
print("SOLANA_RPC:", os.getenv("SOLANA_RPC"))

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
    level=logging.DEBUG,  # ← SHOW EVERYTHING (pair fetch, RPC calls, checks)
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
REFERRAL_COMMISSION = 0.20                     # 20%
ETHERSCAN_KEY = os.getenv("ETHERSCAN_KEY", "")

DATA_FILE = Path("data.json")
SAVE_INTERVAL = 30
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens"
MORALIS_URL = "https://solana-gateway.moralis.io/token/mainnet/exchange/pumpfun/new"
JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP  = "https://quote-api.jup.ag/v6/swap"

# Thresholds
MIN_LIQUIDITY = 75
MIN_FDVS_SNIPE = 2500
MAX_VOL_SNIPE = 80
MIN_VOL_CONFIRM = 250
MIN_FDVS_CONFIRM = 8000
MIN_VOL_PUMP = 700
MIN_WHALE_USD = 1200

# --------------------------------------------------------------------------- #
#                               MARKDOWN ESCAPER
# --------------------------------------------------------------------------- #
def md(text: str) -> str:
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
                u.setdefault("referrer", None)
                u.setdefault("referrals", [])
                u.setdefault("commissions_earned", 0.0)
                u.setdefault("referral_stats", {"joins": 0, "paid_subs": 0})
                u.setdefault("username", f"user{u.get('id', '')}")
            return raw
        except Exception as e:
            log.error(f"Load error: {e}")
    return {"users": {}, "token_state": {}, "revenue": 0.0}

data = load_data()
users = data.setdefault("users", {})
token_state = data.setdefault("token_state", {})
data["revenue"] = data.get("revenue", 0.0)

# in-memory only
seen: dict[str, float] = {}                     # mint → timestamp
save_lock = asyncio.Lock()

def save_data(data):
    try:
        saveable = data.copy()
        saveable.pop("seen", None)
        for mint, state in saveable.get("token_state", {}).items():
            if "sent" in state:
                state["sent"] = list(state["sent"])
        for u in saveable.get("users", {}).values():
            if "referrals" in u:
                u["referrals"] = list(u["referrals"])
        DATA_FILE.write_text(json.dumps(saveable, indent=2))
    except Exception as e:
        log.error(f"Save error: {e}")

# --------------------------------------------------------------------------- #
#                               HELPERS
# --------------------------------------------------------------------------- #
async def safe_send(app, chat_id, text, reply_markup=None):
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except Exception:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except Exception as e2:
            log.error(f"Send failed: {e2}")

def pump_url(ca): return f"https://pump.fun/{ca}"

def format_alert(sym, addr, liq, fdv, vol, level, extra=""):
    level_map = {"snipe": "SNIPE", "confirm": "CONFIRM", "pump": "PUMP", "whale": "WHALE"}
    e = level_map.get(level, level.upper())
    short = addr[:8] + "..." + addr[-6:]
    base = (
        f"*{e} ALERT* [PUMP]\n"
        f"`{sym[:20]}`\n"
        f"*CA:* `{short}`\n"
        f"Liq: \\${liq:,.0f} \\| FDV: \\${fdv:,.0f}\n"
        f"5m Vol: \\${vol:,.0f}\n"
        f"{extra}"
        f"[View]({pump_url(addr)})"
    )
    return md(base)

def get_referral_link(uid: int) -> str:
    username = users[uid].get("username", f"user{uid}")
    return f"https://t.me/{app.bot.username}?start=ref_{username}"

# --------------------------------------------------------------------------- #
#                               RUG CHECK
# --------------------------------------------------------------------------- #
async def is_rug_proof(mint: str, pair_addr: str, sess) -> tuple[bool, str]:
    try:
        # 1. Mint authority frozen?
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                   "params": [mint, {"encoding": "jsonParsed"}]}
        async with sess.post(SOLANA_RPC, json=payload, timeout=8) as r:
            if r.status != 200: return False, "RPC error"
            info = (await r.json()).get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {})
            if info.get("mintAuthority"): return False, "Mint not frozen"

        # 2. LP burned?
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
                   "params": [pair_addr]}
        async with sess.post(SOLANA_RPC, json=payload, timeout=8) as r:
            if r.status != 200: return False, "RPC error"
            top = (await r.json()).get("result", {}).get("value", [{}])[0]
            if top.get("address") != "dead111111111111111111111111111111111111111":
                return False, "LP not burned"

        # 3. Supply sanity
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
        async with sess.post(SOLANA_RPC, json=payload, timeout=8) as r:
            if r.status != 200: return False, "RPC error"
            total = float((await r.json()).get("result", {}).get("value", {}).get("uiAmount", 0) or 0)
            if total == 0: return False, "No supply"

        # 4. Dev hold <10%
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]}
        async with sess.post(SOLANA_RPC, json=payload, timeout=8) as r:
            if r.status != 200: return False, "RPC error"
            held = float((await r.json()).get("result", {}).get("value", [{}])[0].get("uiAmount", 0) or 0)
            if total > 0 and (held / total) > 0.10:
                return False, f"Dev holds {(held/total)*100:.1f}%"

        return True, "SAFE"
    except Exception as e:
        log.debug(f"Rug check error {mint[:8]}: {e}")
        return False, "Check error"

# --------------------------------------------------------------------------- #
#                               WHALE DETECT
# --------------------------------------------------------------------------- #
async def detect_large_buy(mint: str, sess) -> float:
    try:
        async with sess.get(f"{DEXSCREENER_TOKEN}/{mint}", timeout=10) as r:
            if r.status != 200: return 0
            data = await r.json()
            pair = next((p for p in data.get("pairs", []) if p["quoteToken"]["symbol"] == "SOL"), None)
            if not pair: return 0
            price = float(pair.get("priceUsd", 0) or 0)
            if price <= 0: return 0
            pair_addr = pair["pairAddress"]

        payload = {"jsonrpc": "2.0", "id": 1,
                   "method": "getSignaturesForAddress",
                   "params": [pair_addr, {"limit": 5}]}
        async with sess.post(SOLANA_RPC, json=payload, timeout=15) as r:
            sigs = (await r.json()).get("result", [])
            if not sigs: return 0

        largest = 0.0
        for sig in sigs:
            tx_payload = {"jsonrpc": "2.0", "id": 1,
                          "method": "getTransaction",
                          "params": [sig["signature"], {"encoding": "jsonParsed"}]}
            async with sess.post(SOLANA_RPC, json=tx_payload, timeout=15) as tx_r:
                tx_data = await tx_r.json()
                result = tx_data.get("result")
                if not result: continue
                pre = result["meta"].get("preTokenBalances", [])
                post = result["meta"].get("postTokenBalances", [])
                for bal in pre:
                    if bal.get("mint") != mint: continue
                    owner = bal.get("owner")
                    post_bal = next((p for p in post if p.get("mint") == mint and p.get("owner") == owner), None)
                    if not post_bal: continue
                    bought = bal["uiTokenAmount"].get("uiAmount", 0) - post_bal["uiTokenAmount"].get("uiAmount", 0)
                    if bought > 0:
                        usd = bought * price
                        if usd > largest: largest = usd
        return largest
    except: return 0

# --------------------------------------------------------------------------- #
#                               PAIR FETCH + RETRY
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#                               RPC PAIR DETECTION (BYPASS PUMP.FUN)
# --------------------------------------------------------------------------- #
async def get_pair_from_rpc(mint: str, sess) -> str | None:
    """Fetch pair address directly from Solana RPC (pump.fun program)"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun program ID
            {
                "encoding": "jsonParsed",
                "filters": [
                    {"dataSize": 165},
                    {"memcmp": {"offset": 32, "bytes": mint}}
                ]
            }
        ]
    }
    try:
        async with sess.post(SOLANA_RPC, json=payload, timeout=12) as r:
            if r.status != 200:
                return None
            result = await r.json()
            accounts = result.get("result", [])
            if accounts:
                pair_addr = accounts[0]["pubkey"]
                log.info(f"  → RPC PAIR FOUND: {mint[:8]} → {pair_addr[:8]}...")
                return pair_addr
    except Exception as e:
        log.debug(f"RPC pair fetch error {mint[:8]}: {e}")
    return None
    
# --------------------------------------------------------------------------- #
#                               PAIR FETCH: DEXSCREENER + RPC ONLY
# --------------------------------------------------------------------------- #
async def get_pair_address(mint: str, sess) -> str | None:
    # 1. Dexscreener (99% hit rate at 60s+)
    try:
        async with sess.get(f"{DEXSCREENER_TOKEN}/{mint}", timeout=10) as r:
            if r.status == 200:
                data = await r.json()
                pair = next((p for p in data.get("pairs", []) if p.get("dexId") == "pumpswap"), None)
                if pair:
                    log.info(f"  → PAIR: {mint[:8]} (Dexscreener)")
                    return pair["pairAddress"]
    except:
        pass

    # 2. RPC fallback
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getProgramAccounts",
            "params": [
                "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                {"encoding": "jsonParsed", "filters": [{"dataSize": 165}, {"memcmp": {"offset": 32, "bytes": mint}}]}
            ]
        }
        async with sess.post(SOLANA_RPC, json=payload, timeout=12) as r:
            if r.status == 200:
                result = await r.json()
                accounts = result.get("result", [])
                if accounts:
                    pair_addr = accounts[0]["pubkey"]
                    log.info(f"  → PAIR: {mint[:8]} (RPC)")
                    return pair_addr
    except:
        pass

    log.info(f"  → NO PAIR: {mint[:8]} (should not happen at 60s+)")
    return None
# --------------------------------------------------------------------------- #
#                               SCANNER
# --------------------------------------------------------------------------- #
async def premium_pump_scanner(app: Application):
    volume_hist = defaultdict(lambda: deque(maxlen=4))
    skip_counter = defaultdict(int)
    last_scan_time = 0

    async with aiohttp.ClientSession() as sess:
        while True:
            try:
                await asyncio.sleep(random.uniform(15, 25))  # 15-25s cycles

                # === DEXSCREENER POLLING: NEW PUMP.FUN TOKENS ===
                # Get top new pairs on pump.fun (sort by age)
                now = time.time()
                url = f"{DEXSCREENER_TOKEN}/search/?q=pumpfun&orderBy=pairAge&orderDir=asc&limit=50"
                log.info(f"Dexscreener scan (time: {now:.0f})")

                async with sess.get(url, timeout=15) as r:
                    if r.status != 200:
                        log.error(f"Dexscreener error: {r.status}")
                        await asyncio.sleep(30)
                        continue
                    data = await r.json()
                    new_pairs = data.get("pairs", [])[:50]

                log.info(f"Found {len(new_pairs)} new pump.fun pairs")
                if not new_pairs:
                    await asyncio.sleep(20)
                    continue

                # Clean seen (1h)
                old = [m for m, t in seen.items() if now - t > 3600]
                for m in old: del seen[m]

                for pair in new_pairs:
                    mint = pair.get("baseToken", {}).get("address")
                    if not mint or mint in seen:
                        continue

                    # === 60s+ AGE FILTER ===
                    pair_age_str = pair.get("pairAge", "0")
                    try:
                        pair_age = int(pair_age_str.split()[0]) if pair_age_str else 0  # e.g., "1m" → 1
                        if pair_age < 1:  # <1 min
                            skip_counter["too_new"] += 1
                            log.debug(f"  → SKIP: {mint[:8]} {pair_age_str} old (<1m)")
                            continue
                    except:
                        continue

                    seen[mint] = now

                    # === EXTRACT DATA FROM PAIR ===
                    sym = pair.get("baseToken", {}).get("symbol", "UNKNOWN")[:20]
                    fdv = float(pair.get("fdv", 0) or 0)
                    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                    vol_5m = float(pair.get("volume", {}).get("h6", 0) or 0) / 72  # Approx 5m from 6h

                    log.info(f"CHECK {sym} | FDV ${fdv:,.0f} | Vol ${vol_5m:,.0f} | Liq ${liq:,.0f}")

                    pair_addr = pair.get("pairAddress")
                    if not pair_addr:
                        skip_counter["no_pair"] += 1
                        continue

                    # === RUG CHECK ===
                    safe, reason = await is_rug_proof(mint, pair_addr, sess)
                    log.info(f"  → RUG: {'PASS' if safe else 'FAIL'} | {reason}")
                    if not safe:
                        skip_counter["rug"] += 1
                        continue

                    # === WHALE ===
                    whale = await detect_large_buy(mint, sess)
                    if whale >= MIN_WHALE_USD:
                        extra = f"**\\${whale:,.0f} WHALE BUY**\\n"
                        msg = format_alert(sym, mint, liq, fdv, vol_5m, "whale", extra)
                        kb = [[InlineKeyboardButton("BUY NOW", callback_data=f"askbuy_{mint}")]]
                        await broadcast(msg, InlineKeyboardMarkup(kb))
                        continue

                    # === SNIPE LOGIC ===
                    hist = volume_hist[mint]
                    hist.append(vol_5m)
                    level = None

                    if fdv >= MIN_FDVS_SNIPE and vol_5m <= MAX_VOL_SNIPE:
                        level = "snipe"
                    elif fdv >= MIN_FDVS_CONFIRM and vol_5m >= MIN_VOL_CONFIRM:
                        level = "confirm"
                    elif len(hist) >= 2 and vol_5m >= hist[-2] * 3.0 and vol_5m >= MIN_VOL_PUMP:
                        level = "pump"

                    if level:
                        state = token_state.setdefault(mint, {"sent": set()})
                        if level not in state["sent"]:
                            state["sent"].add(level)
                            token_state[mint] = state
                            kb = [[InlineKeyboardButton("BUY NOW", callback_data=f"askbuy_{mint}")]] if level == "snipe" else None
                            msg = format_alert(sym, mint, liq, fdv, vol_5m, level)
                            await broadcast(msg, InlineKeyboardMarkup(kb) if kb else None)

                log.info(f"Scanner round | Skips: {dict(skip_counter)} | Seen: {len(seen)}")
                skip_counter.clear()

            except Exception as e:
                log.exception(f"Scanner crashed: {e}")
                await asyncio.sleep(20)

# --------------------------------------------------------------------------- #
#                               REFERRAL & PAY
# --------------------------------------------------------------------------- #
def attribute_referral(new_uid: int, code: str):
    if not code or not code.startswith("ref_"): return
    ref_username = code.split("_", 1)[1]
    referrer_uid = next((uid for uid, u in users.items() if u.get("username") == ref_username), None)
    if referrer_uid and referrer_uid != new_uid:
        users[new_uid]["referrer"] = referrer_uid
        users[referrer_uid]["referrals"].append(new_uid)
        users[referrer_uid]["referral_stats"]["joins"] += 1
        log.info(f"Referral: {new_uid} → {referrer_uid}")

def track_commission(paid_uid: int):
    referrer = users[paid_uid].get("referrer")
    if referrer:
        comm = PRICE_PREMIUM * REFERRAL_COMMISSION
        users[referrer]["commissions_earned"] += comm
        users[referrer]["referral_stats"]["paid_subs"] += 1
        data["revenue"] += PRICE_PREMIUM

# --------------------------------------------------------------------------- #
#                               COMMANDS
# --------------------------------------------------------------------------- #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    username = update.effective_user.username or f"user{uid}"

    if uid not in users:
        users[uid] = {
            "free_alerts": 3, "paid": False, "chat_id": chat_id, "wallet": None,
            "pending_buy": None, "referrer": None, "referrals": [], "commissions_earned": 0.0,
            "referral_stats": {"joins": 0, "paid_subs": 0}, "username": username
        }
    users[uid]["chat_id"] = chat_id
    users[uid]["username"] = username

    if ctx.args:
        attribute_referral(uid, ctx.args[0])

    await update.message.reply_text(
        "<b>ONION PREMIUM</b>\n\n"
        "3 free SNIPE alerts\n"
        f"Premium: <code>${PRICE_PREMIUM}/mo</code>\n"
        f"Pay: <code>{WALLET_BSC}</code>\n"
        "<code>/pay TXID</code> | <code>/wallet YOUR_SOL</code>\n",
        parse_mode="HTML"
    )

async def refer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in users:
        await update.message.reply_text("Use /start first.")
        return
    link = get_referral_link(uid)
    stats = users[uid]["referral_stats"]
    earned = users[uid]["commissions_earned"]
    msg = f"*YOUR LINK*\\n[Share]({link})\\n\\n*STATS*\\nJoins: {stats['joins']}\\nPaid: {stats['paid_subs']}\\nEarned: \\${earned:.2f}"
    await update.message.reply_text(md(msg), parse_mode="MarkdownV2", disable_web_page_preview=True)

# ---- PAYMENT VERIFICATION (BSC USDT) ----
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"

async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /pay TXID")
        return
    txid = ctx.args[0]
    uid = update.effective_user.id

    url = (
        f"https://api.bscscan.com/api?module=account&action=tokentx"
        f"&contractaddress={USDT_CONTRACT}&address={WALLET_BSC}"
        f"&startblock=0&endblock=99999999&sort=desc&apikey={ETHERSCAN_KEY}"
    )
    try:
        resp = requests.get(url, timeout=12).json()
        if resp.get("status") != "1":
            await update.message.reply_text("BscScan error.")
            return

        for tx in resp.get("result", []):
            if tx.get("hash").lower() != txid.lower():
                continue
            value = float(tx.get("value", 0)) / 1e18
            if tx.get("tokenSymbol") == "USDT" and value >= PRICE_PREMIUM:
                was_paid = users[uid].get("paid", False)
                users[uid]["paid"] = True
                users[uid]["paid_until"] = (datetime.utcnow() + timedelta(days=30)).isoformat()
                users[uid]["free_alerts"] = 0
                if not was_paid:
                    track_commission(uid)
                await update.message.reply_text(md("*PREMIUM ACTIVATED*"), parse_mode="MarkdownV2")
                return
        await update.message.reply_text("Invalid TX / amount.")
    except Exception as e:
        log.error(f"Pay verify error: {e}")
        await update.message.reply_text("Check failed.")

async def wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not users[uid].get("paid"):
        await update.message.reply_text("Premium only.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /wallet <sol_address>")
        return
    addr = ctx.args[0].strip()
    if len(addr) < 32:
        await update.message.reply_text("Invalid address.")
        return
    users[uid]["wallet"] = addr
    await update.message.reply_text(md(f"Wallet set: `{addr[:8]}...{addr[-6:]}`"), parse_mode="MarkdownV2")

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        msg = f"*ADMIN*\\nUsers: {len(users)}\\nPremium: {sum(1 for u in users.values() if u.get('paid'))}\\nRev: \\${data['revenue']:.2f}"
    else:
        if uid not in users: return
        s = users[uid]["referral_stats"]
        msg = f"*STATS*\\nJoins: {s['joins']}\\nPaid: {s['paid_subs']}\\nEarned: \\${users[uid]['commissions_earned']:.2f}"
    await update.message.reply_text(md(msg), parse_mode="MarkdownV2")

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if users[uid].get("pending_buy"):
        del users[uid]["pending_buy"]
        await update.message.reply_text(md("Cancelled."))

# --------------------------------------------------------------------------- #
#                               AUTO-BUY (Jupiter → Phantom deep-link)
# --------------------------------------------------------------------------- #
async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("askbuy_"): return
    mint = query.data.split("_", 1)[1]
    uid = query.from_user.id
    if not users[uid].get("paid") or not users[uid].get("wallet"):
        await query.edit_message_text(md("Set wallet first: /wallet <addr>"))
        return
    users[uid]["pending_buy"] = {"mint": mint, "time": time.time()}
    await query.edit_message_text(md(f"Enter \\$USD amount (e.g. 50):\\n`{mint[:8]}...`\\n/cancel"))

async def handle_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pending = users[uid].get("pending_buy")
    if not pending or time.time() - pending["time"] > 60:
        if pending: del users[uid]["pending_buy"]
        return
    try:
        usd = float(update.message.text.strip())
        if usd < 1: raise ValueError
    except:
        await update.message.reply_text(md("Enter a valid number"))
        return

    mint = pending["mint"]
    del users[uid]["pending_buy"]

    async with aiohttp.ClientSession() as sess:
        # SOL price
        async with sess.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd") as r:
            sol_price = (await r.json())["solana"]["usd"]
        sol_amount = usd / sol_price

        # Quote
        params = {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": mint,
            "amount": int(sol_amount * 1e9),
            "slippageBps": 100,
            "feeBps": 100
        }
        async with sess.get(JUP_QUOTE, params=params, timeout=12) as r:
            quote = await r.json()

        # Swap transaction
        payload = {
            "quoteResponse": quote,
            "userPublicKey": users[uid]["wallet"],
            "wrapAndUnwrapSol": True,
            "feeAccount": FEE_WALLET,
            "prioritizationFeeLamports": "auto"
        }
        async with sess.post(JUP_SWAP, json=payload, timeout=12) as r:
            swap_resp = await r.json()

        tx_b64 = swap_resp.get("swapTransaction")
        if not tx_b64:
            await update.message.reply_text(md("Swap failed – try again later."))
            return

        deep_link = f"https://phantom.app/ul/v1/signAndSendTransaction?tx={quote(tx_b64)}&cluster=mainnet-beta"
        await update.message.reply_text(
            md(f"*BUY \\${usd}*\\n"
               f"CA: `{mint[:8]}...{mint[-6:]}`\\n"
               f"[Confirm in Phantom]({deep_link})"),
            parse_mode="MarkdownV2",
            disable_web_page_preview=True
        )

# --------------------------------------------------------------------------- #
#                               BROADCAST (free-alert enforcement)
# --------------------------------------------------------------------------- #
async def broadcast(msg, reply_markup=None):
    async with save_lock:
        for uid, u in list(users.items()):
            chat_id = u.get("chat_id")
            if not chat_id: continue

            paid = u.get("paid", False)
            free_left = u.get("free_alerts", 0)

            if paid or free_left > 0:
                await safe_send(app, chat_id, msg, reply_markup)
                if not paid:
                    u["free_alerts"] = free_left - 1
            # else: silently skip

# --------------------------------------------------------------------------- #
#                               AUTO-SAVE
# --------------------------------------------------------------------------- #
async def auto_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        async with save_lock:
            save_data(data)

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
    app.add_handler(CommandHandler("refer", refer))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount))

    await app.initialize()
    await app.start()

    # START SCANNER & AUTO-SAVE
    asyncio.create_task(premium_pump_scanner(app))
    asyncio.create_task(auto_save())

    log.info("ONION BOT LIVE @alwaysgamble | NIGERIA READY")
    await app.updater.start_polling()
    await asyncio.Event().wait()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
