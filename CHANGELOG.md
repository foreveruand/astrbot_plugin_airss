# Changelog

All notable changes to this project will be documented in this file.
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