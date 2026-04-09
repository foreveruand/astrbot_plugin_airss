"""
Data models for the RSS plugin.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Adapter types
TELEGRAM_ADAPTER = "telegram"
WECOM_ADAPTER = "wecom"
WEBHOOK_ADAPTER = "webhook"

# Configurable fields for subscription global config
GLOBAL_CONFIGURABLE_FIELDS = [
    "name",
    "url",
    "black_keyword",
    "cookies",
    "content_to_remove",
    "max_image_number",
    "interval",
    "ai_summary_enabled",
    "source_group_id",
]


@dataclass
class RSSSubscription:
    """RSS subscription model."""

    id: int | None = None
    name: str = ""
    url: str = ""
    interval: int = 5  # fetch interval in minutes
    source_group_id: int = 1  # group ID for digest scheduling

    # Global config fields
    cookies: str | None = None
    black_keyword: str | None = None
    content_to_remove: str | None = None  # regex pattern to remove from content
    max_image_number: int = 0  # 0 = unlimited
    ai_summary_enabled: bool = (
        False  # True = aggregate in digest, False = send individually
    )
    enable_proxy: bool = False
    stop: bool = False

    # Internal tracking
    error_count: int = 0
    last_fetch: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class RSSArticle:
    """RSS article model."""

    id: int | None = None
    subscription_id: int = 0
    title: str = ""
    content: str = ""
    link: str = ""
    guid: str = ""
    author: str = ""  # Article author, empty if not available
    published_at: datetime | None = None
    fetched_at: datetime = field(default_factory=datetime.now)
    is_sent: bool = False
    image_urls: list[str] = field(default_factory=list)


@dataclass
class RSSGroup:
    """RSS group model for organizing subscriptions and digest schedules."""

    id: int | None = None
    name: str = ""
    schedules: list[str] = field(default_factory=list)  # HH:MM format
    persona_id: str | None = None


@dataclass
class Subscriber:
    """Subscriber model linking a user/session to a subscription.

    The `umo` (unified message origin) field uses AstrBot's format:
    - For telegram/wecom: "platform_name:message_type:session_id"
      - Example: "telegram:GroupMessage:-100123456"
    - For webhook: the webhook URL directly (starts with http:// or https://)

    Adapter type is inferred from umo, not stored separately.
    """

    id: int | None = None
    subscription_id: int = 0
    umo: str = ""  # Unified session ID or webhook URL
    personal_config: dict[str, Any] = field(default_factory=dict)

    def is_webhook(self) -> bool:
        """Check if this subscriber uses webhook (URL instead of umo)."""
        return self.umo.startswith("http://") or self.umo.startswith("https://")

    def get_adapter(self) -> str:
        """Get adapter type from umo."""
        if self.is_webhook():
            return WEBHOOK_ADAPTER
        # Parse platform from umo: "platform_name:message_type:session_id"
        parts = self.umo.split(":", 1)
        if parts:
            return parts[0]
        return TELEGRAM_ADAPTER

    def get_webhook_url(self) -> str | None:
        """Get webhook URL if this is a webhook subscriber."""
        if self.is_webhook():
            return self.umo
        return None


# Personalization config keys (per-subscriber)
PERSONAL_CONFIG_KEYS = {
    "only_title": False,  # Only send article title
    "only_pic": False,  # Only send images
    "only_has_pic": False,  # Only send if article has image
    "enable_spoiler": False,  # Spoiler images
    "stop": False,  # Pause subscription for this subscriber
    "black_keyword": "",  # Keyword filter
}

# Config number to name mapping for interactive selection (personal configs ①-⑥)
CONFIG_NUMBER_MAP = {
    "①": "only_title",
    "②": "only_pic",
    "③": "only_has_pic",
    "④": "enable_spoiler",
    "⑤": "stop",
    "⑥": "black_keyword",
    # Global configs ⑦-⑫
    "⑦": "interval",
    "⑧": "max_image_number",
    "⑨": "ai_summary_enabled",
    "⑩": "enable_proxy",
    "⑪": "source_group_id",
    "⑫": "black_keyword",
}

# Config name to number mapping (reverse of CONFIG_NUMBER_MAP)
CONFIG_NAME_MAP = {
    "only_title": "①",
    "only_pic": "②",
    "only_has_pic": "③",
    "enable_spoiler": "④",
    "stop": "⑤",
    "black_keyword": "⑥",  # Personal config takes precedence
    "interval": "⑦",
    "max_image_number": "⑧",
    "ai_summary_enabled": "⑨",
    "enable_proxy": "⑩",
    "source_group_id": "⑪",
}

# Config descriptions with placeholders for current value
# black_keyword uses different numbers for personal (⑥) and global (⑫) contexts
# so we use separate keys: black_keyword_personal and black_keyword_global
CONFIG_DESCRIPTIONS = {
    "only_title": "① 仅发送标题 - 当前: {value}",
    "only_pic": "② 仅发送图片 - 当前: {value}",
    "only_has_pic": "③ 仅发送有图片的文章 - 当前: {value}",
    "enable_spoiler": "④ 图片剧透标签 - 当前: {value}",
    "stop": "⑤ 暂停订阅 - 当前: {value}",
    "interval": "⑦ 抓取间隔（分钟）- 当前: {value}",
    "max_image_number": "⑧ 最大图片数 - 当前: {value}",
    "ai_summary_enabled": "⑨ AI摘要开关 - 当前: {value}",
    "enable_proxy": "⑩ 使用代理 - 当前: {value}",
    "source_group_id": "⑪ 所属分组ID - 当前: {value}",
    "black_keyword_personal": "⑥ 关键词黑名单 - 当前: {value}",
    "black_keyword_global": "⑫ 关键词黑名单（全局）- 当前: {value}",
}


PERSONAL_CONFIG_NUMBERS = ["①", "②", "③", "④", "⑤", "⑥"]
GLOBAL_CONFIG_NUMBERS = ["⑦", "⑧", "⑨", "⑩", "⑪", "⑫"]


def get_config_number(name: str) -> str | None:
    """Get the config number symbol for a given config name.

    Args:
        name: The config parameter name (e.g., "only_title", "interval")

    Returns:
        The number symbol (e.g., "①", "⑦") or None if not found
    """
    return CONFIG_NAME_MAP.get(name)


def get_config_name(number: str) -> str | None:
    """Get the config parameter name for a given number symbol.

    Args:
        number: The number symbol (e.g., "①", "⑦")

    Returns:
        The config parameter name (e.g., "only_title", "interval") or None if not found
    """
    return CONFIG_NUMBER_MAP.get(number)


def get_effective_bool(
    subscriber: Subscriber, key: str, subscription: RSSSubscription
) -> bool:
    """Get effective bool value from subscriber config or subscription default."""
    config = subscriber.personal_config or {}
    if key in config:
        return bool(config[key])
    return bool(getattr(subscription, key, False))


def get_effective_text(
    subscriber: Subscriber, key: str, subscription: RSSSubscription
) -> str:
    """Get effective text value from subscriber config or subscription default."""
    config = subscriber.personal_config or {}
    if key in config:
        return str(config[key])
    return str(getattr(subscription, key, ""))
