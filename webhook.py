# webhook.py
import os
import json
import logging
from fastapi import FastAPI, Request, HTTPException
from web3 import Web3
from telegram import Bot
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")
app = FastAPI()

BSC_RPC = os.getenv("BSC_RPC")
USDT_CONTRACT = os.getenv("USDT_CONTRACT")
WALLET = os.getenv("USDT_BSC_WALLET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = "data.json"
REQUIRED_USDT = 29.99 * 1e6

w3 = Web3(Web3.HTTPProvider(BSC_RPC))
usdt_abi = [{"name":"Transfer","inputs":[{"type":"address","name":"from"},{"type":"address","name":"to"},{"type":"uint256","name":"value"}],"type":"event"}]
contract = w3.eth.contract(address=USDT_CONTRACT, abi=usdt_abi)

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"users": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

@app.post("/usdt-webhook")
async def usdt_webhook(request: Request):
    if request.headers.get("X-Secret") != WEBHOOK_SECRET:
        raise HTTPException(403)
    payload = await request.json()
    tx_hash = payload.get("hash")
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        logs = contract.events.Transfer().process_receipt(receipt)
        for log in logs:
            if log["args"]["to"].lower() == WALLET.lower() and log["args"]["value"] >= REQUIRED_USDT:
                data = load_data()
                for uid, u in data["users"].items():
                    if u.get("bsc_wallet", "").lower() == log["args"]["from"].lower():
                        u["paid"] = True
                        u["free_alerts"] = 999
                        save_data(data)
                        bot = Bot(BOT_TOKEN)
                        await bot.send_message(u["chat_id"], "<b>Payment Received!</b>\nPremium activated!", parse_mode="HTML")
                        return {"status": "activated"}
        return {"status": "ignored"}
    except Exception as e:
        log.error(f"Error: {e}")
        return {"status": "error"}
