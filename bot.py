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
import html
from dotenv import load_dotenv
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, MessageReactionHandler
from telegram.request import HTTPXRequest
from pymongo import MongoClient
from bs4 import BeautifulSoup

VERSION = "v2.7.1"

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

MONGO_URI = os.environ.get('MONGO_URI')
if MONGO_URI:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client['media_downloader']
    wishlist_col = db['wishlist']
    stats_col = db['stats']
    retry_col = db['retry_queue']
    history_col = db['download_history']
else:
    mongo_client = None
    retry_col = None
    history_col = None

def track_stat(user_id, field="downloads", url=None):
    if not mongo_client: return
    from datetime import datetime, timezone
    try:
        inc_ops = {field: 1}
        if url:
            from urllib.parse import urlparse
            import re
            domain = urlparse(url).netloc.lower()
            if 'reddit.com' in domain:
                inc_ops["sources.reddit"] = 1
                match = re.search(r'reddit\.com/r/([^/]+)', url, re.IGNORECASE)
                if match:
                    sub = match.group(1).lower()
                    inc_ops[f"subreddits.{sub}"] = 1
            elif 'twitter.com' in domain or 'x.com' in domain:
                inc_ops["sources.twitter"] = 1
            elif 'tiktok.com' in domain:
                inc_ops["sources.tiktok"] = 1
            elif 'instagram.com' in domain:
                inc_ops["sources.instagram"] = 1
            elif 'redgifs.com' in domain:
                inc_ops["sources.redgifs"] = 1
            elif 'nhentai.net' in domain:
                pass # Already tracked as manga_downloads or similar
            else:
                inc_ops["sources.other"] = 1

        stats_col.update_one(
            {"_id": str(user_id)},
            {"$inc": inc_ops, "$set": {"last_active": datetime.now(timezone.utc).isoformat()},
             "$setOnInsert": {"first_seen": datetime.now(timezone.utc).isoformat()}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error tracking stat {field}: {e}")

def get_wishlist(user_id):
    try:
        if mongo_client:
            doc = wishlist_col.find_one({"_id": str(user_id)})
            return doc.get('data', []) if doc else []
        else:
            if os.path.exists('wishlist.json'):
                with open('wishlist.json', 'r') as f:
                    data = json.load(f)
                    return data.get(str(user_id), [])
    except Exception as e:
        logger.error(f"Error getting wishlist: {e}")
    return []

def save_wishlist(user_id, wishlist):
    try:
        if mongo_client:
            wishlist_col.update_one(
                {"_id": str(user_id)},
                {"$set": {"data": wishlist}},
                upsert=True
            )
        else:
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

def get_failed_queue(user_id):
    try:
        if mongo_client and retry_col is not None:
            doc = retry_col.find_one({"_id": str(user_id)})
            return doc.get('items', []) if doc else []
        else:
            if os.path.exists('retry_queue.json'):
                with open('retry_queue.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get(str(user_id), [])
    except Exception as e:
        logger.error(f"Error getting retry queue: {e}")
    return []

def add_failed_download(user_id, url, raw_text, reason=None):
    from datetime import datetime, timezone
    try:
        items = get_failed_queue(user_id)
        # Avoid duplicate URLs, move latest attempt to top
        items = [i for i in items if i.get('url') != url]
        items.insert(0, {
            "url": url,
            "raw_text": raw_text,
            "reason": str(reason)[:200] if reason else "Download failed",
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        })
        items = items[:50]
        if mongo_client and retry_col is not None:
            retry_col.update_one(
                {"_id": str(user_id)},
                {"$set": {"items": items}},
                upsert=True
            )
        else:
            data = {}
            if os.path.exists('retry_queue.json'):
                with open('retry_queue.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
            data[str(user_id)] = items
            with open('retry_queue.json', 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        logger.info(f"Added {url} to retry queue for user {user_id}")
    except Exception as e:
        logger.error(f"Error adding to retry queue: {e}")

def remove_failed_download(user_id, url):
    try:
        items = get_failed_queue(user_id)
        new_items = [i for i in items if i.get('url') != url]
        if len(new_items) == len(items):
            return
        if mongo_client and retry_col is not None:
            retry_col.update_one(
                {"_id": str(user_id)},
                {"$set": {"items": new_items}},
                upsert=True
            )
        else:
            data = {}
            if os.path.exists('retry_queue.json'):
                with open('retry_queue.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
            data[str(user_id)] = new_items
            with open('retry_queue.json', 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        logger.info(f"Removed {url} from retry queue for user {user_id}")
    except Exception as e:
        logger.error(f"Error removing from retry queue: {e}")

def clear_failed_queue(user_id):
    try:
        if mongo_client and retry_col is not None:
            retry_col.delete_one({"_id": str(user_id)})
        else:
            if os.path.exists('retry_queue.json'):
                with open('retry_queue.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
                data.pop(str(user_id), None)
                with open('retry_queue.json', 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error clearing retry queue: {e}")

def normalize_history_url(url):
    if not url:
        return ""
    # Strip whitespace, queries, fragments, and trailing slashes for resilient lookup
    clean = url.strip().split('?')[0].split('#')[0].rstrip('/')
    return clean.lower()

def record_download_history(user_id, url, status, title=None, error=None, message_id=None):
    from datetime import datetime, timezone
    try:
        norm_key = normalize_history_url(url)
        record = {
            "user_id": str(user_id),
            "url": url,
            "norm_key": norm_key,
            "status": status,
            "title": title,
            "error": str(error) if error else None,
            "message_id": message_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "time_display": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        }
        if mongo_client and history_col is not None:
            history_col.update_one(
                {"user_id": str(user_id), "norm_key": norm_key},
                {"$set": record},
                upsert=True
            )
        else:
            history = {}
            if os.path.exists('download_history.json'):
                try:
                    with open('download_history.json', 'r', encoding='utf-8') as f:
                        history = json.load(f)
                except Exception:
                    history = {}
            user_hist = history.get(str(user_id), {})
            user_hist[norm_key] = record
            if len(user_hist) > 300:
                sorted_keys = sorted(user_hist.keys(), key=lambda k: user_hist[k].get('timestamp', ''), reverse=True)[:300]
                user_hist = {k: user_hist[k] for k in sorted_keys}
            history[str(user_id)] = user_hist
            with open('download_history.json', 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error recording download history: {e}")

def get_download_history_item(user_id, url):
    try:
        norm_key = normalize_history_url(url)
        if mongo_client and history_col is not None:
            doc = history_col.find_one({
                "user_id": str(user_id),
                "$or": [{"norm_key": norm_key}, {"url": url}]
            })
            return doc
        else:
            if os.path.exists('download_history.json'):
                with open('download_history.json', 'r', encoding='utf-8') as f:
                    history = json.load(f)
                    user_hist = history.get(str(user_id), {})
                    if norm_key in user_hist:
                        return user_hist[norm_key]
                    for k, item in user_hist.items():
                        if k == norm_key or item.get('url') == url:
                            return item
    except Exception as e:
        logger.error(f"Error getting download history: {e}")
    return None

def extract_urls_from_message(msg):
    if not msg:
        return []
    urls = []
    text = msg.text or msg.caption or ""
    entities = list(msg.entities or ()) + list(msg.caption_entities or ())
    for entity in entities:
        if entity.type == 'text_link' and entity.url:
            if entity.url not in urls:
                urls.append(entity.url)
        elif entity.type == 'url':
            u = text[entity.offset:entity.offset + entity.length]
            if u and u not in urls:
                urls.append(u)
    for u in re.findall(r'https?://[^\s<>"]+', text):
        clean_u = u.rstrip(').,;!?')
        if clean_u not in urls:
            urls.append(clean_u)
    return urls

redl_cache = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"🤖 <b>Media Downloader Bot</b> <code>{VERSION}</code>\n\n"

        "📥 <b>Download Media</b>\n"
        "Send any link from Reddit, Twitter/X, RedGifs, Instagram, TikTok, "
        "or most video sites and I'll grab the media for you.\n"
        "• Add <code>-f</code> after a link to get raw files instead of compressed media\n"
        "• For albums, add numbers to pick specific items: "
        "<code>https://reddit.com/... 1 3 5</code>\n"
        "• Use ranges too: <code>https://reddit.com/... 2-6</code>\n"
        "• /retry — Retry any previously failed downloads (/retry list, /retry clear)\n"
        "• /clean — Clean up all bot responses & media while keeping your link messages intact\n\n"

        "📖 <b>Manga & Doujins</b>\n"
        "/manga <code>&lt;title&gt;</code> — Search & download manga chapters as CBZ\n"
        "Send a 5-6 digit code to look up & download from nhentai\n"
        "/wh — View your saved doujin wishlist\n\n"

        "📚 <b>Light Novels</b>\n"
        "/ln <code>&lt;title&gt;</code> — Search & download light novels as EPUB\n\n"

        "❤️ <b>Vault & Anti-Censorship Cloak</b>\n"
        "• Forward any media post from Telegram channels here to send an unlinked clean copy to your Vault (bypasses all channel blocks & bans)\n"
        "• React to any downloaded media with 👍 or ❤️ to send it to your Vault\n"
        "• /setvault — Set or update your private Vault channel\n\n"

        "📊 <b>Stats & More</b>\n"
        "/stats — View your download stats\n"
        "/logs — View recent bot logs"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

def fetch_twitter_details(url):
    match = re.search(r'status/(\d+)', url)
    if not match:
        return None
    tweet_id = match.group(1)
    api_url = f"https://api.vxtwitter.com/Twitter/status/{tweet_id}"
    try:
        res = requests.get(api_url, headers={'User-Agent': 'TelegramBot (like TwitterBot)'}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('hasMedia'):
                items = []
                for media in data.get('media_extended', []):
                    m_type = media.get('type')
                    m_url = media.get('url')
                    full_res = m_url
                    if m_type == 'image' and 'pbs.twimg.com' in m_url:
                        base = m_url.split('?')[0]
                        full_res = base + '?name=orig'
                    items.append({
                        'type': m_type,
                        'url': m_url,
                        'full_res': full_res,
                        'width': media.get('size', {}).get('width'),
                        'height': media.get('size', {}).get('height')
                    })
                if not items and data.get('mediaURLs'):
                    for u in data['mediaURLs']:
                        is_vid = any(u.endswith(ext) for ext in ('.mp4', '.m3u8'))
                        full_res = u
                        if not is_vid and 'pbs.twimg.com' in u:
                            full_res = u.split('?')[0] + '?name=orig'
                        items.append({
                            'type': 'video' if is_vid else 'image',
                            'url': u,
                            'full_res': full_res
                        })
                return {
                    'title': data.get('text', ''),
                    'user_name': data.get('user_name', ''),
                    'user_screen_name': data.get('user_screen_name', ''),
                    'tweet_url': f"https://x.com/{data.get('user_screen_name', 'i')}/status/{tweet_id}",
                    'items': items
                }
        elif res.status_code == 404:
            return "ERROR_REGION_BLOCKED"
    except Exception as e:
        logger.error(f"vxtwitter API error in fetch_twitter_details: {e}")
    return None

async def download_media(url: str, media_indices: list = None, as_files: bool = False):
    """Returns (files, title, original_url)"""
    title = None
    original_url = url

    def _fallback_reddit(url):
        nonlocal title
        # 1. Try vxreddit proxy to extract media and bypass Reddit's web block/JS challenges
        try:
            s = requests.Session()
            headers = {'User-Agent': 'TelegramBot (like TwitterBot)'}
            r = s.get(url, headers=headers, allow_redirects=True, timeout=10)
            full_url = r.url.split('?')[0]

            vx_url = re.sub(r'https?://(?:www\.)?reddit\.com', 'https://vxreddit.com', full_url)
            r2 = s.get(vx_url, headers=headers, timeout=10)
            if r2.status_code == 200:
                soup = BeautifulSoup(r2.text, 'html.parser')
                og_title = soup.find('meta', property='og:title')
                if og_title:
                    title = og_title.get('content')

                # Check if description points to an external host (RedGifs, Imgur, etc.)
                desc_meta = soup.find('meta', property='og:description')
                if desc_meta:
                    desc = desc_meta.get('content', '')
                    ext_matches = re.findall(r'(https?://\S+)', desc)
                    for match in ext_matches:
                        if any(d in match for d in ['redgifs.com', 'imgur.com', 'gfycat.com']):
                            return [], match

                # Check for video streams
                video_urls = []
                for meta in soup.find_all('meta', property=re.compile(r'og:video(:url|:secure_url)?$')):
                    c = meta.get('content')
                    if c and c not in video_urls: video_urls.append(c)
                for meta in soup.find_all('meta', attrs={'name': re.compile(r'twitter:player(:stream)?$')}):
                    c = meta.get('content')
                    if c and c not in video_urls: video_urls.append(c)

                if video_urls:
                    return [], video_urls[0]

                # Check for gallery / single images
                image_urls = []
                for meta in soup.find_all('meta', property='og:image'):
                    c = meta.get('content')
                    if c and 'preview.redd.it' not in c and 'external-preview' not in c:
                        if c not in image_urls: image_urls.append(c)
                if not image_urls:
                    for meta in soup.find_all('meta', property='og:image'):
                        c = meta.get('content')
                        if c and c not in image_urls: image_urls.append(c)

                if image_urls:
                    if media_indices:
                        filtered = []
                        for idx in media_indices:
                            if 0 < idx <= len(image_urls):
                                filtered.append(image_urls[idx - 1])
                        image_urls = filtered

                    files = []
                    os.makedirs('downloads', exist_ok=True)
                    for m_url in image_urls:
                        m_res = s.get(m_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
                        if m_res.status_code == 200:
                            ct = m_res.headers.get('content-type', '').lower()
                            if 'html' in ct or 'text' in ct:
                                continue
                            ext = m_url.split('.')[-1].split('?')[0]
                            if len(ext) > 4 or not ext: ext = 'jpg'
                            fname = f'downloads/reddit_{uuid.uuid4().hex}.{ext}'
                            with open(fname, 'wb') as f:
                                f.write(m_res.content)
                            files.append(fname)
                    if files:
                        return files, None
        except Exception as e:
            logger.error(f"vxreddit fallback failed: {e}")

        # 2. Check Rapidsave resolver for external embedded videos (RedGifs, Imgur, etc.) to bypass Reddit age-gate
        try:
            r_rs = s.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}, allow_redirects=True, timeout=8)
            resolved_url = r_rs.url.split('?')[0]
            rs_res = s.get(f'https://rapidsave.com/info?url={resolved_url}', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}, timeout=10)
            if rs_res.status_code == 200:
                rs_soup = BeautifulSoup(rs_res.text, 'html.parser')
                d_btn = rs_soup.find('a', class_='downloadbutton')
                if d_btn and d_btn.get('href'):
                    target_media = d_btn.get('href')
                    if any(d in target_media for d in ['redgifs.com', 'imgur.com', 'gfycat.com']):
                        logger.info(f"Resolved Reddit external embed via Rapidsave: {target_media}")
                        return [], target_media
        except Exception as e:
            logger.warning(f"Rapidsave external embed check failed: {e}")

        # 3. Fallback to direct Reddit JSON API as tertiary backup
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

            if media_indices:
                filtered_urls = []
                for idx in media_indices:
                    if idx > 0 and idx <= len(media_urls):
                        filtered_urls.append(media_urls[idx - 1])
                media_urls = filtered_urls

            files = []
            os.makedirs('downloads', exist_ok=True)
            for m_url, ext in media_urls:
                m_res = s.get(m_url)
                if m_res.status_code == 200:
                    ct = m_res.headers.get('content-type', '').lower()
                    if 'html' in ct or 'text' in ct:
                        continue
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

            if media_indices:
                filtered_urls = []
                for idx in media_indices:
                    if idx > 0 and idx <= len(media_urls):
                        filtered_urls.append(media_urls[idx - 1])
                media_urls = filtered_urls

            files = []
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            os.makedirs('downloads', exist_ok=True)
            for m_url in media_urls:
                m_res = requests.get(m_url, headers=headers)
                if m_res.status_code == 200:
                    ct = m_res.headers.get('content-type', '').lower()
                    if 'html' in ct or 'text' in ct:
                        continue
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
        data = fetch_twitter_details(url)
        if data == "ERROR_REGION_BLOCKED":
            return None, "ERROR_REGION_BLOCKED"
        if data and data.get('items'):
            v_url = None
            for it in data['items']:
                if it.get('type') in ('video', 'gif'):
                    v_url = it.get('url')
                    break
            if not v_url:
                v_url = data['items'][0].get('url')
            return v_url, data.get('title')
        return None, None

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
        elif 'tiktok.com' in url:
            try:
                # Use tikwm API to get 100% watermark-free raw video
                res = requests.get(f"https://www.tikwm.com/api/?url={url}", timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    if data.get('data') and data['data'].get('play'):
                        play_url = data['data']['play']
                        title = data['data'].get('title', '')
                        
                        os.makedirs('downloads', exist_ok=True)
                        m_res = requests.get(play_url, headers={'User-Agent': 'Mozilla/5.0'})
                        if m_res.status_code == 200:
                            filename = f"downloads/tiktok_{uuid.uuid4().hex}.mp4"
                            with open(filename, 'wb') as f:
                                f.write(m_res.content)
                            return [filename]
            except Exception as e:
                logger.error(f"TikTok tikwm fallback failed: {e}")
                # Fall through to yt-dlp if it fails

        # Bandwidth optimization: Default to <=720p to save ~70% bandwidth unless raw files (-f) explicitly requested
        format_selector = (
            'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best/bv*+ba/b'
            if as_files else
            'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/bestvideo[height<=720]+bestaudio/best[height<=720]/best'
        )

        ydl_opts = {
            'outtmpl': 'downloads/%(extractor)s_%(id)s.%(ext)s',
            'format': format_selector,
            'merge_output_format': 'mp4',
            'postprocessor_args': {'merger': ['-c', 'copy']},
            'noplaylist': False,
            'quiet': True,
            'no_warnings': True,
        }
        
        if media_indices:
            ydl_opts['playlist_items'] = ','.join(str(idx) for idx in media_indices)
            
        # Add Instagram cookie if provided in .env to bypass the strict login wall
        ig_cookie = os.environ.get('INSTAGRAM_SESSION_ID')
        if ig_cookie and ('instagram.com' in url or 'ig.me' in url):
            with open('ig_cookies.txt', 'w') as f:
                f.write(f"# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2145916800\tsessionid\t{ig_cookie}\n")
            ydl_opts['cookiefile'] = 'ig_cookies.txt'

        # Add Reddit cookie if provided in .env or file to bypass strict 18+ login walls
        reddit_cookie = os.environ.get('REDDIT_SESSION')
        if ('reddit.com' in url or 'redd.it' in url):
            if reddit_cookie:
                with open('reddit_cookies.txt', 'w', encoding='utf-8') as f:
                    f.write(f"# Netscape HTTP Cookie File\n.reddit.com\tTRUE\t/\tTRUE\t2145916800\treddit_session\t{reddit_cookie}\n")
                ydl_opts['cookiefile'] = 'reddit_cookies.txt'
            elif os.path.exists('reddit_cookies.txt'):
                ydl_opts['cookiefile'] = 'reddit_cookies.txt'
            
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
            await message.reply_text("⏳ A file in this album is over 50MB! Uploading the uncompressed original to Gofile...", quote=True)
            cloud_link = upload_to_gofile(lf)
            if cloud_link:
                await message.reply_text(f"✅ One of the files was too large for Telegram, so I uploaded it here:\n\n🔗 {cloud_link}", disable_web_page_preview=True, quote=True)
            else:
                await message.reply_text("❌ A file was larger than Telegram's 50MB limit and cloud upload failed.", quote=True)
                
        if not valid_files:
            return

        if as_files:
            group = []
            for i, f in enumerate(valid_files):
                handle = open(f, 'rb')
                handles.append(handle)
                group.append(InputMediaDocument(handle, caption=caption if i == 0 else None, parse_mode='HTML' if caption else None))
            for chunk in chunk_list(group, 10):
                await message.reply_media_group(media=chunk, quote=True, allow_sending_without_reply=True, read_timeout=300, write_timeout=300)
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
                await message.reply_media_group(media=chunk, quote=True, allow_sending_without_reply=True, read_timeout=300, write_timeout=300)
    finally:
        for h in handles:
            h.close()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    text = message.text or message.caption or ""
    user_id = message.from_user.id
    reply_to = message.reply_to_message

    # Check for nhentai 5-6 digit code
    nh_match = re.fullmatch(r'(\d{5,6})', text.strip())
    if nh_match:
        code = nh_match.group(1)
        reply_msg = await update.message.reply_text("⏳ Fetching doujin info...", quote=True)
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
                    wishlist = get_wishlist(user_id)
                    wishlist = [item for item in wishlist if item.get('code') != code]
                    wishlist.insert(0, {
                        'code': code,
                        'title': title,
                        'cover_url': cover_url,
                        'tags': tags
                    })
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
                        
                        keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("⬇️ Download CBZ", callback_data=f"nh_dl_{code}")]
                        ])
                        
                        with open(cover_path, 'rb') as f:
                            await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML', reply_markup=keyboard, quote=True)
                            
                        os.remove(cover_path)
                        await reply_msg.delete()
                        return
            
            await reply_msg.edit_text("❌ Could not fetch info for that code. It might not exist.")
        except Exception as e:
            logger.error(f"Error fetching nhentai: {e}")
            await reply_msg.edit_text(f"❌ Error fetching doujin info. ({VERSION})")
        return

    # Check if current message has its own links
    new_urls = re.findall(r'(https?://\S+)', text)

    # If the user did NOT send a new link, BUT replied to an existing message:
    if not new_urls and reply_to:
        reply_urls = extract_urls_from_message(reply_to)
        if reply_urls:
            target_url = reply_urls[0]
            await handle_replied_link(message, reply_to, target_url, text, user_id)
            return

    return await process_media_request(message, text, user_id)

async def process_media_request(message, text: str, user_id: int) -> bool:
    urls = re.findall(r'(https?://\S+)', text)
    if not urls:
        return False

    if len(urls) > 1:
        as_files = bool(re.search(r'\s-f\b', text))
        flag_suffix = " -f" if as_files else ""
        status_msg = await message.reply_text(f"📋 Found {len(urls)} links! Processing them one by one...", quote=True)
        succeeded = 0
        failed = 0
        for idx, single_url in enumerate(urls, 1):
            single_text = f"{single_url}{flag_suffix}"
            success = await process_single_link(message, single_url, single_text, user_id)
            if success:
                succeeded += 1
            else:
                failed += 1
            await asyncio.sleep(1)

        try:
            await status_msg.delete()
        except Exception:
            pass
        return succeeded > 0

    return await process_single_link(message, urls[0], text, user_id)

async def process_single_link(message, url: str, text: str, user_id: int) -> bool:
    as_files = bool(re.search(r'\s-f\b', text))

    clean_text = text.replace(url, '').replace('-f', '').strip()
    
    # Extract numbers and ranges for indices
    media_indices = set()
    for token in clean_text.split():
        if re.match(r'^\d+$', token):
            media_indices.add(int(token))
        elif re.match(r'^\d+-\d+$', token):
            try:
                start, end = map(int, token.split('-'))
                if start <= end and end - start < 100:
                    media_indices.update(range(start, end + 1))
            except:
                pass
                
    media_indices = sorted(list(media_indices)) if media_indices else None

    reply_msg = await message.reply_text("⏳ Processing link...", quote=True)

    # Fast zero-bandwidth passthrough for Twitter / X links
    is_twitter = ('twitter.com' in url or 'x.com' in url)
    if is_twitter and not as_files:
        try:
            tw_data = await asyncio.to_thread(fetch_twitter_details, url)
            if tw_data == "ERROR_REGION_BLOCKED":
                add_failed_download(user_id, url, text, "Region blocked on Twitter")
                record_download_history(user_id, url=url, status="failed", error="Region blocked on Twitter", message_id=message.message_id)
                try:
                    await reply_msg.delete()
                except Exception:
                    pass
                return False
            if tw_data and tw_data.get('items'):
                items = tw_data['items']
                if media_indices:
                    filtered = []
                    for idx in media_indices:
                        if 1 <= idx <= len(items):
                            filtered.append(items[idx - 1])
                    if filtered:
                        items = filtered

                title = tw_data.get('title', '')
                caption_parts = []
                if title:
                    safe_title = html.escape(title[:300])
                    caption_parts.append(f"📌 <b>{safe_title}</b>")

                tweet_link = tw_data.get("tweet_url", url)
                escaped_tw_href = html.escape(tweet_link, quote=True)
                escaped_tw_display = html.escape(tweet_link if len(tweet_link) <= 65 else tweet_link[:62] + "...")
                caption_parts.append(f'🔗 <a href="{escaped_tw_href}">{escaped_tw_display}</a>')

                # Single photo via URL passthrough
                if len(items) == 1 and items[0]['type'] == 'image':
                    item = items[0]
                    caption_parts.append(f'📥 <a href="{html.escape(item["full_res"], quote=True)}">Direct Full-Res File</a>')
                    caption = "\n\n".join(caption_parts)
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📥 Direct Full-Res File", url=item['full_res'])]
                    ])
                    await message.reply_photo(
                        photo=item['url'],
                        caption=caption,
                        parse_mode='HTML',
                        reply_markup=keyboard,
                        quote=True,
                        read_timeout=300,
                        write_timeout=300
                    )
                    track_stat(user_id, "downloads", url=url)
                    remove_failed_download(user_id, url)
                    record_download_history(user_id, url=url, status="success", title=title, message_id=message.message_id)
                    try:
                        await reply_msg.delete()
                    except:
                        pass
                    return True

                # Single video via URL passthrough
                elif len(items) == 1 and items[0]['type'] in ('video', 'gif'):
                    item = items[0]
                    caption_parts.append(f'📥 <a href="{html.escape(item["full_res"], quote=True)}">Direct Full-Res File</a>')
                    caption = "\n\n".join(caption_parts)
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📥 Direct Full-Res File", url=item['full_res'])]
                    ])
                    v_kwargs = {'supports_streaming': True, 'read_timeout': 300, 'write_timeout': 300, 'quote': True}
                    if item.get('width'): v_kwargs['width'] = item['width']
                    if item.get('height'): v_kwargs['height'] = item['height']
                    try:
                        await message.reply_video(
                            video=item['url'],
                            caption=caption,
                            parse_mode='HTML',
                            reply_markup=keyboard,
                            **v_kwargs
                        )
                        track_stat(user_id, "downloads", url=url)
                        remove_failed_download(user_id, url)
                        record_download_history(user_id, url=url, status="success", title=title, message_id=message.message_id)
                        try:
                            await reply_msg.delete()
                        except:
                            pass
                        return True
                    except Exception as e:
                        logger.warning(f"Direct video URL send failed (likely >20MB limit): {e}, falling back to yt-dlp")

                # Photo album via URL passthrough
                elif all(m['type'] == 'image' for m in items):
                    try:
                        media_group = []
                        for idx, m in enumerate(items):
                            if idx == 0:
                                cap = "\n\n".join(caption_parts)
                                media_group.append(InputMediaPhoto(media=m['url'], caption=cap, parse_mode='HTML'))
                            else:
                                media_group.append(InputMediaPhoto(media=m['url']))

                        for chunk in chunk_list(media_group, 10):
                            await message.reply_media_group(media=chunk, quote=True, read_timeout=300, write_timeout=300)

                        buttons = []
                        for idx, m in enumerate(items):
                            buttons.append(InlineKeyboardButton(f"📥 Photo {idx+1} Full-Res", url=m['full_res']))
                        btn_rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
                        ctrl_caption = f"📸 <b>Album ({len(items)} photos)</b> • Tap below for uncompressed files:"
                        await message.reply_text(ctrl_caption, reply_markup=InlineKeyboardMarkup(btn_rows), parse_mode='HTML', quote=True)

                        track_stat(user_id, "downloads", url=url)
                        remove_failed_download(user_id, url)
                        record_download_history(user_id, url=url, status="success", title=title, message_id=message.message_id)
                        try:
                            await reply_msg.delete()
                        except:
                            pass
                        return True
                    except Exception as e:
                        logger.warning(f"Direct album send failed: {e}, falling back to yt-dlp")
        except Exception as e:
            logger.warning(f"Zero-bandwidth Twitter extraction failed: {e}, falling back to standard downloader")

    try:
        files, title, original_url = await download_media(url, media_indices, as_files=as_files)
        if not files:
            add_failed_download(user_id, url, text, "No media found")
            record_download_history(user_id, url=url, status="failed", error="No media found", message_id=message.message_id)
            try:
                await reply_msg.delete()
            except Exception:
                pass
            return False

        # Build caption
        caption_parts = []
        if title:
            safe_title = html.escape(title[:300])
            caption_parts.append(f"📌 <b>{safe_title}</b>")
        
        escaped_href = html.escape(original_url, quote=True)
        display_url = original_url if len(original_url) <= 65 else original_url[:62] + "..."
        escaped_display = html.escape(display_url)
        caption_parts.append(f'🔗 <a href="{escaped_href}">{escaped_display}</a>')
        caption = "\n".join(caption_parts)

        if len(files) == 1:
            f = files[0]
            try:
                if os.path.exists(f) and os.path.getsize(f) > 0:
                    if os.path.getsize(f) > 50 * 1024 * 1024:
                        await reply_msg.edit_text("⏳ File is over 50MB! Uploading the uncompressed original to Gofile...")
                        cloud_link = upload_to_gofile(f)
                        if cloud_link:
                            try:
                                await reply_msg.delete()
                            except:
                                pass
                            await message.reply_text(f"{caption}\n\n✅ File was too large for Telegram, so I uploaded it here:\n\n🔗 {cloud_link}", disable_web_page_preview=True, parse_mode='HTML', quote=True)
                            track_stat(user_id, "downloads", url=original_url)
                            remove_failed_download(user_id, url)
                            remove_failed_download(user_id, original_url)
                            record_download_history(user_id, url=original_url, status="success", title=title, message_id=message.message_id)
                            return True
                        else:
                            add_failed_download(user_id, url, text, "File >50MB and cloud upload failed")
                            record_download_history(user_id, url=url, status="failed", error="File >50MB and cloud upload failed", message_id=message.message_id)
                            try:
                                await reply_msg.delete()
                            except Exception:
                                pass
                            return False
                            
                    with open(f, 'rb') as doc:
                        if as_files:
                            await message.reply_document(document=doc, caption=caption, parse_mode='HTML', quote=True, read_timeout=300, write_timeout=300)
                        elif f.endswith(('.mp4', '.webm', '.mkv', '.gif')):
                            w, h = get_video_dimensions(f)
                            await message.reply_video(video=doc, caption=caption, parse_mode='HTML', quote=True, width=w, height=h, supports_streaming=True, read_timeout=300, write_timeout=300)
                        elif f.endswith(('.jpg', '.png', '.jpeg')):
                            await message.reply_photo(photo=doc, caption=caption, parse_mode='HTML', quote=True, read_timeout=300, write_timeout=300)
                        else:
                            await message.reply_document(document=doc, caption=caption, parse_mode='HTML', quote=True, read_timeout=300, write_timeout=300)
            except Exception as e:
                logger.error(f"Failed to send file: {e}")
                if ("timed out" in str(e).lower() or "timeout" in str(e).lower()) and not as_files:
                    try:
                        logger.info("Upload timed out, retrying send as document...")
                        with open(f, 'rb') as doc:
                            await message.reply_document(document=doc, caption=caption, parse_mode='HTML', quote=True, read_timeout=300, write_timeout=300)
                        track_stat(user_id, "downloads", url=original_url)
                        remove_failed_download(user_id, url)
                        remove_failed_download(user_id, original_url)
                        record_download_history(user_id, url=original_url, status="success", title=title, message_id=message.message_id)
                        try:
                            await reply_msg.delete()
                        except:
                            pass
                        return True
                    except Exception as retry_e:
                        logger.error(f"Retry as document failed: {retry_e}")
                add_failed_download(user_id, url, text, f"Failed to send: {e}")
                record_download_history(user_id, url=url, status="failed", error=str(e), message_id=message.message_id)
                try:
                    await reply_msg.delete()
                except Exception:
                    pass
                return False
            finally:
                if os.path.exists(f):
                    os.remove(f)
            track_stat(user_id, "downloads", url=original_url)
            remove_failed_download(user_id, url)
            remove_failed_download(user_id, original_url)
            record_download_history(user_id, url=original_url, status="success", title=title, message_id=message.message_id)
            try:
                await reply_msg.delete()
            except:
                pass
            return True
        else:
            try:
                await send_album(message, files, as_files, caption)
                track_stat(user_id, "downloads", url=original_url)
                remove_failed_download(user_id, url)
                remove_failed_download(user_id, original_url)
                record_download_history(user_id, url=original_url, status="success", title=title, message_id=message.message_id)
                try:
                    await reply_msg.delete()
                except:
                    pass
                return True
            except Exception as e:
                logger.error(f"Failed to send album: {e}")
                add_failed_download(user_id, url, text, f"Failed to send album: {e}")
                record_download_history(user_id, url=url, status="failed", error=str(e), message_id=message.message_id)
                try:
                    await reply_msg.delete()
                except Exception:
                    pass
                return False
            finally:
                for f in files:
                    if os.path.exists(f):
                        os.remove(f)
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download failed: {e}")
        error_msg = e.message if hasattr(e, 'message') else str(e)
        if len(error_msg) > 800:
            error_msg = error_msg[:800] + "... [Truncated]"
        add_failed_download(user_id, url, text, f"Download failed: {error_msg[:120]}")
        record_download_history(user_id, url=url, status="failed", error=str(error_msg)[:120], message_id=message.message_id)
        try:
            await reply_msg.delete()
        except Exception:
            pass
        return False
    except Exception as e:
        import traceback
        logger.error(f"Error processing link: {e}\n{traceback.format_exc()}")
        add_failed_download(user_id, url, text, f"Error: {e}")
        record_download_history(user_id, url=url, status="failed", error=str(e), message_id=message.message_id)
        try:
            await reply_msg.delete()
        except Exception:
            pass
        return False

async def handle_replied_link(message, reply_to, url: str, user_text: str, user_id: int):
    """Inspects a link the user replied to, checks past success/failure, and takes action."""
    # 1. Check if user explicitly wants to force download (typed "retry", "download", "-f", etc.)
    is_force_download = bool(re.search(r'\b(retry|redownload|download|dl|again|-f)\b', user_text.lower()))
    
    # 2. Check retry queue
    failed_items = get_failed_queue(user_id)
    clean_url = url.split('?')[0]
    failed_match = next((item for item in failed_items if item.get('url') == url or item.get('url', '').split('?')[0] == clean_url), None)
    
    # 3. Check download history
    history_item = get_download_history_item(user_id, url)
    
    # 4. Check reply_to message itself
    is_reply_to_bot = bool(reply_to.from_user and reply_to.from_user.is_bot)
    reply_to_text = (reply_to.text or reply_to.caption or "").lower()
    
    is_bot_error = is_reply_to_bot and any(k in reply_to_text for k in ['timed out', 'could not download', 'failed to send', '❌', 'error'])
    is_bot_success = is_reply_to_bot and (bool(reply_to.video or reply_to.photo or reply_to.document) or 'gofile.io' in reply_to_text or '✅' in reply_to_text)
    
    has_succeeded = False
    success_title = None
    success_time = None
    
    if history_item and history_item.get('status') == 'success':
        has_succeeded = True
        success_title = history_item.get('title')
        success_time = history_item.get('time_display')
    elif is_bot_success:
        has_succeeded = True
        success_title = (reply_to.caption or "").split('\n')[0] if reply_to.caption else None
        
    has_failed = False
    failure_reason = None
    if failed_match:
        has_failed = True
        failure_reason = failed_match.get('reason')
    elif is_bot_error:
        has_failed = True
        failure_reason = reply_to.text.split('\n')[0] if reply_to.text else "Bot reported an error"
    elif history_item and history_item.get('status') == 'failed':
        has_failed = True
        failure_reason = history_item.get('error')

    # If it succeeded and the user did NOT explicitly request a re-download:
    if has_succeeded and not is_force_download:
        token = uuid.uuid4().hex[:10]
        if len(redl_cache) > 200:
            redl_cache.pop(next(iter(redl_cache)))
        redl_cache[token] = {"url": url, "user_id": user_id}
        
        title_display = html.escape(success_title[:250]) if success_title else "Media item"
        escaped_url = html.escape(url, quote=True)
        disp_url = html.escape(url if len(url) <= 65 else url[:62] + "...")
        time_display = f" ({html.escape(success_time)})" if success_time else ""
        
        reply_card = (
            f"✅ <b>This link was already completed!</b>\n\n"
            f"📌 <b>Title:</b> {title_display}\n"
            f"🔗 <b>Link:</b> <a href=\"{escaped_url}\">{disp_url}</a>\n"
            f"🕒 <b>Delivered:</b> Successfully completed{time_display}.\n\n"
            f"<i>Tap below if you want to re-download or get it as an uncompressed file:</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Download Again", callback_data=f"redl_std_{token}")],
            [InlineKeyboardButton("📥 Download as File (-f)", callback_data=f"redl_doc_{token}")]
        ])
        await message.reply_text(reply_card, reply_markup=keyboard, parse_mode="HTML", quote=True)
        return

    # Otherwise: It failed, or was not completed, or user requested re-download.
    # Take it from there!
    if has_failed:
        status_note = f"⚠️ <b>Previous attempt failed:</b> <i>{html.escape(str(failure_reason)[:200])}</i>\n\n"
    elif is_force_download:
        status_note = "🔄 <b>Re-download requested!</b>\n\n"
    else:
        status_note = "ℹ️ <b>Link predates recent history:</b>\n<i>Fetching media now to ensure it is delivered and logged in your history...</i>\n\n"
        
    status_msg = await message.reply_text(
        f"{status_note}🚀 <b>Taking it from here... Downloading now!</b>",
        parse_mode="HTML",
        quote=True
    )
    
    clean_user_opts = user_text.replace(url, '').strip()
    req_text = f"{url} {clean_user_opts}".strip()
    success = await process_media_request(message, req_text, user_id)
    try:
        await status_msg.delete()
    except Exception:
        pass

