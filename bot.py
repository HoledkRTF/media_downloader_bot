import os
import zipfile
import io
import asyncio
import subprocess
import yt_dlp
import logging
import requests
import re
import time
import uuid
from dotenv import load_dotenv
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

VERSION = "v1.7.0"

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO,
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://media-downloader-bot-2ia1.onrender.com")

import json

def get_wishlist(user_id):
    try:
        if os.path.exists('wishlist.json'):
            with open('wishlist.json', 'r') as f:
                data = json.load(f)
                return data.get(str(user_id), [])
    except Exception as e:
        logger.error(f"Error getting wishlist: {e}")
    return []

def save_wishlist(user_id, wishlist):
    try:
        data = {}
        if os.path.exists('wishlist.json'):
            with open('wishlist.json', 'r') as f:
                data = json.load(f)
        data[str(user_id)] = wishlist
        # ponytail: wishlist.json has no file locking, if throughput matters switch to sqlite
        with open('wishlist.json', 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error saving wishlist: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a link from Reddit, RedGifs, Twitter, or Instagram and I'll download the media for you.\n\n"
        "Commands:\n"
        "/manga <title> - Search & download manga chapters\n\n"
        "Add -f after a link to receive files instead of media."
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
        temp_dir = 'downloads'
        ydl_opts = {
            'format': 'best',
            'outtmpl': f'{temp_dir}/%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
        }
        
        # Add Instagram cookie if provided in .env to bypass the strict login wall
        ig_cookie = os.environ.get('INSTAGRAM_SESSION_ID')
        if ig_cookie and ('instagram.com' in url or 'ig.me' in url):
            with open('ig_cookies.txt', 'w') as f:
                f.write(f"# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2145916800\tsessionid\t{ig_cookie}\n")
            ydl_opts['cookiefile'] = 'ig_cookies.txt'

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)

            media_urls = []
            if info.get('_type') == 'playlist' and 'entries' in info:
                for entry in info.get('entries', []):
                    if entry:
                        if entry.get('url'):
                            media_urls.append(entry['url'])
                        elif entry.get('thumbnails'):
                            media_urls.append(entry['thumbnails'][-1]['url'])
            else:
                if info.get('url'):
                    media_urls.append(info['url'])
                elif info.get('thumbnails'):
                    media_urls.append(info['thumbnails'][-1]['url'])

            files = []
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            os.makedirs('downloads', exist_ok=True)
            for m_url in media_urls:
                # Removed strict extension check so Instagram image URLs work
                m_res = requests.get(m_url, headers=headers)
                if m_res.status_code == 200:
                    ext = m_url.split('.')[-1].split('?')[0]
                    if len(ext) > 4 or not ext: ext = 'jpg'
                    filename = f"downloads/fallback_{uuid.uuid4().hex}.{ext}"
                    with open(filename, 'wb') as f:
                        f.write(m_res.content)
                    files.append(filename)
            return files
        except Exception as e:
            logger.error(f"Fallback failed: {e}")
            return []

    def _fallback_twitter(url):
        video_url = None
        title_text = None
        
        try:
            # Extract tweet ID
            match = re.search(r'status/(\d+)', url)
            if not match:
                return None, None
                
            tweet_id = match.group(1)
            api_url = f"https://api.vxtwitter.com/Twitter/status/{tweet_id}"
            
            res = requests.get(api_url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data.get('hasMedia') and data.get('mediaURLs'):
                    # Find highest quality video or just take the first video URL
                    for media in data.get('media_extended', []):
                        if media.get('type') == 'video':
                            video_url = media.get('url')
                            break
                    if not video_url:
                        video_url = data['mediaURLs'][0]
                    title_text = data.get('text', '')
            elif res.status_code == 404:
                return None, "ERROR_REGION_BLOCKED"
                
        except Exception as e:
            logger.error(f"vxtwitter API error: {e}")
            
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
        
        # Add Instagram cookie if provided in .env to bypass the strict login wall
        ig_cookie = os.environ.get('INSTAGRAM_SESSION_ID')
        if ig_cookie and ('instagram.com' in url or 'ig.me' in url):
            with open('ig_cookies.txt', 'w') as f:
                f.write(f"# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2145916800\tsessionid\t{ig_cookie}\n")
            ydl_opts['cookiefile'] = 'ig_cookies.txt'
            
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

def upload_to_gofile(filepath):
    """Upload large file to gofile.io and return direct link to the download page."""
    try:
        r = requests.get('https://api.gofile.io/servers')
        if r.status_code == 200:
            server = r.json()['data']['servers'][0]['name']
            with open(filepath, 'rb') as f:
                r2 = requests.post(f'https://{server}.gofile.io/contents/uploadfile', files={'file': f})
            if r2.status_code == 200:
                return r2.json()['data']['downloadPage']
    except Exception as e:
        logger.error(f"Gofile upload error: {e}")
    return None

async def send_album(message, files, as_files, caption=None):
    """Send multiple files as media group or document group."""
    handles = []
    try:
        # Check files for size limit. Any file > 50MB gets sent to Gofile instead.
        large_files = []
        valid_files = []
        for f in files:
            if os.path.exists(f) and os.path.getsize(f) > 50 * 1024 * 1024:
                large_files.append(f)
            else:
                valid_files.append(f)
        
        # If there are large files, upload them to Gofile and notify the user
        for lf in large_files:
            await message.reply_text("⏳ A file in this album is over 50MB! Uploading the uncompressed original to Gofile...")
            cloud_link = upload_to_gofile(lf)
            if cloud_link:
                await message.reply_text(f"✅ One of the files was too large for Telegram, so I uploaded it here:\n\n🔗 {cloud_link}", disable_web_page_preview=True)
            else:
                await message.reply_text("❌ A file was larger than Telegram's 50MB limit and cloud upload failed.")
                
        if not valid_files:
            return

        if as_files:
            group = []
            for i, f in enumerate(valid_files):
                handle = open(f, 'rb')
                handles.append(handle)
                group.append(InputMediaDocument(handle, caption=caption if i == 0 else None, parse_mode='HTML' if caption else None))
            for chunk in chunk_list(group, 10):
                await message.reply_media_group(media=chunk)
        else:
            group = []
            for i, f in enumerate(valid_files):
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

    # Check for nhentai 5-6 digit code
    nh_match = re.fullmatch(r'(\d{5,6})', text.strip())
    if nh_match:
        code = nh_match.group(1)
        reply_msg = await update.message.reply_text("⏳ Fetching doujin info...")
        try:
            from curl_cffi import requests as c_requests
            from bs4 import BeautifulSoup
            r = await asyncio.to_thread(c_requests.get, f'https://nhentai.net/g/{code}', impersonate='chrome110', timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                cover_div = soup.find('div', id='cover')
                info_div = soup.find('div', id='info')
                
                if cover_div and cover_div.find('img') and info_div and info_div.find('h1'):
                    cover_url = cover_div.find('img')['src']
                    title = info_div.find('h1').text.strip()
                    tags = [a.find('span', class_='name').text for a in soup.find_all('a', class_='tag') if a.find('span', class_='name')]
                    
                    # Save to wishlist database
                    user_id = update.message.from_user.id
                    wishlist = get_wishlist(user_id)
                    # Remove if already exists to move to top
                    wishlist = [item for item in wishlist if item.get('code') != code]
                    wishlist.insert(0, {
                        'code': code,
                        'title': title,
                        'cover_url': cover_url,
                        'tags': tags
                    })
                    # Keep only last 100 to avoid KV limit
                    if len(wishlist) > 100: wishlist = wishlist[:100]
                    save_wishlist(user_id, wishlist)
                    
                    # Download cover
                    img_res = await asyncio.to_thread(c_requests.get, cover_url, impersonate='chrome110', timeout=10)
                    if img_res.status_code == 200:
                        os.makedirs('downloads', exist_ok=True)
                        cover_path = f"downloads/nh_{code}.jpg"
                        with open(cover_path, 'wb') as f:
                            f.write(img_res.content)
                            
                        caption = f"📖 <b>{title.replace('<', '&lt;').replace('>', '&gt;')}</b>\n🔗 <a href=\"https://nhentai.net/g/{code}\">nhentai.net/g/{code}</a>\n✅ <i>Saved to your /wh wishlist!</i>\n\n🤖 <i>{VERSION}</i>"
                        with open(cover_path, 'rb') as f:
                            await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
                            
                        os.remove(cover_path)
                        await reply_msg.delete()
                        return
            
            await reply_msg.edit_text("❌ Could not fetch info for that code. It might not exist.")
        except Exception as e:
            logger.error(f"Error fetching nhentai: {e}")
            await reply_msg.edit_text(f"❌ Error fetching doujin info. ({VERSION})")
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
                    if os.path.getsize(f) > 50 * 1024 * 1024:
                        await reply_msg.edit_text("⏳ File is over 50MB! Uploading the uncompressed original to Gofile...")
                        cloud_link = upload_to_gofile(f)
                        if cloud_link:
                            await reply_msg.delete()
                            await update.message.reply_text(f"{caption}\n\n✅ File was too large for Telegram, so I uploaded it here:\n\n🔗 {cloud_link}", disable_web_page_preview=True, parse_mode='HTML')
                        else:
                            await reply_msg.edit_text("❌ File is larger than Telegram's 50MB limit and cloud upload failed.")
                        if os.path.exists(f): os.remove(f)
                        return
                            
                    with open(f, 'rb') as doc:
                        if as_files:
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
                await reply_msg.edit_text(f"❌ Failed to send file to Telegram.")
                return
            finally:
                if os.path.exists(f):
                    os.remove(f)
            await reply_msg.edit_text(f"✅ Done! ({VERSION})")
        else:
            try:
                await send_album(update.message, files, as_files, caption)
                await reply_msg.edit_text(f"✅ Done! ({VERSION})")
            except Exception as e:
                logger.error(f"Failed to send album: {e}")
                await reply_msg.edit_text(f"❌ Failed to send album.")
            finally:
                for f in files:
                    if os.path.exists(f):
                        os.remove(f)
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download failed: {e}")
        if hasattr(e, 'message'):
            error_msg = e.message
        else:
            error_msg = str(e)
        
        # Truncate extremely long error messages
        if len(error_msg) > 800:
            error_msg = error_msg[:800] + "... [Truncated]"
            
        await reply_msg.edit_text(f"❌ Could not download from this link. ({VERSION})\n\n<b>Debug Report:</b>\n<pre>{error_msg}</pre>", parse_mode='HTML')
        return
    except Exception as e:
        import traceback
        logger.error(f"Error processing link: {e}\n{traceback.format_exc()}")
        tb = traceback.format_exc().replace('<', '&lt;').replace('>', '&gt;')
        if len(tb) > 3500: tb = "... (truncated)\n" + tb[-3500:]
        await reply_msg.edit_text(f"❌ Error processing link. ({VERSION})\n\n<b>Debug Report:</b>\n<pre>{tb}</pre>", parse_mode='HTML')

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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def wh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    wishlist = get_wishlist(user_id)
    
    if not wishlist:
        await update.message.reply_text("📭 Your wishlist is empty! Send me a 5 or 6 digit nhentai code to save one.")
        return
        
    page = 0
    keyboard = build_wh_keyboard(wishlist, page)
    await update.message.reply_text(
        f"📖 <b>Your Doujin Wishlist</b> (Page {page+1})\n<i>Select a title to view details:</i>",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

def build_wh_keyboard(wishlist, page):
    ITEMS_PER_PAGE = 10
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    current_items = wishlist[start:end]
    
    keyboard = []
    for item in current_items:
        title = item['title'][:40] + "..." if len(item['title']) > 40 else item['title']
        keyboard.append([InlineKeyboardButton(title, callback_data=f"wh_view_{item['code']}")])
        
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"wh_page_{page-1}"))
    if end < len(wishlist):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"wh_page_{page+1}"))
        
    if nav_buttons:
        keyboard.append(nav_buttons)
        
    return InlineKeyboardMarkup(keyboard)

