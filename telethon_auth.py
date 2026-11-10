import os
import sys
import json
import asyncio
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE = os.getenv("TELEGRAM_PHONE", "")
SESSION_PATH = os.path.join(os.path.dirname(__file__), "telethon_user")
AUTH_JSON = os.path.join(os.path.dirname(__file__), "telethon_auth.json")

async def send_code():
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    try:
        res = await client.send_code_request(PHONE)
        with open(AUTH_JSON, "w", encoding="utf-8") as f:
            json.dump({"phone_code_hash": res.phone_code_hash, "phone": PHONE}, f)
        print(f"CODE_SENT: Successfully requested login code for {PHONE}.")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        await client.disconnect()

async def sign_in(code, password=None):
    if not os.path.exists(AUTH_JSON):
        print("ERROR: No pending code request found. Run --send-code first.")
        return
    with open(AUTH_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    phone_code_hash = data.get("phone_code_hash")
    phone = data.get("phone", PHONE)

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    try:
        if password:
            try:
                await client.sign_in(password=password)
            except Exception:
                try:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                except SessionPasswordNeededError:
                    await client.sign_in(password=password)
        else:
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                print("2FA_REQUIRED: Two-step verification password is required.")
                return

        me = await client.get_me()
        print(f"AUTHORIZED: Logged in as {me.first_name} (ID: {me.id})")
        if os.path.exists(AUTH_JSON):
            os.remove(AUTH_JSON)
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        await client.disconnect()

async def check_status():
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    try:
        authorized = await client.is_user_authorized()
        if authorized:
            me = await client.get_me()
            print(f"AUTHORIZED: {me.first_name} (ID: {me.id})")
        else:
            print("NOT_AUTHORIZED")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--send-code":
        asyncio.run(send_code())
    elif len(sys.argv) > 2 and sys.argv[1] == "--sign-in":
        code = sys.argv[2]
        pwd = sys.argv[3] if len(sys.argv) > 3 else None
        asyncio.run(sign_in(code, pwd))
    elif len(sys.argv) > 1 and sys.argv[1] == "--status":
        asyncio.run(check_status())
    else:
        print("Usage: python telethon_auth.py [--send-code | --sign-in <code> [password] | --status]")
