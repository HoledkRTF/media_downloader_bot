## Recent Codenames
- **Aether-Loop** (v2.9.0): Automated GIF-to-MP4 transcoding via FFmpeg, native Telegram send_animation routing, and album media group duration fix preventing 0-second unplayable video clips.
- **Zenith-Pulse** (v2.8.0): Fixed Message.reply_text quote parameter crash, untracked dead test dumps, deduplicated download success handlers, hardened MangaKatana dynamic regex and ID routing, and accelerated manga downloads via concurrent page fetching.
- **Phantom-Relay** (v2.7.2): Non-blocking webhook dispatch via asyncio.create_task for on-demand cold start execution, on-demand spin-down, and /logs monitoring endpoint.
- **Silent-Shadow** (v2.6.0): Silent failure handling — when downloads fail, they are automatically queued to the persistent retry list/database and their temporary status messages are deleted from chat instead of displaying ugly error cards.
- **Vortex-Bypass** (v2.5.0): Added automated Reddit age-gate bypass via embedded video resolver (recovering RedGifs/external media without account requirement), REDDIT_SESSION cookie support for native 18+ Reddit videos, and default 720p format selector saving up to 84% internet bandwidth.
