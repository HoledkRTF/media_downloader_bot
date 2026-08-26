import os
import asyncio
import yt_dlp
import logging
import requests
import re
import time
import uuid
from dotenv import load_dotenv
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://media-downloader-bot-2ia1.onrender.com")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a link from Reddit, RedGifs, Twitter, or Instagram and I'll download the media for you.\n\n"
        "Add -f after the link to receive files instead of media."
    )

async def download_media(url: str):
    """Returns (files, title, original_url)"""
    title = None
    original_url = url

    def _fallback_reddit(url):
        nonlocal title
        try:
            s = requests.Session()
            s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            s.get('https://old.reddit.com/')

            res = s.get(url, allow_redirects=True)
            full_url = res.url.split('?')[0]
            if not full_url.endswith('/'):
                full_url += '/'
            json_url = full_url + '.json'

            res = s.get(json_url)
            if res.status_code != 200:
                return [], None

            data = res.json()
            post = data[0]['data']['children'][0]['data']
            if 'crosspost_parent_list' in post and post['crosspost_parent_list']:
                post = post['crosspost_parent_list'][0]

            title = post.get('title')

            if post.get('is_video'):
                return [], url

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
                else:
                    return [], m_url

            files = []
            os.makedirs('downloads', exist_ok=True)
            for m_url, ext in media_urls:
                m_res = s.get(m_url)
                if m_res.status_code == 200:
                    filename = f"downloads/reddit_{uuid.uuid4().hex}.{ext}"
                    with open(filename, 'wb') as f:
                        f.write(m_res.content)
                    files.append(filename)
            return files, None
        except Exception as e:
            logger.error(f"Fallback reddit failed: {e}")
            return [], None

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
        nonlocal title
        target_url = url
        if 'reddit.com' in url or 'redd.it' in url:
            fallback_files, extracted_url = _fallback_reddit(url)
            if fallback_files:
                return fallback_files
            if extracted_url:
                target_url = extracted_url

        ydl_opts = {
            'outtmpl': 'downloads/%(extractor)s_%(id)s.%(ext)s',
            'format': 'bv*+ba/b',
            'merge_output_format': 'mp4',
            'noplaylist': False,
            'quiet': True,
            'no_warnings': True,
        }
        os.makedirs('downloads', exist_ok=True)
        files = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_url, download=True)
                if not title and info.get('title'):
                    title = info['title']
                if 'entries' in info:
                    for entry in info['entries']:
                        if entry:
                            files.append(ydl.prepare_filename(entry))
                else:
                    files.append(ydl.prepare_filename(info))
            return files
        except yt_dlp.utils.DownloadError as e:
            fallback_files = _fallback_image(target_url)
            if fallback_files:
                return fallback_files
            raise e

    files = await asyncio.to_thread(_download)
    return files, title, original_url

def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

async def send_album(message, files, as_files, caption=None):
    """Send multiple files as media group or document group."""
    handles = []
    try:
        if as_files:
            group = []
            for i, f in enumerate(files):
                handle = open(f, 'rb')
                handles.append(handle)
                group.append(InputMediaDocument(handle, caption=caption if i == 0 else None, parse_mode='HTML' if caption else None))
            for chunk in chunk_list(group, 10):
                await message.reply_media_group(media=chunk)
        else:
            group = []
            for i, f in enumerate(files):
                handle = open(f, 'rb')
                handles.append(handle)
                cap = caption if i == 0 else None
                pm = 'HTML' if cap else None
                if f.endswith(('.mp4', '.webm', '.mkv', '.gif')):
                    group.append(InputMediaVideo(handle, caption=cap, parse_mode=pm))
                else:
                    group.append(InputMediaPhoto(handle, caption=cap, parse_mode=pm))
            for chunk in chunk_list(group, 10):
                await message.reply_media_group(media=chunk)
    finally:
        for h in handles:
            h.close()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return

    urls = re.findall(r'(https?://\S+)', text)
    if not urls:
        return

    url = urls[0]
    as_files = bool(re.search(r'\s-f\b', text))

    reply_msg = await update.message.reply_text("⏳ Processing link...")

    try:
        files, title, original_url = await download_media(url)
        if not files:
            await reply_msg.edit_text("❌ Couldn't find any media to download.")
            return

        # Build caption
        caption_parts = []
        if title:
            safe_title = title.replace('<', '&lt;').replace('>', '&gt;')
            caption_parts.append(safe_title)
        caption_parts.append(f'🔗 <a href="{original_url}">Link</a>')
        caption = "\n".join(caption_parts)

        if len(files) == 1:
            f = files[0]
            try:
                if os.path.exists(f) and os.path.getsize(f) > 0:
                    with open(f, 'rb') as doc:
                        if os.path.getsize(f) > 50 * 1024 * 1024:
                            await update.message.reply_text("❌ File is larger than Telegram's 50MB bot limit.")
                        elif as_files:
                            await update.message.reply_document(document=doc, caption=caption, parse_mode='HTML')
                        elif f.endswith(('.mp4', '.webm', '.mkv', '.gif')):
                            await update.message.reply_video(video=doc, caption=caption, parse_mode='HTML')
                        elif f.endswith(('.jpg', '.png', '.jpeg')):
                            await update.message.reply_photo(photo=doc, caption=caption, parse_mode='HTML')
                        else:
                            await update.message.reply_document(document=doc, caption=caption, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Failed to send file: {e}")
                await update.message.reply_text(f"❌ Failed to send file to Telegram.")
            finally:
                if os.path.exists(f):
                    os.remove(f)
            await reply_msg.edit_text("✅ Done!")
        else:
            try:
                await send_album(update.message, files, as_files, caption)
                await reply_msg.edit_text("✅ Done!")
            except Exception as e:
                logger.error(f"Failed to send album: {e}")
                await reply_msg.edit_text(f"❌ Failed to send album.")
            finally:
                for f in files:
                    if os.path.exists(f):
                        os.remove(f)
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download Error: {e}")
        safe_error = str(e).replace('<', '&lt;').replace('>', '&gt;')
        if len(safe_error) > 3500: safe_error = safe_error[:3500] + "... (truncated)"
        await reply_msg.edit_text(f"❌ Could not download from this link.\n\n<b>Debug Report:</b>\n<pre>{safe_error}</pre>", parse_mode='HTML')
    except Exception as e:
        import traceback
        logger.error(f"Error processing link: {e}")
        tb = traceback.format_exc().replace('<', '&lt;').replace('>', '&gt;')
        if len(tb) > 3500: tb = "... (truncated)\n" + tb[-3500:]
        await reply_msg.edit_text(f"❌ Error processing link.\n\n<b>Debug Report:</b>\n<pre>{tb}</pre>", parse_mode='HTML')

async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic cleanup of orphan files in downloads/"""
    if os.path.exists('downloads'):
        now = time.time()
        for f in os.listdir('downloads'):
            f_path = os.path.join('downloads', f)
            if os.path.isfile(f_path) and now - os.path.getmtime(f_path) > 600:
                try:
                    os.remove(f_path)
                    logger.info(f"Cleaned up orphan file {f_path}")
                except:
                    pass

def main():
    if not TOKEN or TOKEN == 'your_bot_token_here':
        logger.error("Please set TELEGRAM_BOT_TOKEN")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Cleanup orphan files every 10 minutes
    application.job_queue.run_repeating(cleanup_job, interval=600, first=600)

    port = int(os.environ.get('PORT', 10000))
    application.run_webhook(
        listen='0.0.0.0',
        port=port,
        url_path='webhook',
        webhook_url=f'{RENDER_URL}/webhook',
    )

if __name__ == "__main__":
    main()
