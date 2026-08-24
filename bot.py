import os
import asyncio
import yt_dlp
import logging
import requests
import uuid
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! Send me a link from Reddit (RedGifs), Twitter, or Instagram and I'll download the media for you."
    )

async def download_media(url: str):
    def _fallback_reddit_image(url):
        try:
            s = requests.Session()
            s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            s.get('https://old.reddit.com/') # Get cookie to bypass 403 on .json
            
            res = s.get(url, allow_redirects=True)
            full_url = res.url.split('?')[0]
            if not full_url.endswith('/'):
                full_url += '/'
            json_url = full_url + '.json'
            
            res = s.get(json_url)
            if res.status_code != 200:
                return []
            
            data = res.json()
            post = data[0]['data']['children'][0]['data']
            
            if post.get('is_video'):
                return [] # Let yt-dlp handle videos
                
            media_urls = []
            if 'gallery_data' in post:
                media_dict = post.get('media_metadata', {})
                for item in post['gallery_data'].get('items', []):
                    media_id = item['media_id']
                    if media_id in media_dict:
                        s_info = media_dict[media_id]['s']
                        if 'mp4' in s_info:
                            media_urls.append((s_info['mp4'].replace('&amp;', '&'), 'mp4'))
                        elif 'u' in s_info:
                            media_urls.append((s_info['u'].replace('&amp;', '&'), 'jpg'))
                        elif 'gif' in s_info:
                            media_urls.append((s_info['gif'].replace('&amp;', '&'), 'gif'))
            elif 'url_overridden_by_dest' in post:
                m_url = post['url_overridden_by_dest']
                if m_url.endswith(('.jpg', '.jpeg', '.png', '.gif')) or 'i.redd.it' in m_url:
                    ext = m_url.split('.')[-1].split('?')[0]
                    if len(ext) > 4: ext = 'jpg'
                    media_urls.append((m_url, ext))
                    
            files = []
            os.makedirs('downloads', exist_ok=True)
            for m_url, ext in media_urls:
                m_res = s.get(m_url)
                if m_res.status_code == 200:
                    filename = f"downloads/reddit_{uuid.uuid4().hex}.{ext}"
                    with open(filename, 'wb') as f:
                        f.write(m_res.content)
                    files.append(filename)
            return files
        except Exception as e:
            logger.error(f"Fallback reddit failed: {e}")
            return []

    def _fallback_image(url):
        try:
            ydl_opts = {'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
            
            media_urls = []
            if info.get('_type') == 'playlist' and 'entries' in info:
                for entry in info.get('entries', []):
                    if entry and entry.get('url'):
                        media_urls.append(entry['url'])
            elif info.get('url'):
                media_urls.append(info['url'])
                
            files = []
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            os.makedirs('downloads', exist_ok=True)
            for m_url in media_urls:
                if 'i.redd.it' in m_url or m_url.split('?')[0].endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    m_res = requests.get(m_url, headers=headers)
                    if m_res.status_code == 200:
                        ext = m_url.split('.')[-1].split('?')[0]
                        if len(ext) > 4: ext = 'jpg'
                        filename = f"downloads/fallback_{uuid.uuid4().hex}.{ext}"
                        with open(filename, 'wb') as f:
                            f.write(m_res.content)
                        files.append(filename)
            return files
        except Exception as e:
            logger.error(f"Fallback failed: {e}")
            return []

    def _download():
        if 'reddit.com' in url or 'redd.it' in url:
            fallback_files = _fallback_reddit_image(url)
            if fallback_files:
                return fallback_files
                
        ydl_opts = {
            'outtmpl': 'downloads/%(extractor)s_%(id)s.%(ext)s',
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'noplaylist': False,
            'quiet': True,
            'no_warnings': True,
        }
        os.makedirs('downloads', exist_ok=True)
        files = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if 'entries' in info:
                    for entry in info['entries']:
                        if entry:
                            files.append(ydl.prepare_filename(entry))
                else:
                    files.append(ydl.prepare_filename(info))
            return files
        except yt_dlp.utils.DownloadError as e:
            fallback_files = _fallback_image(url)
            if fallback_files:
                return fallback_files
            raise e
        
    return await asyncio.to_thread(_download)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith('http'):
        return
        
    reply_msg = await update.message.reply_text("⏳ Processing link...")
    
    try:
        files = await download_media(url)
        if not files:
            await reply_msg.edit_text("❌ Couldn't find any media to download.")
            return

        if len(files) == 1:
            f = files[0]
            try:
                if os.path.exists(f) and os.path.getsize(f) > 0:
                    with open(f, 'rb') as doc:
                        if os.path.getsize(f) > 50 * 1024 * 1024:
                            await update.message.reply_text("❌ File is larger than Telegram's 50MB bot limit.")
                        elif f.endswith(('.mp4', '.webm', '.mkv', '.gif')):
                            await update.message.reply_video(video=doc)
                        elif f.endswith(('.jpg', '.png', '.jpeg')):
                            await update.message.reply_photo(photo=doc)
                        else:
                            await update.message.reply_document(document=doc)
            except Exception as e:
                logger.error(f"Failed to send file: {e}")
                await update.message.reply_text(f"❌ Failed to send file to Telegram.")
            finally:
                if os.path.exists(f):
                    os.remove(f)
            await reply_msg.edit_text("✅ Done!")
        else:
            unique_id = uuid.uuid4().hex
            context.bot_data[unique_id] = files
            keyboard = [
                [InlineKeyboardButton("Media", callback_data=f"send_media|{unique_id}")],
                [InlineKeyboardButton("File", callback_data=f"send_file|{unique_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await reply_msg.edit_text("Download album as media or file?", reply_markup=reply_markup)
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download Error: {e}")
        safe_error = str(e).replace('<', '&lt;').replace('>', '&gt;')
        await reply_msg.edit_text(f"❌ Could not download from this link.\n\n<b>Debug Report:</b>\n<pre>{safe_error}</pre>", parse_mode='HTML')
    except Exception as e:
        import traceback
        logger.error(f"Error processing link: {e}")
        tb = traceback.format_exc()[-1000:].replace('<', '&lt;').replace('>', '&gt;')
        await reply_msg.edit_text(f"❌ Error processing link.\n\n<b>Debug Report:</b>\n<pre>{tb}</pre>", parse_mode='HTML')

def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action, unique_id = query.data.split('|')
    files = context.bot_data.get(unique_id)
    
    if not files:
        await query.edit_message_text("❌ Files expired or not found.")
        return
        
    await query.edit_message_text("⏳ Uploading album...")
    
    try:
        if action == 'send_media':
            media_group = []
            for f in files:
                if f.endswith(('.mp4', '.webm', '.mkv', '.gif')):
                    media_group.append(InputMediaVideo(open(f, 'rb')))
                else:
                    media_group.append(InputMediaPhoto(open(f, 'rb')))
            
            for chunk in chunk_list(media_group, 10):
                await query.message.reply_media_group(media=chunk)
        else:
            document_group = []
            for f in files:
                document_group.append(InputMediaDocument(open(f, 'rb')))
            
            for chunk in chunk_list(document_group, 10):
                await query.message.reply_media_group(media=chunk)
                
        await query.edit_message_text("✅ Done!")
    except Exception as e:
        logger.error(f"Failed to send album: {e}")
        await query.edit_message_text(f"❌ Failed to send album.")
    finally:
        for f in files:
            if os.path.exists(f):
                os.remove(f)
        del context.bot_data[unique_id]

async def run_bot():
    if not TOKEN or TOKEN == 'your_bot_token_here':
        logger.error("Please set TELEGRAM_BOT_TOKEN in the .env file")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot is starting...")
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    logger.info("Bot is polling...")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

def main():
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
