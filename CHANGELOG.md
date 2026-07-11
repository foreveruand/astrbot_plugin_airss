# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed
- Clarified that batched AI topic-filter responses must use current article IDs, preventing models from returning candidate article IDs that the filter rejects

## [1.6.12] - 2026-07-10

### Fixed
- Removed the AI topic-filter request timeout introduced in 1.6.11 so remote model responses are not cancelled after 10 seconds
- AI topic-filter failures now include the exception type in logs
- AI topic-filter failures are persisted as not duplicate and are not retried for the same article

## [1.6.11] - 2026-07-10

### Changed
- AI topic duplicate filtering now batches pending articles within each RSS source and persists the result per article
  - Multiple subscribers with `ai_filter_enabled` no longer repeat AI calls for the same article
  - Subscriber blacklists, whitelists, and image-only filtering run before an article enters an AI batch
  - Duplicate results apply only to subscribers that enabled AI filtering; other subscribers still receive the article
  - Previously marked duplicate articles are excluded from later AI candidate lists
  - Existing databases are migrated by adding nullable AI filter result and check-time columns

## [1.6.10] - 2026-07-10

### Added
- Per-subscriber `ai_filter_enabled` duplicate filtering for newly fetched articles
  - Uses a single AstrBot LLM call to compare the article title with plugin-wide recent article titles
  - Similar articles are marked as sent only for that subscriber
  - AI errors or unparsable results fall back to normal sending
- AI filter settings `ai_filter_provider` and `ai_filter_recent_minutes`
  - The filter provider is independent from the AI digest provider

## [1.6.9] - 2026-07-09

### Changed
- Telegram `/rssupdate` text and numeric config buttons now wait for the next message as the new value
  - Deletes the user input message when Telegram permits it
  - Refreshes the original inline config menu in place after success or cancellation

## [1.6.8] - 2026-06-18

### Fixed
- Personal `stop` recovery now marks the subscriber's current backlog as sent
  - Prevents a paused subscriber from receiving every article accumulated during the pause after resuming
  - Other subscribers on the same feed keep their own unread state unchanged

## [1.6.7] - 2026-06-13

### Added
- Personal `white_keyword` subscription filtering for `/rssupdate`
  - Matches article title and content with comma-separated keywords
  - Keeps black keyword filtering higher priority when both are configured

### Changed
- Telegram `/rssupdate` buttons now include short labels instead of emoji-only controls
- Admin users can switch from the personal config panel to the global config panel with a dedicated inline button

### Fixed
- Telegram `/rssupdate` callback data is shorter and no longer includes unnecessary user IDs
- Callback admin permission now falls back to the keyboard session creator, so global config buttons keep working after Telegram callback events

## [1.6.6] - 2026-06-08

### Fixed
- RSSHub `code` generation now matches current RSSHub access-key validation
  - Calculates MD5 from the final URL pathname plus `rsshub_key`
  - Preserves existing percent escapes such as `%2F` instead of double-encoding them
  - Encodes raw special characters before signing and appends `code` correctly when the route already has query parameters

## [1.6.5] - 2026-06-08

### Fixed
- Command parsing now preserves argument text instead of removing command words from later arguments
  - `/rssadd` keeps the full optional name after the URL
  - `/rssdel` can match subscription names containing spaces
  - `/rssupdate` keeps multi-word configuration values
- Global update config now recognizes `enable_proxy`, `stop`, and numbered `⑫ black_keyword`
  - `⑥ black_keyword` remains personal config, while `⑫ black_keyword` targets global config for admins
  - Telegram config callbacks now enforce admin permission before changing global fields
- New subscriptions now read `fetch_config.default_interval` and inherit `proxy_config.enable_proxy`
- The schema default for `ai_digest_recent_days` now matches documented behavior (`0`, unlimited)
- The stopped-subscription warning now points to `/rssutil trigger` instead of the removed `/rsstrigger`

### Changed
- Stopped subscriptions remove their fetch cron job instead of keeping a no-op scheduled handler
- Manual "trigger all" fetches now use `fetch_config.max_concurrent_fetches` for bounded concurrency
- AI digest collection now applies subscriber `black_keyword` and `only_has_pic` filters before generating summaries
- `content_to_remove` is applied before storing fetched articles
- Per-subscription `enable_proxy` now controls whether that feed uses the configured proxy

## [1.6.4] - 2026-06-04

### Fixed
- AI digest Agent config now uses AstrBot's current `llm_compress_keep_recent_ratio` setting
  - Fixes `MainAgentBuildConfig.__init__() got an unexpected keyword argument 'llm_compress_keep_recent'` on recent AstrBot versions

## [1.6.3] - 2026-05-23

### Changed
- Digest group schedules now accept either legacy daily `HH:MM` values or 5-field cron expressions
  - Existing daily schedules continue to work without migration
  - `/rssgroup time <id> add/del ...` now accepts cron strings such as `0 9 * * 1-5`

## [1.6.2] - 2026-05-10

### Fixed
- AI digest cron cleanup now supports both AstrBot conversation object shapes
  - Prevents crashes such as `'Conversation' object has no attribute 'conversation_id'`
