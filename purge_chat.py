import os
import re
import sys
import json
import asyncio
from telethon import TelegramClient

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_PATH = os.path.join(os.path.dirname(__file__), "telethon_user")
BOT_USERNAME = os.getenv("BOT_USERNAME", "ThePervertedBrotherBot")

async def purge_chat():
    if "--force" not in sys.argv:
        print("SAFETY WARNING: purge_chat permanently removes bot messages. Pass --force to execute.")
        return
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: User is not authorized.")
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"Logged in as {me.first_name} ({me.id})")
    
    bot_entity = await client.get_entity(BOT_USERNAME)
    print(f"Scanning messages to purge in chat with @{BOT_USERNAME}...")

    msgs_to_delete = []
    link_msgs = []

    async for msg in client.iter_messages(bot_entity):
        text = msg.message or ""
        has_link = bool(re.search(r'https?://\S+', text))

        if msg.sender_id == bot_entity.id:
            # Delete all bot messages
            msgs_to_delete.append(msg.id)
        elif not has_link:
            # Delete user messages that do NOT have any links (e.g. /start, /clean, /retry, chatter)
            msgs_to_delete.append(msg.id)
        else:
            # Keep user link messages
            link_msgs.append(msg.id)

    print(f"Found {len(msgs_to_delete)} messages to delete (bot responses + non-link commands).")
    print(f"Preserving {len(link_msgs)} link messages.")

    # Delete in batches of 100
    total_deleted = 0
    for i in range(0, len(msgs_to_delete), 100):
        batch = msgs_to_delete[i:i+100]
        try:
            await client.delete_messages(bot_entity, batch, revoke=True)
            total_deleted += len(batch)
            print(f"Deleted {total_deleted}/{len(msgs_to_delete)} messages...")
            await asyncio.sleep(1.0)
        except Exception as e:
            print(f"Error deleting batch: {e}")
            await asyncio.sleep(3.0)

    print("Purge completed! Chat now contains exclusively your original link messages.")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(purge_chat())
