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
| `astrbot_config_file` | "" | 可选的 AstrBot 配置名或文件名。留空时不启用 AI 摘要轮换，仅使用主 Provider。可直接填写管理面板里的配置名（default）或实际文件名；默认配置对应 `data/cmd_config.json`，通过管理面板创建的配置通常位于 `data/config/abconf_*.json`。填写后将读取该配置里的 `provider_settings.fallback_chat_models` 作为 AI 摘要回退顺序。 |
| `ai_summary_timezone` | "Asia/Shanghai" | 摘要时区 |
| `ai_digest_max_articles` | 50 | 每次摘要最大文章数 |
| `ai_digest_recent_days` | 0 | 仅获取并摘要最近 X 天更新的未发送文章，0 为不限制；发送与已读标记也以该范围为准 |
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
| `article_retention_days` | 30 | 文章保留天数，按发布时间优先清理；抓取时也会跳过超出保留期的旧文章 |

### 输出配置 (`output_config`)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `t2i_webhook_enabled` | false | Webhook 摘要启用图片渲染 |
| `t2i_platform_enabled` | false | 平台消息摘要启用图片渲染 |
| `t2i_image_type` | "jpeg" | 图片格式，可选 jpeg 或 png |
| `t2i_image_quality` | 70 | 图片质量（仅 JPEG 有效），范围 10-100 |
| `t2i_scale` | "device" | 页面缩放设置，可选 css 或 device |
| `t2i_full_page` | true | 渲染完整页面 |

#### 图片质量控制说明

当启用 `t2i_webhook_enabled` 或 `t2i_platform_enabled` 时，AI 摘要会渲染为图片发送。
可通过以下选项控制图片质量：

- **`t2i_image_type`**: 选择图片格式
  - `jpeg`: 文件更小，适合网络传输
  - `png`: 支持透明背景，文件较大
- **`t2i_image_quality`**: JPEG 图片质量（10-100）
  - 推荐 60-90，数值越高质量越好但文件越大
  - 企业微信 Webhook 有 2MB 文件限制，建议不超过 80
- **`t2i_scale`**: 页面缩放设置
  - `device`: 使用设备缩放设置，适合高分屏
  - `css`: CSS像素对应设备分辨率，高分屏截图变小
- **`t2i_full_page`**: 是否渲染完整页面
  - `true`: 渲染完整内容页面
  - `false`: 仅渲染视口大小

#### AI 回退来源说明

`astrbot_config_file` 用于指定回退 Provider 列表的来源配置。

- 留空：不启用轮换，仅使用主 Provider
- 可填写配置名或文件名：例如 `default`、`Local`、`QQ`、`cmd_config.json` 或 `abconf_xxx.json`
- 如果目标文件不存在或配置项为空，插件会回退到旧的 `ai_fallback_providers` 配置作为兼容兜底

## 命令列表

### 订阅命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/rssadd <url> [name]` | 添加订阅 | `/rssadd https://example.com/feed.xml 科技资讯` |
| `/rssdel <name\|id>` | 删除订阅 | `/rssdel 科技资讯` |
| `/rsslist` | 列出所有订阅 | `/rsslist` |
| `/rssupdate [name\|id] [key] [value]` | 更新订阅配置 | 详见下方交互说明 |
| `/rssgroup <add/rename/list/time>` | 分组管理 | 详见分组命令 |
| `/rsssub <join/leave/add/del/list>` | 订阅者管理 | 详见订阅者命令 |
| `/rssutil <rsshub/test/trigger>` | 工具命令 | 详见工具命令 |

### rssupdate 交互模式

`/rssupdate` 支持 4 种交互模式：

**模式 A: 无参数**
```
/rssupdate
```
- Telegram: 显示订阅列表的内联键盘，点击选择
- 其他平台: 显示文本列表，需回复选择

**模式 B: 仅指定订阅**
```
/rssupdate 科技资讯
```
显示该订阅的可配置项（个人配置 ①-⑥，全局配置 ⑦-⑫）

**模式 C: 订阅 + 配置项**
```
/rssupdate 科技资讯 only_title
```
提示输入该配置项的值

**模式 D: 完整参数**
```
/rssupdate 科技资讯 only_title true
```
直接修改配置

### 配置项编号

**个人配置 (①-⑥)**