async def wh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    
    wishlist = get_wishlist(user_id)
    
    if data.startswith("wh_page_"):
        page = int(data.split("_")[2])
        keyboard = build_wh_keyboard(wishlist, page)
        text = f"📖 <b>Your Doujin Wishlist</b> (Page {page+1})\n<i>Select a title to view details:</i>"
        if query.message.photo:
            await query.message.delete()
            await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=keyboard, parse_mode='HTML')
        else:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        return
        
    if data.startswith("wh_view_"):
        code = data.split("_")[2]
        item = next((i for i in wishlist if i['code'] == code), None)
        if not item:
            await query.edit_message_text("❌ Item not found in wishlist.")
            return
            
        tags = item.get('tags', [])
        tag_str = ", ".join(tags) if tags else "No tags found"
        if len(tag_str) > 800:
            tag_str = tag_str[:797] + "..."
            
        caption = (
            f"📖 <b>{item['title'].replace('<', '&lt;').replace('>', '&gt;')}</b>\n"
            f"🏷️ <b>Tags:</b> {tag_str}\n"
            f"🔗 <a href=\"https://nhentai.net/g/{code}\">nhentai.net/g/{code}</a>"
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Remove from Wishlist", callback_data=f"wh_del_{code}")],
            [InlineKeyboardButton("🔙 Back to List", callback_data="wh_page_0")]
        ])
        
        # We can't edit a text message into a photo message directly, 
        # so we send a new photo message and delete the old menu message.
        from curl_cffi import requests as c_requests
        img_res = await asyncio.to_thread(c_requests.get, item['cover_url'], impersonate='chrome110', timeout=10)
        if img_res.status_code == 200:
            os.makedirs('downloads', exist_ok=True)
            cover_path = f"downloads/wh_{code}.jpg"
            with open(cover_path, 'wb') as f:
                f.write(img_res.content)
            with open(cover_path, 'rb') as f:
                await query.message.reply_photo(photo=f, caption=caption, parse_mode='HTML', reply_markup=keyboard)
            os.remove(cover_path)
            await query.message.delete()
        else:
            await query.edit_message_text(f"❌ Failed to load cover.\n{caption}", reply_markup=keyboard, parse_mode='HTML', disable_web_page_preview=True)
        return
        
    if data.startswith("wh_del_"):
        code = data.split("_")[2]
        wishlist = [i for i in wishlist if i['code'] != code]
        save_wishlist(user_id, wishlist)
        
        keyboard = build_wh_keyboard(wishlist, 0)
        # Check if the current message is a photo or text
        if query.message.photo:
            await query.message.delete()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🗑️ <i>Removed {code} from wishlist.</i>\n\n📖 <b>Your Doujin Wishlist</b> (Page 1)",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(
                f"🗑️ <i>Removed {code} from wishlist.</i>\n\n📖 <b>Your Doujin Wishlist</b> (Page 1)",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
        return

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