async def redl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Fetching media...")
    data = query.data
    parts = data.split('_', 2)
    mode = parts[1] if len(parts) > 1 else 'std'
    token = parts[2] if len(parts) > 2 else ''
    
    url_info = redl_cache.get(token)
    if not url_info:
        await query.message.reply_text("⚠️ This download session expired. Please reply to the link again.", quote=True)
        return
        
    url = url_info.get('url')
    as_files = (mode == 'doc')
    req_text = f"{url} -f" if as_files else url
    user_id = query.from_user.id
    
    await process_media_request(query.message, req_text, user_id)

async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    message = update.message
    reply_to = message.reply_to_message

    # If replying to a message with a link while calling /retry, retry that specific link!
    if reply_to:
        reply_urls = extract_urls_from_message(reply_to)
        if reply_urls:
            target_url = reply_urls[0]
            await handle_replied_link(message, reply_to, target_url, "retry", user_id)
            return

    args = context.args
    if args and args[0].lower() in ['clear', 'reset']:
        clear_failed_queue(user_id)
        await update.message.reply_text("🧹 Cleared your failed retry list.", quote=True)
        return
    
    if args and args[0].lower() in ['list', 'show']:
        items = get_failed_queue(user_id)
        if not items:
            await update.message.reply_text("✨ You have no failed downloads queued.", quote=True)
            return
        lines = [f"📋 <b>Failed Downloads ({len(items)}):</b>\n"]
        for idx, item in enumerate(items, 1):
            url_display = item.get('url', '')
            reason = item.get('reason', 'Failed')
            time_str = item.get('time', '')
            lines.append(f"{idx}. 🔗 {html.escape(url_display)}\n   ⚠️ <i>{html.escape(reason)}</i> ({html.escape(time_str)})")
        lines.append("\n<i>Send /retry to attempt downloading these again, or /retry clear to reset.</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True, quote=True)
        return

    items = get_failed_queue(user_id)
    if not items:
        await update.message.reply_text("✨ You have no failed downloads to retry!", quote=True)
        return

    status_msg = await update.message.reply_text(f"🔄 Retrying {len(items)} failed download(s)...", quote=True)
    succeeded = 0
    failed = 0
    for item in list(items):
        raw_text = item.get('raw_text') or item.get('url')
        success = await process_media_request(update.message, raw_text, user_id)
        if success:
            succeeded += 1
            remove_failed_download(user_id, item.get('url'))
        else:
            failed += 1
        await asyncio.sleep(1)
    
    remaining = get_failed_queue(user_id)
    summary = f"🏁 <b>Retry Complete:</b>\n✅ Succeeded: {succeeded}\n❌ Still failing: {failed}"
    if remaining:
        summary += f"\n\n<i>Use /retry list to view remaining failures, or /retry clear to discard them.</i>"
    await status_msg.edit_text(summary, parse_mode="HTML")

async def clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes all messages sent by the bot in the chat while keeping user's link messages intact."""
    message = update.message
    if not message:
        return
    user_id = message.from_user.id
    current_msg_id = message.message_id
    bot = context.bot

    status_msg = await message.reply_text("🧹 Cleaning up bot messages... Your links will remain untouched.", quote=True)

    deleted_count = 0
    # Sweep backwards through up to 300 recent message IDs
    for msg_id in range(current_msg_id - 1, max(0, current_msg_id - 300), -1):
        try:
            await bot.delete_message(chat_id=user_id, message_id=msg_id)
            deleted_count += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass

    await status_msg.edit_text(f"✅ Cleaned up {deleted_count} bot responses! Your original links are preserved.")
    await asyncio.sleep(4)
    try:
        await status_msg.delete()
        await message.delete()
    except Exception:
        pass

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

    if data.startswith("nh_dl_"):
        code = data.split("_")[2]
        
        # Don't fail if caption doesn't exist
        original_caption = query.message.caption or ""
        await query.edit_message_caption(caption=original_caption + "\n\n⏳ Downloading pages...")
        
        try:
            from curl_cffi import requests as c_requests
            from bs4 import BeautifulSoup
            
            r = await asyncio.to_thread(c_requests.get, f'https://nhentai.net/g/{code}/', impersonate='chrome110', timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            thumbs = soup.select('.gallerythumb img')
            if not thumbs:
                await query.edit_message_caption(caption=original_caption + "\n❌ Failed to find pages.")
                return
                
            image_urls = []
            for img in thumbs:
                thumb_url = img.get('data-src') or img.get('src')
                if thumb_url:
                    full_url = re.sub(r'//t\d*\.', '//i.', thumb_url).replace('t.', '.')
                    image_urls.append(full_url)
                    
            cbz_dir = f"downloads/nh_{code}"
            os.makedirs(cbz_dir, exist_ok=True)
            
            async def fetch_page(idx, url, sem):
                async with sem:
                    img_res = await asyncio.to_thread(c_requests.get, url, impersonate='chrome110', timeout=15)
                    if img_res.status_code == 200:
                        ext = url.split('.')[-1]
                        with open(f"{cbz_dir}/{idx:03d}.{ext}", 'wb') as f:
                            f.write(img_res.content)
                            
            sem = asyncio.Semaphore(15)
            tasks = [fetch_page(i, url, sem) for i, url in enumerate(image_urls, 1)]
            await asyncio.gather(*tasks)
            
            cbz_path = f"downloads/nh_{code}.cbz"
            with zipfile.ZipFile(cbz_path, 'w') as zf:
                for f in os.listdir(cbz_dir):
                    zf.write(os.path.join(cbz_dir, f), f)
                    
            if os.path.getsize(cbz_path) > 50 * 1024 * 1024:
                await query.edit_message_caption(caption=original_caption + "\n⏳ File over 50MB, uploading to Gofile...")
                cloud_link = upload_to_gofile(cbz_path)
                if cloud_link:
                    track_stat(query.from_user.id, "downloads")
                    await query.edit_message_caption(caption=original_caption + f"\n✅ CBZ is too large for Telegram. Download here:\n🔗 {cloud_link}")
                else:
                    await query.edit_message_caption(caption=original_caption + "\n❌ Failed to upload large CBZ.")
            else:
                with open(cbz_path, 'rb') as f:
                    await context.bot.send_document(chat_id=query.message.chat_id, document=f)
                track_stat(query.from_user.id, "downloads")
                await query.edit_message_caption(caption=original_caption)
                
            import shutil
            shutil.rmtree(cbz_dir, ignore_errors=True)
            if os.path.exists(cbz_path): os.remove(cbz_path)
            
        except Exception as e:
            logger.error(f"nHentai DL Error: {e}")
            await query.edit_message_caption(caption=original_caption + "\n❌ Error downloading CBZ.")
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

async def reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward liked messages to the user's Vault."""
    reaction = update.message_reaction
    if not reaction: return
    
    # Telegram sends the red heart without the variation selector (\uFE0F), so we include both variants
    has_like = any(r.type == 'emoji' and r.emoji in ['👍', '❤️', '❤', '🔥', '⭐'] for r in reaction.new_reaction)
    if not has_like: return

    user = reaction.user
    user_id = user.id if user else reaction.chat.id
    settings = get_wishlist('settings') or {}
    user_settings = settings.get(str(user_id), {})
    vault_id = user_settings.get('vault_id')

    if vault_id:
        try:
            await context.bot.copy_message(
                chat_id=vault_id,
                from_chat_id=reaction.chat.id,
                message_id=reaction.message_id
            )
            track_stat(user_id, "vaults")
        except Exception as e:
            logger.error(f"Vault forward failed: {e}")
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Failed to forward to Vault. Make sure I am an admin in your Vault channel with the 'Post Messages' permission!"
            )
    else:
        await context.bot.send_message(
            chat_id=user_id, 
            text="You liked a message, but you haven't set up a Vault yet! Forward any message from a private Channel here to set it as your Vault."
        )

async def setvault_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates setting or changing the user's Vault channel."""
    context.user_data['awaiting_vault_setup'] = True
    await update.message.reply_text(
        "🛠️ <b>Set / Change Vault Channel</b>\n\n"
        "1. Make sure I am an Administrator in your private channel with the <b>Post Messages</b> permission.\n"
        "2. Forward any message from that channel to me right now, and I will lock it in as your Vault!",
        parse_mode='HTML'
    )

async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages forwarded from channels/chats to save media to the Vault cleanly."""
    message = update.message
    if not message:
        return

    user_id = message.from_user.id
    settings = get_wishlist('settings') or {}
    user_settings = settings.get(str(user_id), {})
    vault_id = user_settings.get('vault_id')

    origin = message.forward_origin
    text = (message.text or message.caption or "").strip()

    # Check if user explicitly wants to set or update their Vault channel
    is_vault_setup = (
        context.user_data.get('awaiting_vault_setup')
        or text.lower() == "/setvault"
        or (not vault_id and origin and origin.type in ['channel', 'chat'])
    )

    if is_vault_setup and origin and origin.type in ['channel', 'chat'] and hasattr(origin, 'chat'):
        chat_id = origin.chat.id
        if str(user_id) not in settings:
            settings[str(user_id)] = {}
        settings[str(user_id)]['vault_id'] = chat_id
        save_wishlist('settings', settings)
        context.user_data.pop('awaiting_vault_setup', None)
        chat_title = getattr(origin.chat, 'title', 'Channel')
        await message.reply_text(
            f"✅ <b>Vault set to: {chat_title}!</b>\n\n"
            "From now on, any media you forward here (or react to with 👍/❤️) will be automatically sent to your Vault as a clean, standalone copy with zero channel tags or regional blocks.",
            parse_mode='HTML'
        )
        return

    # Check if this forwarded message has media (video, photo, doc, animation, audio, voice, video note)
    has_media = bool(
        message.video
        or message.photo
        or message.document
        or message.animation
        or message.audio
        or message.voice
        or message.video_note
    )

    # If it's a text-only message with a downloadable URL (Reddit, Twitter, etc.), pass to handle_message
    if not has_media and text:
        urls = re.findall(r'(https?://\S+)', text)
        if urls:
            await handle_message(update, context)
            return

    # If no media and no text, ignore
    if not has_media and not text:
        return

    # User needs a Vault set up
    if not vault_id:
        await message.reply_text(
            "❌ You haven't set up a Vault channel yet!\n\n"
            "To set it up:\n"
            "1. Create a private channel and add this bot as an Admin (with Post Messages permission).\n"
            "2. Send any message in that channel, forward it to this bot, and it will be locked in as your Vault!"
        )
        return

    try:
        await context.bot.copy_message(
            chat_id=vault_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id
        )
        track_stat(user_id, "vaults")

        # Debounce confirmation reply for media albums (so user isn't spammed for each photo in an album)
        media_group_id = message.media_group_id
        if media_group_id:
            if context.user_data.get('last_vaulted_group') != media_group_id:
                context.user_data['last_vaulted_group'] = media_group_id
                await message.reply_text("✅ Vaulted! Clean copy sent to your Vault channel (all channel tags and regional blocks removed).")
        else:
            await message.reply_text("✅ Vaulted! Clean copy sent to your Vault channel (all channel tags and regional blocks removed).")
    except Exception as e:
        logger.error(f"Forwarded vault copy failed: {e}")
        await message.reply_text(
            "❌ Failed to send to your Vault channel.\n"
            "Make sure I am added as an Administrator in your Vault channel with the 'Post Messages' permission!"
        )

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

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not mongo_client:
        await update.message.reply_text("❌ Stats require a MongoDB database connection.")
        return
        
    doc = stats_col.find_one({"_id": str(user_id)}) or {}
    downloads = doc.get("downloads", 0)
    vaults = doc.get("vaults", 0)
    manga = doc.get("manga_downloads", 0)
    ln = doc.get("ln_downloads", 0)
    total = downloads + manga + ln
    first_seen = doc.get("first_seen", "Today")
    
    sources = doc.get("sources", {})
    source_msg = ""
    if sources:
        source_msg = "\n\n🌐 <b>Top Sources</b>\n"
        for s, c in sorted(sources.items(), key=lambda x: x[1], reverse=True)[:3]:
            source_msg += f"• {s.capitalize()}: {c}\n"
            
    subreddits = doc.get("subreddits", {})
    sub_msg = ""
    if subreddits:
        sub_msg = "\n🤖 <b>Top Subreddits</b>\n"
        for s, c in sorted(subreddits.items(), key=lambda x: x[1], reverse=True)[:3]:
            sub_msg += f"• r/{s}: {c}\n"
    
    msg = (
        f"📊 <b>Your Stats</b>\n\n"
        f"⬇️ Media downloads: <b>{downloads}</b>\n"
        f"📖 Manga chapters: <b>{manga}</b>\n"
        f"📚 Light novels: <b>{ln}</b>\n"
        f"📦 Total downloads: <b>{total}</b>\n"
        f"❤️ Vaulted items: <b>{vaults}</b>"
        f"{source_msg}{sub_msg}\n"
        f"📅 Member since: <code>{first_seen[:10]}</code>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

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
        from curl_cffi import requests as cffi_requests
        r = await asyncio.to_thread(
            cffi_requests.get, f"https://allnovelfull.com/search?keyword={query}",
            impersonate="chrome110", timeout=15
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
        from curl_cffi import requests as cffi_requests
        r = await loop.run_in_executor(None, lambda: cffi_requests.get(url, impersonate="chrome110", timeout=15))
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
            from curl_cffi import requests as cffi_requests
            r = await asyncio.to_thread(
                cffi_requests.get, search_data['link'],
                impersonate="chrome110", timeout=15
            )
            
            m = re.search(r'data-novel-id=[\'\"]([0-9]+)[\'\"]', r.text)
            if not m:
                await query.edit_message_text(f"❌ Error extracting novel ID. ({VERSION})")
                return
            novel_id = m.group(1)
            
            r_ch = await asyncio.to_thread(
                cffi_requests.get, f"https://allnovelfull.com/ajax-chapter-option?novelId={novel_id}",
                impersonate="chrome110", timeout=15
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
                track_stat(query.from_user.id, "ln_downloads")
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
                track_stat(query.from_user.id, "manga_downloads")
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

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=60.0,
        read_timeout=300.0,
        write_timeout=300.0,
        pool_timeout=60.0
    )
    application = Application.builder().token(TOKEN).request(request).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setvault", setvault_command))
    application.add_handler(CommandHandler("adminmisuse", adminmisuse))
    application.add_handler(CommandHandler("wh", wh_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("manga", manga_command))
    application.add_handler(CommandHandler("ln", ln_command))
    application.add_handler(CommandHandler("retry", retry_command))
    application.add_handler(CommandHandler(["clean", "cleanup", "purge"], clean_command))
    application.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
    application.add_handler(MessageReactionHandler(reaction_callback))
    application.add_handler(CallbackQueryHandler(wh_callback, pattern="^wh_"))
    application.add_handler(CallbackQueryHandler(wh_callback, pattern="^nh_"))
    application.add_handler(CallbackQueryHandler(manga_callback, pattern="^mg_"))
    application.add_handler(CallbackQueryHandler(ln_callback, pattern="^ln_"))
    application.add_handler(CallbackQueryHandler(redl_callback, pattern="^redl_"))

    # Cleanup orphan files every 10 minutes
    application.job_queue.run_repeating(cleanup_job, interval=600, first=600)

    # Initialize PTB
    await application.initialize()
    await application.start()
    
    is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))
    if is_render:
        # Set Telegram Webhook to point to Render
        await application.bot.set_webhook(url=f"{RENDER_URL}/webhook", allowed_updates=["message", "callback_query", "message_reaction"])

        async def health_check(request):
            return web.Response(text="Bot is running!")

        async def telegram_webhook(request):
            try:
                data = await request.json()
                update = Update.de_json(data, application.bot)
                await application.update_queue.put(update)
                return web.Response(text="OK")
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
    else:
        await application.bot.delete_webhook(drop_pending_updates=False)
        await application.updater.start_polling(allowed_updates=["message", "callback_query", "message_reaction"])
        logger.info(f"Bot {VERSION} started in POLLING mode locally!")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        if not is_render and application.updater and application.updater.running:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
