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
    source_group_id: int | None = None  # group ID for digest scheduling

    # Global config fields
    cookies: str | None = None
    black_keyword: str | None = None
    content_to_remove: str | None = None  # regex pattern to remove from content
    max_image_number: int = 0  # 0 = unlimited
    ai_summary_enabled: bool = (
        True  # True = aggregate in digest, False = send individually
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


def get_effective_bool(
    subscriber: Subscriber, key: str, subscription: RSSSubscription
) -> bool:
    """Get effective bool value from subscriber config or subscription default."""
    if key in subscriber.personal_config:
        return bool(subscriber.personal_config[key])
    return bool(getattr(subscription, key, False))


def get_effective_text(
    subscriber: Subscriber, key: str, subscription: RSSSubscription
) -> str:
    """Get effective text value from subscriber config or subscription default."""
    if key in subscriber.personal_config:
        return str(subscriber.personal_config[key])
    return str(getattr(subscription, key, ""))