| 编号 | 参数 | 说明 |
|------|------|------|
| ① | `only_title` | 仅发送标题 |
| ② | `only_pic` | 仅发送图片 |
| ③ | `only_has_pic` | 仅发送有图片的文章 |
| ④ | `enable_spoiler` | 图片使用剧透标签 |
| ⑤ | `stop` | 暂停订阅 |
| ⑥ | `black_keyword` | 关键词黑名单（逗号分隔） |

**全局配置 (⑦-⑫，管理员)**

| 编号 | 参数 | 说明 |
|------|------|------|
| ⑦ | `interval` | 抓取间隔（分钟） |
| ⑧ | `max_image_number` | 每篇文章最大图片数 |
| ⑨ | `ai_summary_enabled` | 启用 AI 摘要 |
| ⑩ | `enable_proxy` | 启用代理 |
| ⑪ | `source_group_id` | 所属分组 ID |
| ⑫ | `black_keyword` | 关键词黑名单 |

### 分组命令 (管理员)

| 命令 | 说明 | 示例 |
|------|------|------|
| `/rssgroup add <name>` | 创建分组 | `/rssgroup add 科技资讯` |
| `/rssgroup rename <id> <name>` | 重命名分组 | `/rssgroup rename 1 科技` |
| `/rssgroup list` | 列出所有分组 | `/rssgroup list` |
| `/rssgroup time <id> add <HH:MM>` | 添加推送时间 | `/rssgroup time 1 add 09:00` |
| `/rssgroup time <id> del <HH:MM>` | 删除推送时间 | `/rssgroup time 1 del 09:00` |

### 订阅者命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/rsssub join <group_id>` | 加入分组（订阅该分组所有源） | `/rsssub join 1` |
| `/rsssub leave <group_id>` | 离开分组（取消订阅） | `/rsssub leave 1` |
| `/rsssub add <sub_id> <umo>` | 添加订阅者（管理员） | `/rsssub add 1 telegram:GroupMessage:-100123456` |
| `/rsssub del <sub_id> <umo>` | 删除订阅者（管理员） | `/rsssub del 1 telegram:GroupMessage:-100123456` |
| `/rsssub list <sub_id>` | 列出订阅者（管理员） | `/rsssub list 1` |

### 工具命令 (管理员)

| 命令 | 说明 | 示例 |
|------|------|------|
| `/rssutil rsshub [path]` | 打印 RSSHub URL | `/rssutil rsshub /twitter/user/elonmusk` |
| `/rssutil test <url>` | 测试 RSS 源可用性 | `/rssutil test https://example.com/feed.xml` |
| `/rssutil trigger [name\|id]` | 手动触发抓取 | `/rssutil trigger 科技资讯` |

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

## Telegram 内联键盘交互

在 Telegram 平台，`/rssdel` 和 `/rssupdate` 命令支持内联键盘交互：

- **`/rssdel`**: 显示订阅列表的内联按钮，点击即可删除
- **`/rssupdate`**: 显示订阅列表，选择后显示配置项编号按钮（①-⑫），点击配置项即可修改

其他平台会显示文本列表，需通过回复序号或完整命令进行操作。

## 注意事项

1. **Telegram Keyboard**: 支持 Telegram 内联键盘，在 `/rssdel` 和 `/rssupdate` 命令中可通过键盘按钮交互操作，详见上方说明
2. **AI Provider**: 确保在配置中设置正确的 Provider ID，或在会话中配置默认 Provider
3. **Persona**: 创建分组时会自动创建对应的 Persona，可在管理面板中修改
4. **错误处理**: 订阅连续失败超过 `max_error_count` 次后会跳过抓取，直到手动触发或重置
5. **文章清理规则**: `article_retention_days` 会优先按文章 `published_at` 判断是否过期；如果订阅源没有发布时间，则回退使用抓取时间 `fetched_at`
6. **RSSHub 集成**: 配置 `rsshub_url` 后，可直接使用 `/rssadd /path/to/feed` 添加 RSSHub 路由，无需完整 URL

## 迁移自 NoneBot

本插件从 `nonebot_plugin_rss` 迁移而来，主要变化：

1. **AI 调用**: 使用 AstrBot 原生 `llm_generate()` 替代 `nonebot_plugin_chatgpt`
2. **调度系统**: 使用 AstrBot 内置 `CronJobManager` 替代直接 APScheduler
3. **消息发送**: 使用 AstrBot 统一消息接口 `context.send_message()`
4. **Persona**: 使用 AstrBot Persona 系统存储分组摘要提示词

## 许可证

MIT License
