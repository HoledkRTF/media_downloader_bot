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

async def extract_chat_links(delete_bot_msgs=False):
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: User is not authorized. Please run telethon_auth.py first.")
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"Connected as {me.first_name} ({me.id})")
    
    bot_entity = await client.get_entity(BOT_USERNAME)
    print(f"Scanning chat with @{BOT_USERNAME}...")

    all_links = []
    bot_msg_ids = []
    total_messages = 0

    # Fetch messages in chronological order (oldest to newest)
    async for msg in client.iter_messages(bot_entity, reverse=True):
        total_messages += 1
        
        # Check if the message was sent by the bot
        if msg.sender_id == bot_entity.id:
            bot_msg_ids.append(msg.id)
            continue

        # Extract links from user messages
        text = msg.message or ""
        urls = re.findall(r'(https?://\S+)', text)
        for u in urls:
            # Clean trailing punctuation
            u_clean = u.rstrip('.,;!?)>"\'')
            all_links.append({
                "url": u_clean,
                "msg_id": msg.id,
                "date": msg.date.isoformat(),
                "text": text
            })

    print(f"Scanned {total_messages} messages:")
    print(f"- Found {len(all_links)} links from your messages")
    print(f"- Found {len(bot_msg_ids)} bot responses to clean up")

    # Optional: Delete all bot messages
    if delete_bot_msgs and bot_msg_ids:
        print(f"Deleting {len(bot_msg_ids)} bot messages...")
        # Telethon delete_messages accepts chunks of up to 100
        for i in range(0, len(bot_msg_ids), 100):
            chunk = bot_msg_ids[i:i+100]
            await client.delete_messages(bot_entity, chunk)
            await asyncio.sleep(0.5)
        print("Bot clutter successfully deleted from chat!")

    # Save found links
    output_path = os.path.join(os.path.dirname(__file__), "extracted_links.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_links, f, indent=2)
    print(f"Saved {len(all_links)} links to extracted_links.json")

    await client.disconnect()
    return all_links

if __name__ == "__main__":
    delete_bot = "--delete-bot" in sys.argv
    if not delete_bot:
        print("Note: Running in read-only mode. Bot messages will NOT be deleted. Pass --delete-bot to enable deletion.")
    asyncio.run(extract_chat_links(delete_bot_msgs=delete_bot))