async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    # Ensure this is somewhat protected (optional, maybe check against an admin ID if you have one)
    if os.path.exists('bot.log'):
        with open('bot.log', 'r', encoding='utf-8') as f:
            from collections import deque
            recent_logs = "".join(deque(f, 50))
            # Truncate if too long for Telegram (4096 limit)
            if len(recent_logs) > 4000:
                recent_logs = "..." + recent_logs[-4000:]
            await update.message.reply_text(f"📝 <b>Recent Logs:</b>\n<pre>{recent_logs.replace('<', '&lt;').replace('>', '&gt;')}</pre>", parse_mode='HTML')
    else:
        await update.message.reply_text("📭 No log file found.")

from aiohttp import web

# ── Light Novel Integration ──────────────────────────────────────────────

async def ln_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ln <title> command."""
    if not context.args:
        await update.message.reply_text(f"Usage: /ln <title>\nExample: /ln Overlord\n\n🤖 {VERSION}")
        return

    query = " ".join(context.args)
    reply_msg = await update.message.reply_text(f"🔍 Searching AllNovelFull for '{query}'...")

    try:
        from bs4 import BeautifulSoup
        r = await asyncio.to_thread(
            requests.get, f"https://allnovelfull.com/search?keyword={query}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15
        )
        soup = BeautifulSoup(r.text, 'html.parser')
        results = [a for h3 in soup.find_all('h3', class_='tit') for a in h3.find_all('a')]

        if not results:
            await reply_msg.edit_text(f"❌ No light novel found for '{query}'. ({VERSION})")
            return

        keyboard = []
        if 'ln_search' not in context.user_data:
            context.user_data['ln_search'] = {}
            
        for i, a in enumerate(results[:5]): # Top 5 results
            label = a.text.strip()[:50]
            link = a['href']
            ch_id = str(uuid.uuid4())[:8] # short unique id
            context.user_data['ln_search'][ch_id] = {'label': label, 'link': f"https://allnovelfull.com{link}"}
            keyboard.append([InlineKeyboardButton(label, callback_data=f"ln_sel_{ch_id}")])

        await reply_msg.edit_text(
            f"📚 <b>Results for '{query}':</b>\n<i>Select a novel:</i>",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Light Novel search error: {e}")
        await reply_msg.edit_text(f"❌ Error searching Light Novels. ({VERSION})")

async def _fetch_ln_chapter(url, sem, loop):
    async with sem:
        from bs4 import BeautifulSoup
        r = await loop.run_in_executor(None, lambda: requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}))
        soup = BeautifulSoup(r.text, 'html.parser')
        content = soup.find('div', class_='chapter-c')
        title_el = soup.find('a', class_='chapter-title')
        title = title_el.text.strip() if title_el else "Unknown Chapter"
        return title, (content.decode_contents() if content else "")

async def ln_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all ln_ callback buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data

    from bs4 import BeautifulSoup
    from ebooklib import epub

    if data.startswith("ln_sel_"):
        ch_id = data[7:]
        search_data = context.user_data.get('ln_search', {}).get(ch_id)
        if not search_data:
            await query.edit_message_text("❌ Session expired. Please search again with /ln.")
            return
            
        await query.edit_message_text("⏳ Loading novel details...")
        
        try:
            r = await asyncio.to_thread(
                requests.get, search_data['link'],
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15
            )
            
            m = re.search(r'data-novel-id=[\'\"]([0-9]+)[\'\"]', r.text)
            if not m:
                await query.edit_message_text(f"❌ Error extracting novel ID. ({VERSION})")
                return
            novel_id = m.group(1)
            
            r_ch = await asyncio.to_thread(
                requests.get, f"https://allnovelfull.com/ajax-chapter-option?novelId={novel_id}",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15
            )
            soup = BeautifulSoup(r_ch.text, 'html.parser')
            options = soup.find_all('option')
            
            if not options:
                await query.edit_message_text(f"❌ No chapters found. ({VERSION})")
                return
                
            ch_urls = [f"https://allnovelfull.com{o['value']}" for o in options]
            
            # Store chapters for downloading
            context.user_data[f"ln_chapters_{novel_id}"] = ch_urls
            context.user_data[f"ln_title_{novel_id}"] = search_data['label']
            
            keyboard = [[InlineKeyboardButton(f"⬇️ Build EPUB ({len(ch_urls)} chapters)", callback_data=f"ln_dl_{novel_id}")]]
            await query.edit_message_text(
                f"📖 <b>{search_data['label']}</b>\n📄 {len(ch_urls)} chapters available.",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Light Novel chapter list error: {e}")
            await query.edit_message_text(f"❌ Error loading chapters. ({VERSION})")
        return

    if data.startswith("ln_dl_"):
        novel_id = data[6:]
        ch_urls = context.user_data.get(f"ln_chapters_{novel_id}")
        novel_title = context.user_data.get(f"ln_title_{novel_id}", "Novel")
        
        if not ch_urls:
            await query.edit_message_text("❌ Session expired. Please search again with /ln.")
            return

        await query.edit_message_text(f"⏳ Building EPUB for {len(ch_urls)} chapters...\nThis will take a few minutes.")

        try:
            sem = asyncio.Semaphore(20)
            loop = asyncio.get_running_loop()
            
            progress = {'done': 0, 'total': len(ch_urls)}
            
            async def wrapped_fetch(url):
                res = await _fetch_ln_chapter(url, sem, loop)
                progress['done'] += 1
                if progress['done'] % 50 == 0 or progress['done'] == progress['total']:
                    try:
                        await query.edit_message_text(f"⏳ Downloading chapters... {progress['done']}/{progress['total']}")
                    except Exception:
                        pass
                return res

            tasks = [wrapped_fetch(url) for url in ch_urls]
            results = await asyncio.gather(*tasks)
            
            await query.edit_message_text("⏳ Compiling EPUB file...")
            
            book = epub.EpubBook()
            book.set_identifier(str(uuid.uuid4()))
            book.set_title(novel_title)
            book.set_language('en')
            
            spine = ['nav']
            toc = []
            
            for i, (title, content) in enumerate(results):
                ch = epub.EpubHtml(title=title, file_name=f'chap_{i}.xhtml')
                ch.content = f'<h1>{title}</h1>{content}'
                book.add_item(ch)
                spine.append(ch)
                toc.append(epub.Link(f'chap_{i}.xhtml', title, f'chap_{i}'))
                
            book.toc = tuple(toc)
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            book.spine = spine
            
            os.makedirs("downloads", exist_ok=True)
            safe_title = re.sub(r'[^\w\s-]', '', novel_title)[:50].strip()
            epub_filename = f"downloads/{safe_title}.epub"
            
            epub.write_epub(epub_filename, book, {})
            
            file_size = os.path.getsize(epub_filename)
            caption = f"📖 <b>{novel_title}</b>\n📄 {len(ch_urls)} chapters\n\n🤖 <i>{VERSION}</i>"

            if file_size > 50 * 1024 * 1024:
                await query.edit_message_text("⏳ EPUB is over 50MB, uploading to Gofile...")
                cloud_link = upload_to_gofile(epub_filename)
                if cloud_link:
                    await query.edit_message_text(
                        f"{caption}\n\n✅ File was too large for Telegram:\n🔗 {cloud_link}",
                        parse_mode="HTML", disable_web_page_preview=True
                    )
                else:
                    await query.edit_message_text(f"❌ EPUB too large and cloud upload failed. ({VERSION})")
            else:
                with open(epub_filename, "rb") as f:
                    await query.message.reply_document(
                        document=f, filename=os.path.basename(epub_filename),
                        caption=caption, parse_mode="HTML"
                    )
                await query.edit_message_text(f"✅ Light Novel sent! ({VERSION})")

            if os.path.exists(epub_filename):
                os.remove(epub_filename)

        except Exception as e:
            logger.error(f"Light Novel download error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await query.edit_message_text(f"❌ Error building EPUB. ({VERSION})")
        return

# ── MangaKatana Integration ──────────────────────────────────────────────

async def manga_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /manga <title> command."""
    if not context.args:
        await update.message.reply_text(f"Usage: /manga <title>\nExample: /manga Overlord\n\n🤖 {VERSION}")
        return

    query = " ".join(context.args)
    reply_msg = await update.message.reply_text(f"🔍 Searching MangaKatana for '{query}'...")

    try:
        from bs4 import BeautifulSoup
        r = await asyncio.to_thread(
            requests.get, f"https://mangakatana.com/?search={query}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15
        )
        soup = BeautifulSoup(r.text, 'html.parser')
        results = soup.select('#book_list .item .text .title a')

        if not results:
            await reply_msg.edit_text(f"❌ No manga found for '{query}'. ({VERSION})")
            return

        keyboard = []
        for m in results[:5]: # Top 5 results
            label = m.text.strip()[:50]
            link = m['href']
            manga_id = link.split('/')[-1]
            keyboard.append([InlineKeyboardButton(label, callback_data=f"mg_sel_{manga_id[:40]}")])

        await reply_msg.edit_text(
            f"📚 <b>Results for '{query}':</b>\n<i>Select a manga:</i>",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"MangaKatana search error: {e}")
        await reply_msg.edit_text(f"❌ Error searching MangaKatana. ({VERSION})")


