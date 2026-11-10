# Elephant-Goldfish Model (EGM) Codebase Review Report

**Date**: 2026-11-09  
**Review Target**: Workspace Codebase (`media_downloader_bot`, `twitter-curation-bot`, `kurotok`, `sankaku-cacher`)  
**Methodology**: Tri-lens Adversarial Goldfish Swarm (`egm-zoom-out`, `egm-codebase-details`, `egm-design-critic`) running in isolated, stateless read-only contexts without conversation history.

---

## Executive Summary

The Goldfish review identified six critical blockers, eight major reliability and architectural defects, and six minor issues across the codebase. 

While core video downloading functions adequately under normal single-link loads, the codebase suffers from severe monolithic coupling, resource leaks, unauthenticated public endpoints, and global executor starvation risks under concurrent or scraping operations.

---

## Review Lens 1: Critical Blockers

### 1. Multi-Image Twitter Album NameError Crash
* **Lens**: `egm-codebase-details`
* **Scope**: `media_downloader_bot/bot.py`, Line 1255 (`_process_single_link_impl`)
* **Finding**: Multi-image Twitter album passthrough attempts to construct the first image caption using `cap = "\n\n".join(caption_parts)`. `caption_parts` is an undeclared variable in this scope. Every multi-image Twitter request crashes with `NameError: name 'caption_parts' is not defined` and aborts to yt-dlp fallback.
* **Remediation**: Replace line 1255 with `cap = caption`, reusing the already-computed caption from line 1188.

### 2. Leaked Transcoded Animation Files on Disk (`anim_*.mp4`)
* **Lens**: `egm-codebase-details`
* **Scope**: `media_downloader_bot/bot.py`, Lines 918, 1372–1388 (`send_album` & `_process_single_link_impl`)
* **Finding**: In `send_album`, `files = list(ex.map(convert_gif_to_mp4, files))` locally rebinds the parameter. The caller's `finally:` block retains the original `files` list containing stale `.gif` paths and attempts to remove non-existent `.gif` files. The newly generated `anim_*.mp4` files are never tracked or deleted, permanently leaking disk space. Inspection confirmed 20 orphaned `anim_reddit_*.mp4` files sitting in `downloads/`.
* **Remediation**: Mutate the caller list in place (`converted = list(...); files.clear(); files.extend(converted)`), ensuring the caller's `finally:` block sees and deletes all converted `.mp4` artifacts.

### 3. Batch Downloader Crash on Animated Media
* **Lens**: `egm-codebase-details`
* **Scope**: `media_downloader_bot/batch_downloader.py`, Lines 18–93 (`MessageProxy`)
* **Finding**: `MessageProxy` implements `reply_photo`, `reply_video`, and `reply_document`, but lacks `reply_animation`. Handling any Reddit or Twitter animation invokes `await message.reply_animation(...)` and crashes with `AttributeError`.
* **Remediation**: Implement `async def reply_animation(self, animation, **kwargs)` on `MessageProxy` delegating to `self.bot.send_animation`.

### 4. Global Threadpool Starvation via Monolithic Executor
* **Lens**: `egm-design-critic`
* **Scope**: Concurrency & Worker Architecture (`bot.py`, `asyncio.to_thread` / `run_in_executor`)
* **Finding**: All blocking operations—AllNovelFull chapter scraping, MangaKatana downloads, Twitter scraping, yt-dlp execution, and FFmpeg transcode jobs—share Python's default `ThreadPoolExecutor` via `asyncio.to_thread`. On Render free tier (1 vCPU), executor capacity defaults to 5 workers. A single `/ln` request fetching 500+ chapters floods the pool with `Semaphore(20)` tasks, completely locking out interactive video downloads for all bot users until the scraping job finishes or times out.
* **Remediation**: Decouple executors. Create a dedicated, bounded `ThreadPoolExecutor(max_workers=3)` specifically for novel/manga scrapers, isolating them from interactive media downloads.

### 5. Unauthenticated Public Webhook Endpoint
* **Lens**: `egm-design-critic`
* **Scope**: `media_downloader_bot/bot.py`, Lines 2594, 2607–2615 (`set_webhook`, `telegram_webhook`)
* **Finding**: `set_webhook` does not pass a `secret_token`, and the public aiohttp route `/webhook` performs zero authentication on incoming requests. Any external entity who discovers the Render URL can POST arbitrary JSON payloads, spoofing updates from any Telegram user ID to trigger downloads or wipe chat history.
* **Remediation**: Supply `secret_token=SECRET_TOKEN` during `set_webhook`, and reject incoming POST requests where `request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET_TOKEN`.

