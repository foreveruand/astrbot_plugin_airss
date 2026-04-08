# Changelog

All notable changes to this project will be documented in this file.

## [1.4.2] - 2026-04-08

### Fixed
- Article retention now uses article publish time when available
  - Cleanup no longer deletes recently fetched but historically published articles based only on `fetched_at`
  - RSS fetch now skips saving articles that already fall outside `article_retention_days`
  - When feeds do not provide `published_at`, retention falls back to `fetched_at`

### Added
- AI 摘要新增 `ai_digest_recent_days` 配置项
  - 可限制仅摘要最近 X 天内更新的文章
  - 与 `ai_digest_max_articles` 配合使用，先按时间过滤，再按文章数量截取
  
## [1.4.1] - 2026-04-07

### Fixed
- **Database locking (`database is locked`)**: root-cause fix by replacing per-method
  short-lived `aiosqlite.connect()` calls (28 occurrences) with a single persistent
  shared connection opened in `init_db()`. All operations are now serialized through
  aiosqlite's internal background thread — concurrent RSS fetches can no longer
  compete for the SQLite write lock.
  - Removed retry loop in `add_article()` (no longer needed)
  - Added `Database.close()`, called from `Main.terminate()` for clean shutdown
- **Digest log showing wrong article count**: log now reports the actual number of
  articles processed by the LLM (post-trim), not the total fetched count.
  `generate_digest()` returns `(text, count)` tuple; caller unpacks `trimmed_count`.

## [1.4.0] - 2026-04-07

### Changed
- Configuration schema restructured with hierarchical sections (following astrbot_plugin_opencode pattern)
  - `ai_config`: AI digest settings (provider, fallback providers, token limits, etc.)
  - `fetch_config`: RSS fetch settings (interval, error count, timeout, image limits, etc.)
  - `rsshub_config`: RSSHub settings (URL, access key)
  - `proxy_config`: Proxy settings (URL, enable/disable)
  - `storage_config`: Storage settings (article retention days)
  - `output_config`: Output settings (text-to-image for webhook/platform)
  - Updated config access pattern: `self.config.get("section_name", {}).get("key")`
  - Backward compatible: existing flat config values will still work with `.get()` fallback

## [1.3.0] - 2026-04-07

### Added
- Provider fallback mechanism for AI digest generation
  - New config `ai_fallback_providers`: list of backup Provider IDs to try when primary fails
  - Automatic sequential fallback on provider failure (similar to AstrBot's main agent)
  - Applied to both `generate_digest()` and `generate_single_summary()` methods
  - Logging for fallback attempts and success/failure

## [1.2.1] - 2026-04-07

### Fixed
- Text-to-image rendering for AI digest
  - Fixed empty content issue when html_renderer captures screenshot before JavaScript execution
  - Migrated from client-side JavaScript rendering to server-side Python preprocessing
  - Added `markdown_to_html()` function (from astrbot_plugin_opencode) for reliable markdown parsing
  - Removed dependency on browser-side marked.js library

### Changed
- Digest template styling
  - Changed background from dark theme to paper-style light theme for better readability
  - Body background: `#f5f5f0` (warm gray paper)
  - Card background: `#fffef8` (cream/ivory paper)
  - Updated text colors for light theme compatibility

## [1.2.0] - 2026-03-23

### Added
- Article author field support
  - Added `author` field to `RSSArticle` model
  - Database schema updated with `author` column in articles table
  - Automatic migration for existing databases (backward compatible)
  - Author information is now parsed from RSS feeds (supports `author` and `author_detail` fields)

### Changed
- RSS push message format updated
  - Changed "Source" to "Via" showing article author instead of subscription name
  - Falls back to empty if author is not available in the feed
- Simplified subscriber management commands
  - `/rssadd <subscription_id> <umo>` - Now accepts complete UMO format directly
  - `/rssgroup subadd <group_id> <umo>` - Now accepts complete UMO format directly
  - Removed `adapter` and `is_group` parameters (no longer needed with UMO format)
  - UMO format example: `telegram:FriendMessage:xxxxx`, `wecom:GroupMessage:xxxxx`

## [1.1.1] - 2026-03-18
### Fixed
- 自建RSSHub未拼接Code参数到rsshub地址后

## [1.1.0] - 2025-03-17

### Added
- Telegram inline keyboard support for `/rssdel` command
  - Display subscription list as inline keyboard buttons
  - Click to unsubscribe from selected subscription
- Telegram inline keyboard support for `/rssupdate` command
  - Display subscription list for configuration selection
  - Toggle personal config options with visual status indicators (✅/⭕)
  - Config options: 仅标题, 仅图片, 有图片才发, 图片遮挡, 暂停订阅
- Telegram inline keyboard support for `/rssupdate global` command (admin)
  - Display all subscriptions for global config management
  - Toggle global config options: AI摘要, 代理, 暂停

### Technical
- Added `KEYBOARD_SESSIONS` for keyboard session state management
- Added callback handlers for keyboard interactions
- Platform-aware: keyboard on Telegram, text input preserved for other platforms

## [1.0.0] - Initial Release

### Features
- RSS subscription management
- Automatic RSS fetching with configurable intervals
- AI-powered digest generation
- Multi-platform message delivery
- Webhook support for RSS article push
