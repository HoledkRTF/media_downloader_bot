import os
import sys
import json
import asyncio
import logging
from telegram import Bot, ReplyParameters
from telegram.request import HTTPXRequest

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("batch_downloader")

# Import helpers from bot.py
import bot as bot_module

USER_ID = 8356075099

class MessageProxy:
    """Wraps telegram.Bot to simulate a Message object targeting a specific message_id."""
    def __init__(self, bot: Bot, chat_id: int, message_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.reply_to_message = None

    async def reply_text(self, text, **kwargs):
        kwargs.pop('quote', None)
        reply_params = ReplyParameters(message_id=self.message_id, allow_sending_without_reply=True)
        msg = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_parameters=reply_params,
            **kwargs
        )
        return MessageProxy(self.bot, self.chat_id, msg.message_id)

    async def edit_text(self, text, **kwargs):
        return await self.bot.edit_message_text(
            chat_id=self.chat_id,
            message_id=self.message_id,
            text=text,
            **kwargs
        )

    async def delete(self):
        try:
            return await self.bot.delete_message(
                chat_id=self.chat_id,
                message_id=self.message_id
            )
        except Exception:
            pass

    async def reply_photo(self, photo, **kwargs):
        kwargs.pop('quote', None)
        reply_params = ReplyParameters(message_id=self.message_id, allow_sending_without_reply=True)
        return await self.bot.send_photo(
            chat_id=self.chat_id,
            photo=photo,
            reply_parameters=reply_params,
            **kwargs
        )

    async def reply_video(self, video, **kwargs):
        kwargs.pop('quote', None)
        reply_params = ReplyParameters(message_id=self.message_id, allow_sending_without_reply=True)
        return await self.bot.send_video(
            chat_id=self.chat_id,
            video=video,
            reply_parameters=reply_params,
            **kwargs
        )

    async def reply_document(self, document, **kwargs):
        kwargs.pop('quote', None)
        reply_params = ReplyParameters(message_id=self.message_id, allow_sending_without_reply=True)
        return await self.bot.send_document(
            chat_id=self.chat_id,
            document=document,
            reply_parameters=reply_params,
            **kwargs
        )

    async def reply_media_group(self, media, **kwargs):
        kwargs.pop('quote', None)
        reply_params = ReplyParameters(message_id=self.message_id, allow_sending_without_reply=True)
        return await self.bot.send_media_group(
            chat_id=self.chat_id,
            media=media,
            reply_parameters=reply_params,
            **kwargs
        )

    async def reply_animation(self, animation, **kwargs):
        kwargs.pop('quote', None)
        reply_params = ReplyParameters(message_id=self.message_id, allow_sending_without_reply=True)
        return await self.bot.send_animation(
            chat_id=self.chat_id,
            animation=animation,
            reply_parameters=reply_params,
            **kwargs
        )

    async def reply_chat_action(self, action, **kwargs):
        try:
            return await self.bot.send_chat_action(chat_id=self.chat_id, action=action)
        except Exception:
            pass

async def run_batch(limit=None):
    extracted_path = os.path.join(os.path.dirname(__file__), "extracted_links.json")
    if not os.path.exists(extracted_path):
        logger.error("No extracted_links.json found.")
        return

    with open(extracted_path, "r", encoding="utf-8") as f:
        links_data = json.load(f)

    # Deduplicate unique URLs preserving original message_id
    seen_urls = set()
    unique_links = []
    for item in links_data:
        url = item['url']
        if url not in seen_urls:
            seen_urls.add(url)
            unique_links.append(item)

    logger.info(f"Loaded {len(unique_links)} unique links to process.")

    # Load progress
    progress_path = os.path.join(os.path.dirname(__file__), "batch_progress.json")
    processed = set()
    if os.path.exists(progress_path):
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                processed = set(json.load(f))
        except Exception:
            pass

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=60.0,
        read_timeout=300.0,
        write_timeout=300.0,
        pool_timeout=60.0
    )
    bot = Bot(token=bot_module.TOKEN, request=request)

    count = 0
    total = len(unique_links)
    for idx, item in enumerate(unique_links, 1):
        url = item['url']
        msg_id = item['msg_id']
        text = item.get('text', url)

        if url in processed:
            continue

        if limit and count >= limit:
            logger.info(f"Reached batch limit of {limit} items.")
            break

        count += 1
        logger.info(f"[{idx}/{total}] Processing: {url} (msg_id: {msg_id})")

        proxy = MessageProxy(bot, USER_ID, msg_id)
        try:
            success = await bot_module.process_single_link(proxy, url, text, USER_ID)
            logger.info(f"Result for {url}: {'SUCCESS' if success else 'FAILED'}")
        except Exception as e:
            logger.error(f"Error processing {url}: {e}")

        # Mark processed atomically
        processed.add(url)
        bot_module.atomic_json_dump(progress_path, list(processed), indent=2)

        # Rate limit safety delay
        await asyncio.sleep(2.0)

    logger.info("Batch processing round complete.")

if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    asyncio.run(run_batch(limit=limit_arg))
