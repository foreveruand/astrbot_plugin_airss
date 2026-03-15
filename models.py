"""
Data models for the RSS plugin.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class RSSSubscription:
    """RSS subscription model."""

    id: Optional[int] = None
    name: str = ""
    url: str = ""
    interval: int = 5  # minutes
    group_id: Optional[int] = None
    ai_enabled: bool = True
    error_count: int = 0
    last_fetch: Optional[datetime] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    cookies: Optional[str] = None
    black_keyword: Optional[str] = None


@dataclass
class RSSArticle:
    """RSS article model."""

    id: Optional[int] = None
    subscription_id: int = 0
    title: str = ""
    content: str = ""
    link: str = ""
    guid: str = ""
    published_at: Optional[datetime] = None
    fetched_at: datetime = field(default_factory=datetime.now)
    is_sent: bool = False
    image_urls: list[str] = field(default_factory=list)


@dataclass
class RSSGroup:
    """RSS group model for organizing subscriptions and digest schedules."""

    id: Optional[int] = None
    name: str = ""
    schedules: list[str] = field(default_factory=list)  # HH:MM format
    persona_id: Optional[str] = None


@dataclass
class Subscriber:
    """Subscriber model linking a user/session to a subscription."""

    id: Optional[int] = None
    subscription_id: int = 0
    umo: str = ""  # Unified session ID: platform_name:message_type:session_id
    personal_config: dict[str, Any] = field(default_factory=dict)


# Personalization config keys
PERSONAL_CONFIG_KEYS = {
    "only_title": False,  # Only send article title
    "only_pic": False,  # Only send images
    "only_has_pic": False,  # Only send if article has image
    "enable_spoiler": False,  # Spoiler images
    "stop": False,  # Pause subscription for this subscriber
    "black_keyword": "",  # Keyword filter
}