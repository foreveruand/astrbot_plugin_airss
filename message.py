"""
Message formatting and sending utilities for RSS plugin.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import aiohttp

from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession

from .models import RSSArticle, RSSSubscription, Subscriber

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")

MAX_WEBHOOK_TEXT_LENGTH = 4090


class MessageFormatter:
    """Formats RSS articles and digests for sending."""

    @staticmethod
    def format_article(
        article: RSSArticle,
        subscription: RSSSubscription | None = None,
        personal_config: dict | None = None,
    ) -> str:
        """
        Format a single article for sending.

        Args:
            article: Article to format
            subscription: Optional subscription info
            personal_config: Optional subscriber preferences

        Returns:
            Formatted message string
        """
        config = personal_config or {}
        lines = []

        # Title
        title = article.title or "Untitled"
        lines.append(f"**{title}**")

        # Via (author)
        if article.author:
            lines.append(f"Via: {article.author}")

        # Published time
        if article.published_at:
            time_str = article.published_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"Published: {time_str}")

        # Content
        if not config.get("only_title", False):
            content = article.content or ""
            if content:
                # Clean HTML
                content = MessageFormatter._clean_html(content)
                # Truncate if too long
                if len(content) > 500:
                    content = content[:500] + "..."
                lines.append(f"\n{content}")

        # Link
        if article.link:
            lines.append(f"\n[Read more]({article.link})")

        return "\n".join(lines)

    @staticmethod
    def format_digest(
        articles: list[RSSArticle],
        digest_content: str,
        group_name: str | None = None,
    ) -> str:
        """
        Format an AI digest for sending.

        Args:
            articles: List of articles in digest
            digest_content: AI-generated digest content
            group_name: Optional group name

        Returns:
            Formatted digest message
        """
        lines = []

        # Header
        if group_name:
            lines.append(f"## 📰 {group_name} - RSS Digest")
        else:
            lines.append("## 📰 RSS Digest")

        lines.append(f"\n*{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")

        # AI digest content
        lines.append(digest_content)

        # Footer with article count
        lines.append(f"\n---\n*{len(articles)} articles in this digest*")

        return "\n".join(lines)

    @staticmethod
    def format_article_list(articles: list[RSSArticle]) -> str:
        """
        Format a list of articles as a simple list.

        Args:
            articles: Articles to format

        Returns:
            Formatted article list
        """
        if not articles:
            return "No articles."

        lines = ["📰 Articles:\n"]

        for i, article in enumerate(articles, 1):
            title = article.title or "Untitled"
            lines.append(f"{i}. **{title}**")
            if article.link:
                lines.append(f"   {article.link}")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _clean_html(text: str) -> str:
        """Remove HTML tags and decode entities."""
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", "", text)
        # Decode HTML entities
        text = text.replace("&nbsp;", " ")
        text = text.replace("&amp;", "&")
        text = text.replace("&lt;", "<")
        text = text.replace("&gt;", ">")
        text = text.replace("&quot;", '"')
        # Remove extra whitespace
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def apply_personalization(
        article: RSSArticle,
        personal_config: dict,
    ) -> tuple[bool, str | None]:
        """
        Check if article should be sent based on personalization config.
        Returns (should_send, formatted_content).

        Args:
            article: Article to check
            personal_config: Subscriber's personal config

        Returns:
            Tuple of (should_send, content)
        """
        # Check if paused
        if personal_config.get("stop", False):
            return False, None

        # Check keyword blacklist
        black_keyword = personal_config.get("black_keyword", "")
        if black_keyword:
            keywords = [k.strip() for k in black_keyword.split(",") if k.strip()]
            article_text = f"{article.title} {article.content}".lower()
            for keyword in keywords:
                if keyword.lower() in article_text:
                    logger.debug(f"Article filtered by keyword: {keyword}")
                    return False, None

        # Check image requirements
        only_has_pic = personal_config.get("only_has_pic", False)
        if only_has_pic and not article.image_urls:
            return False, None

        # Format based on preferences
        if personal_config.get("only_title", False):
            content = f"**{article.title}**\n{article.link}"
        elif personal_config.get("only_pic", False):
            if article.image_urls:
                content = f"**{article.title}**\n\n" + "\n".join(article.image_urls)
            else:
                return False, None
        else:
            content = MessageFormatter.format_article(article)

        return True, content


class MessageSender:
    """Handles sending messages to subscribers."""

    def __init__(self, context: "Context"):
        self.context = context

    def _is_webhook(self, umo: str) -> bool:
        """Check if UMO is a webhook URL."""
        return umo.startswith("http://") or umo.startswith("https://")

    async def _send_webhook(
        self,
        webhook_url: str,
        text: str,
        timeout: int = 10,
    ) -> bool:
        """Send message to webhook (WeCom bot format)."""
        if not text:
            return False

        truncated = self._truncate_by_bytes(text, MAX_WEBHOOK_TEXT_LENGTH)

        try:
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": truncated},
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("errcode") == 0:
                            return True
                        logger.warning(f"Webhook error: {result}")
                    else:
                        logger.warning(f"Webhook status: {resp.status}")
        except Exception as e:
            logger.error(f"Webhook send failed: {e}")

        return False

    @staticmethod
    def _truncate_by_bytes(text: str, max_bytes: int) -> str:
        """Truncate text to max bytes."""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    async def send_to_session(
        self,
        session_str: str,
        content: str,
    ) -> bool:
        """
        Send a message to a specific session.

        Args:
            session_str: Session identifier (UMO format or webhook URL)
            content: Message content

        Returns:
            True if sent successfully
        """
        try:
            if self._is_webhook(session_str):
                return await self._send_webhook(session_str, content)

            session = MessageSession.from_str(session_str)
            message_chain = MessageChain().message(content)
            await self.context.send_message(session, message_chain)
            return True
        except Exception as e:
            logger.error(f"Failed to send message to {session_str}: {e}")
            return False

    async def send_to_subscribers(
        self,
        subscribers: list[Subscriber],
        content: str,
        articles: list[RSSArticle] | None = None,
    ) -> int:
        """
        Send message to multiple subscribers with personalization.

        Args:
            subscribers: List of subscribers
            content: Base content (for those without special config)
            articles: Optional articles for per-subscriber formatting

        Returns:
            Number of successful sends
        """
        success_count = 0
        tasks = []

        for subscriber in subscribers:
            # Skip if paused
            if subscriber.personal_config.get("stop", False):
                continue

            if articles and len(articles) == 1:
                # Apply personalization for single article
                should_send, formatted = MessageFormatter.apply_personalization(
                    articles[0],
                    subscriber.personal_config,
                )
                if not should_send:
                    continue
                msg_content = formatted or content
            else:
                msg_content = content

            tasks.append(self.send_to_session(subscriber.umo, msg_content))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success_count = sum(1 for r in results if r is True)

        return success_count