### 6. Exposed Production Secrets & User Session on Disk
* **Lens**: `egm-zoom-out`
* **Scope**: 
  * `media_downloader_bot/chat_sync.py`, `purge_chat.py`, `telethon_auth.py`
  * `media_downloader_bot/check_deploy_logs.py`, `check_render_accounts.py`, `deploy_bot.py`
  * `media_downloader_bot/telethon_user.session`
* **Finding**: Plaintext Render API keys (`rnd_...`), Telegram API credentials (`API_ID`, `API_HASH`), personal phone numbers, and an unencrypted SQLite user session database (`telethon_user.session`) holding decrypted Telegram auth keys are stored unencrypted in repository directories.
* **Remediation**: Revoke and rotate both Render API keys immediately. Move credentials to `.env`. Add `*.session`, `*.session-journal`, and credential files to `.gitignore`.

---

## Review Lens 2: Major Architectural & Reliability Hazards

### 7. Unbounded Heap Allocation and OOM Kill on Light Novels
* **Lens**: `egm-design-critic` & `egm-codebase-details`
* **Scope**: `media_downloader_bot/bot.py`, Lines 2135–2261 (`_fetch_ln_chapter`, `ln_callback`)
* **Finding**: `/ln` downloads all chapters simultaneously without pagination or disk spooling (`results = await asyncio.gather(*tasks)`). Holding HTML strings for 1,000+ chapters in Python memory before passing them into EbookLib easily consumes 300MB–800MB of RAM, causing Render's 512MB container ceiling to trigger an immediate `SIGKILL` 137 exit.
* **Remediation**: Stream chapter files directly to temporary disk storage in batches (e.g. 25 chapters at a time) rather than holding entire novels in heap RAM, and enforce a maximum chapter cap per volume.

### 8. Synchronous Event Loop Blocking Operations
* **Lens**: `egm-codebase-details`
* **Scope**: `media_downloader_bot/bot.py`, Lines 893–910 (`upload_to_gofile`), 848–863 (`ffprobe`), 1816 (`zipfile`), 2261 (`epub.write_epub`)
* **Finding**: 120-second Gofile HTTP file uploads, synchronous `subprocess.run(ffprobe)` calls, zip archive creation, and EPUB compilation execute directly on the asyncio event loop thread. A single large upload freezes the server for up to two minutes, dropping health checks and triggering Render restart cycles.
* **Remediation**: Wrap all file uploads, ffprobe subprocesses, zip creation, and EPUB writes in `await asyncio.to_thread(...)`.

### 9. Catastrophic Database Data Loss on Read Errors
* **Lens**: `egm-codebase-details`
* **Scope**: `media_downloader_bot/bot.py`, Lines 1696–1705 (`wh_callback`)
* **Finding**: In `wh_callback`, `wishlist = get_wishlist(user_id)`. If MongoDB encounters a transient network glitch or timeout, `get_wishlist` returns an empty list `[]`. The handler then executes `wishlist = [i for i in wishlist if i['code'] != code]` (still `[]`) and calls `save_wishlist(user_id, [])`, permanently wiping the user's entire saved wishlist in the database.
* **Remediation**: Have `get_wishlist` return `None` (or raise) on database connection errors. Abort the mutation immediately if `wishlist is None`.

### 10. Path Traversal in CBZ Archive Construction
* **Lens**: `egm-design-critic`
* **Scope**: `media_downloader_bot/bot.py`, Lines 2445–2545 (`manga_callback`)
* **Finding**: In `manga_callback`, `manga_id, ch_suffix = cb_data.split("::")`. `cbz_filename = f"downloads/{safe_title}_{ch_suffix}.cbz"`. While `manga_id` is sanitized, `ch_suffix` is concatenated verbatim without validation. A forged callback payload (`mg_dl_naruto::../../target`) resolves outside `downloads/`, causing `finally: os.remove(cbz_filename)` to delete target files outside the temporary directory.
* **Remediation**: Apply `os.path.basename` and regex alphanumeric sanitization to `ch_suffix`, verifying the target path resides within `downloads/`.

### 11. Monolithic 2,654-Line Single-File God Object
* **Lens**: `egm-zoom-out`
* **Scope**: `media_downloader_bot/bot.py`
* **Finding**: Seven independent domains are packed into a single 2,654-line file: Telegram routing, social media extraction (Reddit, Twitter, TikTok, Instagram, RedGifs), doujin bookmarking, MangaKatana scraping, AllNovelFull EPUB generation, aiohttp webhook server, and dual-store MongoDB/JSON persistence. A defect in novel scraping or manga pagination introduces regression risk to core media downloading.
* **Remediation**: Modularize into `bot.py` (entry point/routing), `extractors/` (media providers), `services/` (manga, novels, wishlist), and `storage/` (database abstractions).

