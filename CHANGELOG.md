# Changelog

All notable changes to this project will be documented in this file.

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