def _build_chapter_keyboard(chapters, manga_id, page):
    """Build paginated chapter list keyboard."""
    PER_PAGE = 10
    start = page * PER_PAGE
    end = start + PER_PAGE
    current = chapters[start:end]

    keyboard = []
    for ch in current:
        title = ch["title"]
        ch_url = ch["url"]
        ch_suffix = ch_url.split('/')[-1]
        label = title[:50]
        cb_data = f"mg_dl_{manga_id[:35]}::{ch_suffix[:15]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=cb_data)])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"mg_chp_{manga_id[:40]}_{page - 1}"))
    if end < len(chapters):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"mg_chp_{manga_id[:40]}_{page + 1}"))
    if nav:
        keyboard.append(nav)

    return InlineKeyboardMarkup(keyboard)


async def manga_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all mg_ callback buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data

    from bs4 import BeautifulSoup

    # ── User selected a manga from search results ──
    if data.startswith("mg_sel_"):
        manga_id = data[7:]
        await query.edit_message_text("⏳ Loading chapters...")

        try:
            r = await asyncio.to_thread(
                requests.get, f"https://mangakatana.com/manga/{manga_id}",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15
            )
            soup = BeautifulSoup(r.text, 'html.parser')
            chapters_tags = soup.select('.chapters .chapter a')
            chapters_tags = list(reversed(chapters_tags)) # Ascending order

            if not chapters_tags:
                await query.edit_message_text(f"❌ No chapters found for this manga. ({VERSION})")
                return

            all_chapters = [{"title": c.text.strip(), "url": c['href']} for c in chapters_tags]

            # Store chapters in user_data for pagination
            context.user_data[f"mg_chapters_{manga_id}"] = all_chapters

            keyboard = _build_chapter_keyboard(all_chapters, manga_id, 0)
            await query.edit_message_text(
                f"📖 <b>{len(all_chapters)} chapters found.</b>\n<i>Select a chapter to download:</i>",
                reply_markup=keyboard, parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"MangaKatana chapter list error: {e}")
            await query.edit_message_text(f"❌ Error loading chapters. ({VERSION})")
        return

    # ── Chapter pagination ──
    if data.startswith("mg_chp_"):
        parts = data[7:].rsplit("_", 1)
        manga_id, page = parts[0], int(parts[1])
        chapters = context.user_data.get(f"mg_chapters_{manga_id}", [])
        if not chapters:
            await query.edit_message_text("❌ Session expired. Please search again with /manga.")
            return
        keyboard = _build_chapter_keyboard(chapters, manga_id, page)
        await query.edit_message_text(
            f"📖 <b>{len(chapters)} chapters found.</b>\n<i>Select a chapter to download:</i>",
            reply_markup=keyboard, parse_mode="HTML"
        )
        return

    # ── Download a specific chapter ──
    if data.startswith("mg_dl_"):
        cb_data = data[6:]
        if "::" not in cb_data:
            await query.edit_message_text("❌ Invalid chapter format.")
            return
            
        manga_id, ch_suffix = cb_data.split("::")
        await query.edit_message_text("⏳ Extracting image URLs...")

        try:
            ch_url = f"https://mangakatana.com/manga/{manga_id}/{ch_suffix}"
            r = await asyncio.to_thread(
                requests.get, ch_url,
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15
            )
            
            m = re.search(r'var thzq=\[(.*?)\];', r.text)
            if not m:
                m = re.search(r'var ytaw=\[(.*?)\];', r.text)
            if not m:
                await query.edit_message_text("❌ Could not extract images from chapter page.")
                return
                
            img_urls = [u.strip("'\"") for u in m.group(1).split(',')]

            # Download all page images into a .cbz (zip) in memory
            os.makedirs("downloads", exist_ok=True)
            safe_title = re.sub(r'[^\w\s-]', '', manga_id)[:40].strip()
            cbz_filename = f"downloads/{safe_title}_{ch_suffix}.cbz"

            with zipfile.ZipFile(cbz_filename, "w", zipfile.ZIP_STORED) as zf:
                for i, img_url in enumerate(img_urls):
                    img_r = await asyncio.to_thread(
                        requests.get, img_url,
                        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://mangakatana.com/"}, 
                        timeout=30
                    )
                    if img_r.status_code == 200:
                        ext = img_url.split(".")[-1]
                        if len(ext) > 4 or not ext: ext = "jpg"
                        zf.writestr(f"{i+1:04d}.{ext}", img_r.content)

                    # Update progress every 10 pages
                    if (i + 1) % 10 == 0:
                        try:
                            await query.edit_message_text(f"⏳ Downloading... {i+1}/{len(img_urls)} pages")
                        except Exception:
                            pass

            file_size = os.path.getsize(cbz_filename)
            caption = f"📖 <b>{manga_id} - {ch_suffix}</b>\n📄 {len(img_urls)} pages\n\n🤖 <i>{VERSION}</i>"

            if file_size > 50 * 1024 * 1024:
                await query.edit_message_text("⏳ Chapter is over 50MB, uploading to Gofile...")
                cloud_link = upload_to_gofile(cbz_filename)
                if cloud_link:
                    await query.edit_message_text(
                        f"{caption}\n\n✅ File was too large for Telegram:\n🔗 {cloud_link}",
                        parse_mode="HTML", disable_web_page_preview=True
                    )
                else:
                    await query.edit_message_text(f"❌ Chapter too large and cloud upload failed. ({VERSION})")
            else:
                with open(cbz_filename, "rb") as f:
                    await query.message.reply_document(
                        document=f, filename=os.path.basename(cbz_filename),
                        caption=caption, parse_mode="HTML"
                    )
                await query.edit_message_text(f"✅ {ch_suffix} sent! ({VERSION})")

            if os.path.exists(cbz_filename):
                os.remove(cbz_filename)

        except Exception as e:
            logger.error(f"MangaKatana download error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await query.edit_message_text(f"❌ Error downloading chapter. ({VERSION})")
        return


async def main():
    if not TOKEN or TOKEN == 'your_bot_token_here':
        logger.error("Please set TELEGRAM_BOT_TOKEN")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("adminmisuse", adminmisuse))
    application.add_handler(CommandHandler("wh", wh_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("manga", manga_command))
    application.add_handler(CommandHandler("ln", ln_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(wh_callback, pattern="^wh_"))
    application.add_handler(CallbackQueryHandler(manga_callback, pattern="^mg_"))
    application.add_handler(CallbackQueryHandler(ln_callback, pattern="^ln_"))

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