- AI digest provider fallback is now restricted to model or endpoint-link errors
  - Avoids rotating to fallback providers for plugin/runtime errors and wasting requests

## [1.6.1] - 2026-05-09

### Fixed
- AI digest cron agents now preload persona-selected tools before entering AstrBot's main-agent loop
  - Fixes cases where explicitly selected tools such as `fetch_url` were missing from the digest agent tool list
- AI digest cron agents now preload the effective Web Search tools from the configured AstrBot profile
  - Fixes missing built-in search tools such as Tavily in scheduled digest runs
- Temporary digest cron conversations are now deleted after each summary run
  - Prevents the conversation list from accumulating many empty `cron` sessions

## [1.6.0] - 2026-05-08

### Changed
- AI digest now runs through AstrBot's full main-agent pipeline by default
  - Added `ai_digest_use_agent` (default `true`)
  - Added `ai_digest_agent_max_steps` (default `8`)
  - Added `ai_digest_tool_call_timeout` (default `60`)
  - Digest provider selection still follows `ai_provider` plus fallback providers from `astrbot_config_file`
- Digest generation is now scoped by each recipient's visible unread article set instead of one shared group-wide article pool
  - Recipients only reuse the same digest when their article ID sets are exactly identical
  - Stopped recipients no longer participate in digest bucketing
  - Articles are marked as sent only after that recipient successfully receives the digest

### Added
- RSS group personas are now auto-maintained as `rss_group_{group_id}`
  - Group creation immediately ensures the persona exists
  - Digest execution repairs historical groups missing `persona_id`
  - Auto-created personas default to `tools=None` and `skills=None` so the full Agent tool/skill set is available

## [1.5.7] - 2026-04-21

### Fixed
- `rssutil rsshub [path]` now parses path arguments robustly and correctly generates `?code=` when `rsshub_key` is configured
- RSSHub code generation now consistently uses configured `rsshub_key` (removed hardcoded key usage)

## [1.5.6] - 2026-04-15

### Added
- Telegram inline keyboard support for `/rssdel` and `/rssupdate` command selection
- Enhanced configuration workflow with numbered options (①-⑫) for interactive selection
- Configuration system with personal and global config separation
- Improved error handling and fallback mechanisms for AI digest operations

## [1.5.5] - 2026-04-14

### Changed
- RSS article push now sends full article content instead of truncating to 500 characters
  - Message splitting is delegated to platform adapters (e.g. Telegram 4096 message limit)

## [1.5.4] - 2026-04-14

### Fixed
- `ai_digest_recent_days` is now applied when loading unread digest articles
  - The digest handler now requests recent articles directly from subscriber unread retrieval
  - `generate_digest()` no longer applies a second filter on top of the retrieved article set
  - Digest sending and `article_sent` marking now stay aligned with the same filtered article set

## [1.5.3] - 2026-04-13

### Changed
- `ai_fallback_providers` is now sourced from AstrBot config files
  - Added `ai_config.astrbot_config_file` to select the AstrBot config file used for fallback provider resolution
  - When set, the plugin reads `provider_settings.fallback_chat_models` from that AstrBot config
  - Leaving `astrbot_config_file` empty disables fallback rotation and uses only the primary provider

## [1.5.2] - 2026-04-13

### Fixed
- AI digest now marks articles as sent for every subscriber record behind the same UMO
  - Prevents the same unread articles from reappearing across digest runs when one UMO subscribes to multiple feeds
  - Keeps per-subscription article tracking scoped to each subscriber's own subscription

## [1.5.1] - 2026-04-09

### Fixed
- Webhook image rendering now validates image format before sending
  - Detects T2I service error responses (e.g., 502 Internal Server Error)
  - Falls back to markdown text when invalid image is detected

### Changed
- change log level to DEBUG if no new articles when fetch

## [1.5.0] - 2026-04-09

### Added
- New `rsssub` command for subscriber management (join/leave/add/del/list)
- New `rssutil` command for utilities (rsshub/test/trigger)
- Configuration item numbering system (①-⑫) for interactive selection
- `rssupdate` 4-mode interaction (no args → subscription selection → config item → edit)
- Telegram inline keyboard button interaction optimization
- Text list interaction for other platforms

### Changed
- Command structure refactored to single responsibility (7 commands)
- `rssadd` simplified to only add subscriptions
- `rssdel` simplified to only delete subscriptions
- `rssgroup` removed subadd/subdel, merged time command
- `rsstrigger` merged into `rssutil trigger`

### Removed
- Removed `rssadd -g/-p` parameters
- Removed `rssgroup subadd/subdel` subcommands
- Removed standalone `rsstrigger` command

## [1.4.3] - 2026-04-08

### Added
- Image quality control configuration for text-to-image rendering
  - `t2i_image_type`: Image format (jpeg/png), jpeg for smaller files, png for transparency
  - `t2i_image_quality`: JPEG quality (10-100), recommended 60-90
  - `t2i_scale`: Page scale setting (css/device), device uses device pixel ratio
  - `t2i_full_page`: Whether to render full page or viewport only
  - These options apply to AI digest image rendering for both webhook and platform messages

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
