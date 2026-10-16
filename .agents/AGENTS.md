## Recent Codenames
- **Aegis-Purge** (v2.4.0): Added /clean command to sweep and delete bot responses, media, and error cards while strictly preserving user link messages intact; added sequential multi-link batch processing to download multiple URLs one by one in order.
- **Echo-Trace** (v2.3.0): Intelligent reply inspection allowing users to swipe-reply to any previous link or error message; automatically checks whether it had a reply or failed, reports historical status with re-download options, and seamlessly takes over to download/deliver uncompleted items.
- **Prism-Quote** (v2.2.1): Added Telegram quote replies across all media and error responses, explicit URL attribution in captions, and link context on failures so users can visually trace every downloaded file to its source link.
- **Onyx-Retry** (v2.2.0): Added persistent failed download queue with /retry command to re-attempt failed downloads, /retry list to view, and /retry clear to reset.
- **Titan-Upload** (v2.1.1): Extended HTTPX timeouts to 300s, added document upload retry on network timeout, and persistent local runner.
