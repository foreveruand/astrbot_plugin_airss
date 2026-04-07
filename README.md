# AstrBot RSS 订阅插件

一个功能完整的 RSS 订阅插件，支持 AI 摘要生成、分组管理、个性化订阅和多平台推送。

## 功能特性

- 🔖 **RSS 订阅管理**: 添加、删除、列出订阅
- 🤖 **AI 摘要生成**: 使用 AstrBot 原生 AI 能力生成文章摘要
- 📂 **分组管理**: 将订阅分组，每个分组可设置不同的推送时间和摘要提示词
- 👤 **Persona 系统**: 每个分组对应一个 Persona，自定义摘要风格
- 🎯 **个性化订阅**: 支持仅标题、仅图片、关键词过滤等个性化选项
- ⏰ **定时推送**: 支持多个推送时间点
- 🌐 **多平台支持**: 通过 AstrBot 统一消息接口推送

## 安装方法

1. 将插件文件夹放置在 `data/plugins/astrbot_plugin_rss/`
2. 安装依赖：
   ```bash
   pip install feedparser aiohttp aiosqlite
   ```
3. 重启 AstrBot

## 配置说明

在 AstrBot 管理面板中配置以下选项（配置已按功能分区）：

### AI 摘要配置 (`ai_config`)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `ai_provider` | "" | AI Provider ID，为空则使用会话默认 |
| `ai_fallback_providers` | [] | 备用 Provider ID 列表，主 Provider 失败时依次尝试 |
| `ai_summary_timezone` | "Asia/Shanghai" | 摘要时区 |
| `ai_digest_max_articles` | 50 | 每次摘要最大文章数 |
| `ai_digest_max_input_tokens` | 131072 | 最大输入 token 数 |
| `ai_digest_max_output_tokens` | 8192 | 最大输出 token 数 |
| `ai_digest_title_max_len` | 120 | 标题最大字符数 |
| `ai_digest_content_max_len` | 2048 | 内容最大字符数 |
| `ai_fallback_message` | "" | AI 摘要失败时的提示消息 |

### RSS 抓取配置 (`fetch_config`)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `default_interval` | 5 | 默认抓取间隔（分钟） |
| `max_error_count` | 100 | 最大连续错误次数，超过后跳过抓取 |
| `max_concurrent_fetches` | 5 | 最大并发抓取数 |
| `request_timeout` | 30 | 请求超时时间（秒） |
| `max_image_number` | 0 | 每篇文章最大图片数，0 为不限制 |
| `enable_spoiler` | false | 图片使用剧透标签 |

### RSSHub 配置 (`rsshub_config`)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `rsshub_url` | "" | RSSHub 服务器地址 |
| `rsshub_key` | "" | RSSHub 访问密钥 |

### 代理配置 (`proxy_config`)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `proxy` | "" | HTTP 代理地址 |
| `enable_proxy` | false | 是否启用代理 |

### 存储配置 (`storage_config`)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `article_retention_days` | 30 | 文章保留天数 |

### 输出配置 (`output_config`)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `t2i_webhook_enabled` | false | Webhook 摘要启用图片渲染 |
| `t2i_platform_enabled` | false | 平台消息摘要启用图片渲染 |

## 命令列表

### 订阅命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/rssadd <url> [name]` | 添加订阅 | `/rssadd https://example.com/feed.xml 科技资讯` |
| `/rssdel <name\|id>` | 删除订阅 | `/rssdel 科技资讯` |
| `/rsslist` | 列出所有订阅 | `/rsslist` |
| `/rssupdate <name\|id> [key] [value]` | 更新订阅配置 | `/rssupdate 1 only_title true` |
| `/rsstrigger [name\|id]` | 手动触发更新（管理员） | `/rsstrigger` |

### 分组命令（管理员）

| 命令 | 说明 | 示例 |
|------|------|------|
| `/rssgroup add <name>` | 创建分组 | `/rssgroup add 科技资讯` |
| `/rssgroup rename <id> <name>` | 重命名分组 | `/rssgroup rename 1 科技` |
| `/rssgroup list` | 列出所有分组 | `/rssgroup list` |
| `/rssgroup timeadd <id> <HH:MM>` | 添加推送时间 | `/rssgroup timeadd 1 09:00` |
| `/rssgroup timedel <id> <HH:MM>` | 删除推送时间 | `/rssgroup timedel 1 09:00` |
| `/rssgroup subadd <id> <session>` | 添加订阅者 | `/rssgroup subadd 1 telegram:GroupMessage:-100123456` |
| `/rssgroup subdel <id> <session>` | 删除订阅者 | `/rssgroup subdel 1 telegram:GroupMessage:-100123456` |

## 个性化配置

订阅者可以为每个订阅设置个性化选项：

| 选项 | 说明 |
|------|------|
| `only_title` | 仅发送标题 |
| `only_pic` | 仅发送图片 |
| `only_has_pic` | 仅发送有图片的文章 |
| `enable_spoiler` | 图片使用剧透标签 |
| `stop` | 暂停订阅 |
| `black_keyword` | 关键词黑名单（逗号分隔） |

示例：
```
/rssupdate 科技资讯 only_title true
/rssupdate 科技资讯 black_keyword 广告,推广
```

## Persona 系统

每个分组对应一个 Persona，ID 格式为 `rss_group_{group_id}`。

可以通过 AstrBot 的 Persona 管理功能自定义每个分组的摘要风格：
- 在 AstrBot 管理面板中找到 Persona 设置
- 创建或编辑 `rss_group_{id}` 的 Persona
- 在系统提示词中定义摘要风格

默认提示词：
```
你是一个RSS文章摘要助手，请为用户整理和总结订阅的文章。
```

## 数据存储

- 数据库位置：`data/plugin_data/astrbot_plugin_rss/rss.db`
- 数据类型：SQLite
- 表结构：
  - `subscriptions`: 订阅源
  - `articles`: 文章缓存
  - `groups`: 分组
  - `subscribers`: 订阅者

## 注意事项

1. **Telegram Keyboard**: 支持 Telegram 内联键盘，在 `/rssdel` 和 `/rssupdate` 命令中可通过键盘按钮交互操作
2. **AI Provider**: 确保在配置中设置正确的 Provider ID，或在会话中配置默认 Provider
3. **Persona**: 创建分组时会自动创建对应的 Persona，可在管理面板中修改
4. **错误处理**: 订阅连续失败超过 `max_error_count` 次后会跳过抓取，直到手动触发或重置

## 迁移自 NoneBot

本插件从 `nonebot_plugin_rss` 迁移而来，主要变化：

1. **AI 调用**: 使用 AstrBot 原生 `llm_generate()` 替代 `nonebot_plugin_chatgpt`
2. **调度系统**: 使用 AstrBot 内置 `CronJobManager` 替代直接 APScheduler
3. **消息发送**: 使用 AstrBot 统一消息接口 `context.send_message()`
4. **Persona**: 使用 AstrBot Persona 系统存储分组摘要提示词

## 许可证

MIT License