import os
import asyncio
import subprocess
import yt_dlp
import logging
import requests
import re
import time
import uuid
from dotenv import load_dotenv
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Ensure Playwright browser is installed at runtime
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
print("Checking Playwright browsers...")
os.system("python -m playwright install chromium")
print("Playwright check complete.")

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

    def _fallback_twitter(url):
        import os
        from playwright.sync_api import sync_playwright
        import threading
        video_url = None
        title_text = None
        
        auth = os.environ.get('TWITTER_AUTH_TOKEN', '627b310f25b01fef273fe12963c2c73a805d6ce0')
        ct0 = os.environ.get('TWITTER_CT0', '33d2d6a03a4d25786d15841355e8d12e26a64a30938c0634682d6f2fa7a36ca12be7106f05ccb58abf26df5d7e49a85570e2aaf2ae9dca30a0001186241294a3d5b7a88cfff2482cf2bbbdb28023f667')

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            context.add_cookies([
                {'name': 'auth_token', 'value': auth, 'domain': '.twitter.com', 'path': '/'},
                {'name': 'ct0', 'value': ct0, 'domain': '.twitter.com', 'path': '/'},
                {'name': 'auth_token', 'value': auth, 'domain': '.x.com', 'path': '/'},
                {'name': 'ct0', 'value': ct0, 'domain': '.x.com', 'path': '/'}
            ])
            page = context.new_page()

            found_event = threading.Event()
            
            def handle_response(response):
                nonlocal video_url, title_text
                url_str = response.url
                if 'video.twimg.com' in url_str and (url_str.endswith('.mp4') or url_str.endswith('.m3u8') or '.mp4?' in url_str or '.m3u8?' in url_str):
                    if not video_url or ('.m3u8' in url_str and '.mp4' in video_url):
                        video_url = url_str
                    found_event.set()
                    
                if 'graphql' in url_str and ('TweetDetail' in url_str or 'TweetResultByRestId' in url_str):
                    if response.status == 200:
                        try:
                            data = response.json()
                            tweet = data['data']['tweetResult']['result']
                            if 'tweet' in tweet: tweet = tweet['tweet']
                            if 'legacy' in tweet:
                                title_text = tweet['legacy'].get('full_text')
                        except Exception:
                            pass
            
            page.on("response", handle_response)
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=15000)
                # Try to click the "View" button for sensitive content if it exists
                try:
                    page.locator("span", has_text="View").first.click(timeout=3000)
                except Exception:
                    pass
                found_event.wait(timeout=10)
                
                # If still not found, check if there's a restriction message
                if not video_url:
                    body_text = page.locator("body").inner_text()
                    if "Due to local regulations" in body_text:
                        logger.error("Twitter Region Blocked.")
                        title_text = "ERROR_REGION_BLOCKED"
            except Exception as e:
                logger.error(f"Playwright error: {e}")
            finally:
                browser.close()
                
        return video_url, title_text

    def _download():
        nonlocal title
        target_url = url
        if 'reddit.com' in url or 'redd.it' in url:
            fallback_files, extracted_url = _fallback_reddit(url)
            if fallback_files:
                return fallback_files
            if extracted_url:
                target_url = extracted_url
        elif 'twitter.com' in url or 'x.com' in url:
            extracted_url, extracted_title = _fallback_twitter(url)
            if extracted_title == "ERROR_REGION_BLOCKED":
                raise Exception("This specific video is blocked due to local regulations (regional restriction on Twitter).")
            if extracted_url:
                target_url = extracted_url
            if extracted_title:
                title = extracted_title

        ydl_opts = {
            'outtmpl': 'downloads/%(extractor)s_%(id)s.%(ext)s',
            'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best/bv*+ba/b',
            'merge_output_format': 'mp4',
            'postprocessor_args': {'merger': ['-c', 'copy']},
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

def get_video_dimensions(filepath):
    """Read actual video width/height via ffprobe so Telegram doesn't guess wrong."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=p=0', filepath],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(',')
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None, None

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
                    w, h = get_video_dimensions(f)
                    group.append(InputMediaVideo(handle, caption=cap, parse_mode=pm, width=w, height=h, supports_streaming=True))
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
                            w, h = get_video_dimensions(f)
                            await update.message.reply_video(video=doc, caption=caption, parse_mode='HTML', width=w, height=h, supports_streaming=True)
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

async def adminmisuse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0] == "69420":
        context.user_data['awaiting_pfp'] = True
        await update.message.reply_text("Admin mode unlocked. Send me a photo to use as my new profile picture.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_pfp'):
        photo = update.message.photo[-1]
        file = await photo.get_file()
        os.makedirs('downloads', exist_ok=True)
        photo_path = f"downloads/pfp_{uuid.uuid4().hex}.jpg"
        await file.download_to_drive(photo_path)
        
        try:
            with open(photo_path, 'rb') as f:
                import json
                payload = {
                    'photo': json.dumps({"type": "static", "photo": "attach://pfp_file"})
                }
                files = {
                    'pfp_file': ('profile.jpg', f, 'image/jpeg')
                }
                res = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/setMyProfilePhoto",
                    data=payload,
                    files=files
                )
            if res.status_code == 200 and res.json().get('ok'):
                await update.message.reply_text("✅ Profile picture updated successfully.")
            else:
                await update.message.reply_text(f"❌ Failed to update profile picture: {res.text}")
        except Exception as e:
            logger.error(f"Error setting pfp: {e}")
            await update.message.reply_text("❌ Error setting profile picture.")
        finally:
            if os.path.exists(photo_path):
                os.remove(photo_path)
            context.user_data['awaiting_pfp'] = False

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

from aiohttp import web

async def main():
    if not TOKEN or TOKEN == 'your_bot_token_here':
        logger.error("Please set TELEGRAM_BOT_TOKEN")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("adminmisuse", adminmisuse))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Cleanup orphan files every 10 minutes
    application.job_queue.run_repeating(cleanup_job, interval=600, first=600)

    # Initialize PTB
    await application.initialize()
    await application.start()
    
    # Set Telegram Webhook to point to Render
    await application.bot.set_webhook(url=f"{RENDER_URL}/webhook")

    async def health_check(request):
        return web.Response(text="Bot is running!")

    async def telegram_webhook(request):
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.update_queue.put(update)
            return web.Response()
        except Exception as e:
            logger.error(f"Error handling webhook: {e}")
            return web.Response(status=500)

    # Setup aiohttp web server
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_post("/webhook", telegram_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    logger.info(f"Webhook server started on port {port}. Health check on /")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())