### 12. Storage Hijacking & Shared Document Race Conditions
* **Lens**: `egm-zoom-out` & `egm-design-critic`
* **Scope**: `media_downloader_bot/bot.py`, Lines 1908–1910, 1951–1970
* **Finding**: Telegram Vault configuration hijacked the doujin `wishlist` collection by saving user configuration dictionaries under a magic pseudo user ID `"settings"`. Concurrent updates by multiple users perform read-modify-write on this single document, overwriting each other's channel settings.
* **Remediation**: Isolate settings into a dedicated `user_settings` collection keyed by user ID (`_id: str(user_id)`).

### 13. Memory Spike from Unbounded Log Reads
* **Lens**: `egm-codebase-details`
* **Scope**: `media_downloader_bot/bot.py`, Lines 2617–2622 (`get_logs`), Line 28 (`FileHandler`)
* **Finding**: `get_logs` reads the entire `bot.log` file into memory before slicing `[-5000:]`. After extended runtime where `bot.log` reaches tens of megabytes, this causes sudden heap spikes on Render. Additionally, `bot.log` has no file rotation configured.
* **Remediation**: Use `f.seek(max(0, file_size - 5000))` to read only trailing bytes, and switch to `RotatingFileHandler(maxBytes=5*1024*1024, backupCount=3)`.

### 14. Workspace Pollution and Redundant Scrapers
* **Lens**: `egm-zoom-out`
* **Scope**: Repository Root & `kurotok/`
* **Finding**: 22 standalone Python scripts exist in the repository root (`add_chikan.py`, `crawl_to_500.py`, `harvest_massive.py`, etc.). Multiple scripts duplicate identical Jable video scraping and regex logic. Four copies of the Next.js application exist (`kurotok/main`, `staging`, `dev`, `features`) alongside 30+ backup zip files and raw screenshots.
* **Remediation**: Move one-off crawler scripts into an `archive/` folder, consolidate Jable scraping into a single modular utility, and purge loose zip backups and images from source trees.

---

## Review Lens 3: Minor Technical Nits

1. **In-Flight Lock Return Code Inconsistency**: `process_single_link` returns `True` on in-flight collisions, which causes `retry_command` to prematurely remove pending failed items from the retry queue.
2. **Missing doujin cover cleanup**: `downloads/nh_{code}.jpg` and `wh_{code}.jpg` lack `try...finally` guards, leaving uncleaned images if Telegram delivery encounters an exception.
3. **Missing yt-dlp network boundary**: `ydl_opts` lacks `socket_timeout`, allowing hung CDN sockets to block worker threads indefinitely.
4. **Duplicate in-function imports**: Multiple modules (`web`, `epub`, `InlineKeyboardMarkup`) are imported inside function scopes rather than at module top level.
5. **Telethon missing from requirements**: `telethon` is imported by three auxiliary scripts but omitted from `requirements.txt`.
6. **Hardcoded admin passcode**: `adminmisuse` gates profile picture mutation with `"69420"` instead of checking authorized Telegram user IDs.

---

## Recommended Action Plan

| Priority | Task | Target Files |
| :--- | :--- | :--- |
| **P0 (Immediate)** | Fix `caption_parts` NameError bug | `media_downloader_bot/bot.py` |
| **P0 (Immediate)** | Fix `send_album` list rebinding to stop `anim_*.mp4` disk leaks | `media_downloader_bot/bot.py` |
| **P0 (Immediate)** | Add `reply_animation` to `MessageProxy` | `media_downloader_bot/batch_downloader.py` |
| **P1 (Security)** | Add `secret_token` validation to `/webhook` | `media_downloader_bot/bot.py` |
| **P1 (Security)** | Rotate Render API keys and add `.session` to `.gitignore` | `media_downloader_bot/` |
| **P1 (Reliability)** | Offload Gofile, ffprobe, and zip operations to `asyncio.to_thread` | `media_downloader_bot/bot.py` |
| **P1 (Reliability)** | Isolate dedicated thread executor for novel/manga scrapers | `media_downloader_bot/bot.py` |
| **P2 (Data Safety)** | Protect `wh_callback` against empty wishlist overwrite on DB errors | `media_downloader_bot/bot.py` |
| **P2 (Memory)** | Implement seek-based log tailing and RotatingFileHandler | `media_downloader_bot/bot.py` |
