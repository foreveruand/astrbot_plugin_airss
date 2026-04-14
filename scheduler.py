"""
Scheduler service for RSS plugin using AstrBot's CronJobManager.
"""

import asyncio
import base64
import hashlib
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp
from PIL import Image

from astrbot.api import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession

from .database import Database
from .fetcher import RSSFetcher
from .models import RSSArticle, RSSGroup, RSSSubscription, Subscriber

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")

MAX_WEBHOOK_TEXT_LENGTH = 4090


def markdown_to_html(text: str) -> str:
    """将 Markdown 文本转换为 HTML，支持常见 Markdown 语法。

    来自 astrbot_plugin_opencode，轻量化实现，无需任何额外依赖。
    支持：标题、粗体/斜体/删除线、行内代码、围栏代码块、
    无序/有序列表、引用块、水平线、链接、普通段落。
    """
    _BLOCK_RE = re.compile(r"^(#{1,6}\s|```|[ \t]*[-*+]\s|[ \t]*\d+\.\s|>)")
    _HR_RE = re.compile(r"^(---+|===+|\*\*\*+)\s*$")

    def _inline(s: str) -> str:
        s = html.escape(s)
        _stash: list = []

        def _save(m: re.Match) -> str:
            _stash.append(m.group(1))
            return f"\x00C{len(_stash) - 1}\x00"

        s = re.sub(r"`([^`\n]+)`", _save, s)
        s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"__(.+?)__", r"<strong>\1</strong>", s)
        s = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", s)
        s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<em>\1</em>", s)
        s = re.sub(r"~~(.+?)~~", r"<del>\1</del>", s)
        s = re.sub(
            r"\[([^\]\n]+)\]\(([^)\n]+)\)",
            lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
            s,
        )
        for idx, raw in enumerate(_stash):
            s = s.replace(f"\x00C{idx}\x00", f"<code>{raw}</code>")
        return s

    lines = text.split("\n")
    parts: list = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]

        if line.startswith("```"):
            lang = line[3:].strip()
            code_lines: list = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1
            code_body = html.escape("\n".join(code_lines))
            lang_attr = f' class="language-{html.escape(lang)}"' if lang else ""
            parts.append(f"<pre><code{lang_attr}>{code_body}</code></pre>")
            continue

        h_m = re.match(r"^(#{1,6})\s+(.*)", line)
        if h_m:
            lvl = len(h_m.group(1))
            parts.append(f"<h{lvl}>{_inline(h_m.group(2))}</h{lvl}>")
            i += 1
            continue

        if _HR_RE.match(line):
            parts.append("<hr>")
            i += 1
            continue

        if re.match(r"^[ \t]*[-*+]\s", line):
            items: list = []
            while i < n and re.match(r"^[ \t]*[-*+]\s", lines[i]):
                content = re.sub(r"^[ \t]*[-*+]\s+", "", lines[i])
                items.append(f"<li>{_inline(content)}</li>")
                i += 1
            parts.append("<ul>" + "".join(items) + "</ul>")
            continue

        if re.match(r"^[ \t]*\d+\.\s", line):
            items = []
            while i < n and re.match(r"^[ \t]*\d+\.\s", lines[i]):
                content = re.sub(r"^[ \t]*\d+\.\s+", "", lines[i])
                items.append(f"<li>{_inline(content)}</li>")
                i += 1
            parts.append("<ol>" + "".join(items) + "</ol>")
            continue

        if line.startswith(">"):
            bq: list = []
            while i < n and lines[i].startswith(">"):
                bq.append(_inline(lines[i][1:].lstrip()))
                i += 1
            parts.append("<blockquote>" + "<br>".join(bq) + "</blockquote>")
            continue

        if not line.strip():
            i += 1
            continue

        para: list = []
        while (
            i < n
            and lines[i].strip()
            and not _BLOCK_RE.match(lines[i])
            and not lines[i].startswith("```")
            and not _HR_RE.match(lines[i])
        ):
            para.append(_inline(lines[i]))
            i += 1
        if para:
            parts.append("<p>" + "<br>".join(para) + "</p>")

    return "\n".join(parts)


class RSSScheduler:
    """Manages RSS fetch and digest scheduling using AstrBot's CronJobManager."""

    JOB_PREFIX_FETCH = "rss_fetch_"
    JOB_PREFIX_DIGEST = "rss_digest_"

    def __init__(
        self,
        context: "Context",
        db: Database,
        fetcher: RSSFetcher,
        config: AstrBotConfig,
    ):
        self.context = context
        self.db = db
        self.fetcher = fetcher
        self.config = config

    def _is_webhook(self, umo: str) -> bool:
        """Check if UMO is a webhook URL."""
        return umo.startswith("http://") or umo.startswith("https://")

    @staticmethod
    def _truncate_by_bytes(text: str, max_bytes: int) -> str:
        """Truncate text to max bytes."""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    @staticmethod
    def _get_article_retention_cutoff(retention_days: int) -> datetime | None:
        """Return the UTC cutoff for article retention filtering."""
        if retention_days <= 0:
            return None
        return datetime.now(timezone.utc) - timedelta(days=retention_days)

    @staticmethod
    def _normalize_article_time(article: RSSArticle) -> datetime | None:
        """Normalize article time for retention checks."""
        article_time = article.published_at or article.fetched_at
        if not article_time:
            return None
        if article_time.tzinfo is None:
            return article_time.replace(tzinfo=timezone.utc)
        return article_time.astimezone(timezone.utc)

    def _filter_expired_articles(
        self, articles: list[RSSArticle], retention_days: int
    ) -> list[RSSArticle]:
        """Drop articles older than the configured retention window."""
        cutoff = self._get_article_retention_cutoff(retention_days)
        if cutoff is None:
            return articles

        filtered_articles = []
        for article in articles:
            article_time = self._normalize_article_time(article)
            if article_time is None or article_time >= cutoff:
                filtered_articles.append(article)

        return filtered_articles

    async def _send_webhook(
        self,
        webhook_url: str,
        text: str,
        timeout: int = 10,
    ) -> bool:
        """Send message to webhook (WeCom bot markdown format)."""
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

    def _load_digest_template(self) -> str:
        tmpl_path = os.path.join(os.path.dirname(__file__), "digest_template.jinja2")
        with open(tmpl_path, encoding="utf-8") as f:
            return f.read()

    def _make_template_data(self, text: str) -> dict:
        html_content = markdown_to_html(text)
        return {
            "content": html_content,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def _get_t2i_options(self) -> dict:
        """Get t2i rendering options from configuration."""
        output_config = self.config.get("output_config", {})
        return {
            "type": output_config.get("t2i_image_type", "jpeg"),
            "quality": output_config.get("t2i_image_quality", 70),
            "full_page": output_config.get("t2i_full_page", True),
            "scale": output_config.get("t2i_scale", "device"),
        }

    async def _render_digest_image(self, text: str, return_url: bool = False) -> str:
        """Render digest markdown to an image using the custom template.

        Args:
            text: Markdown digest text.
            return_url: If True, returns a remote URL; if False, returns a local file path.

        Returns:
            URL or file path of the rendered image.

        Raises:
            Exception: Propagates rendering errors so callers can fall back.
        """
        from astrbot.core import html_renderer

        tmpl_str = self._load_digest_template()
        tmpl_data = self._make_template_data(text)
        t2i_options = self._get_t2i_options()
        return await html_renderer.render_custom_template(
            tmpl_str,
            tmpl_data,
            return_url=return_url,
            options=t2i_options,
        )

    async def _send_webhook_image(
        self, webhook_url: str, text: str, timeout: int = 30
    ) -> bool:
        """Render digest as image using the custom template and send to WeCom webhook.

        Falls back to markdown if rendering fails or the resulting JPEG exceeds 2 MB.
        """
        try:
            image_path: str = await self._render_digest_image(text, return_url=False)

            image_bytes: bytes = await asyncio.to_thread(
                lambda: open(image_path, "rb").read()
            )

            # Validate image format using PIL
            try:
                await asyncio.to_thread(
                    lambda: Image.open(open(image_path, "rb")).verify()
                )
            except Exception as img_err:
                raise ValueError(
                    f"Rendered file is not a valid image: {img_err}. "
                    "T2I service may have returned an error response."
                ) from img_err

            if len(image_bytes) > 2 * 1024 * 1024:
                logger.warning(
                    "t2i image exceeds WeCom 2 MB limit (%d bytes), "
                    "falling back to markdown",
                    len(image_bytes),
                )
                return await self._send_webhook(webhook_url, text, timeout)

            b64_data = base64.b64encode(image_bytes).decode("utf-8")
            md5_hash = hashlib.md5(image_bytes).hexdigest()

            payload = {
                "msgtype": "image",
                "image": {"base64": b64_data, "md5": md5_hash},
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
                        logger.warning(f"Webhook image error: {result}")
                    else:
                        logger.warning(f"Webhook image status: {resp.status}")

        except Exception as e:
            logger.error(f"Webhook image send failed: {e}", exc_info=True)
            return await self._send_webhook(webhook_url, text, timeout)

        return False

    async def _send_to_subscriber(
        self,
        umo: str,
        message_chain: MessageChain,
        text_content: str | None = None,
    ) -> bool:
        """Send message to subscriber, handling both platform and webhook."""
        try:
            if self._is_webhook(umo):
                if text_content:
                    return await self._send_webhook(umo, text_content)
                logger.warning("Webhook requires text_content for markdown format")
                return False

            session = MessageSession.from_str(umo)
            await self.context.send_message(session, message_chain)
            return True
        except Exception as e:
            logger.error(f"Failed to send message to {umo}: {e}")
            return False

    async def start(self) -> None:
        """Initialize scheduler and restore all jobs from database."""
        await self._restore_jobs()

    async def _restore_jobs(self) -> None:
        """Restore all jobs from database on plugin startup."""
        subscriptions = await self.db.get_all_subscriptions()
        for sub in subscriptions:
            await self.schedule_subscription_fetch(sub)

        groups = await self.db.get_all_groups()
        for group in groups:
            for schedule in group.schedules:
                await self.schedule_digest(group, schedule)

        logger.info(
            f"Restored {len(subscriptions)} fetch jobs and digest jobs for {len(groups)} groups"
        )

    def _make_fetch_job_name(self, subscription_id: int) -> str:
        return f"{self.JOB_PREFIX_FETCH}{subscription_id}"

    def _make_digest_job_name(self, group_id: int, schedule: str) -> str:
        safe_schedule = schedule.replace(":", "_")
        return f"{self.JOB_PREFIX_DIGEST}{group_id}_{safe_schedule}"

    def _interval_to_cron(self, interval_minutes: int) -> str:
        """Convert interval in minutes to cron expression."""
        if interval_minutes >= 60:
            hours = interval_minutes // 60
            return f"0 */{hours} * * *"
        return f"*/{interval_minutes} * * * *"

    def _schedule_to_cron(self, time_str: str) -> str:
        """Convert HH:MM to cron expression for daily execution."""
        parts = time_str.split(":")
        hour = int(parts[0]) if len(parts) > 0 else 0
        minute = int(parts[1]) if len(parts) > 1 else 0
        return f"{minute} {hour} * * *"

    async def _delete_job_by_name(self, job_name: str) -> None:
        """Delete a job by its name from both scheduler and database."""
        jobs = await self.context.cron_manager.list_jobs(job_type="basic")
        for job in jobs:
            if job.name == job_name:
                await self.context.cron_manager.delete_job(job.job_id)
                logger.info(f"Deleted existing job: {job_name} (id: {job.job_id})")
                return

    async def schedule_subscription_fetch(self, subscription: RSSSubscription) -> None:
        """Schedule or update fetch job for a subscription."""
        job_name = self._make_fetch_job_name(subscription.id)

        await self._delete_job_by_name(job_name)

        cron_expr = self._interval_to_cron(subscription.interval)

        await self.context.cron_manager.add_basic_job(
            name=job_name,
            cron_expression=cron_expr,
            handler=self._fetch_subscription_handler,
            payload={"subscription_id": subscription.id},
            description=f"RSS订阅抓取: {subscription.name}",
            persistent=True,
        )

        logger.info(
            f"Scheduled fetch job for subscription {subscription.id} ({subscription.name}) every {subscription.interval} minutes"
        )

    async def _fetch_subscription_handler(self, subscription_id: int) -> None:
        """Handler for fetch job execution."""
        try:
            subscription = await self.db.get_subscription(subscription_id)
            if not subscription:
                logger.warning(
                    f"Subscription {subscription_id} not found, skipping fetch"
                )
                return

            if subscription.stop:
                logger.debug(
                    f"Subscription {subscription_id} ({subscription.name}) is stopped, skipping fetch"
                )
                return

            fetch_config = self.config.get("fetch_config", {})
            max_error_count = fetch_config.get("max_error_count", 100)
            retention_days = self.config.get("storage_config", {}).get(
                "article_retention_days", 30
            )

            if subscription.error_count >= max_error_count:
                logger.warning(
                    f"Subscription {subscription_id} ({subscription.name}) exceeded max errors "
                    f"({subscription.error_count}/{max_error_count}), stopping subscription"
                )

                subscription.stop = True
                await self.db.update_subscription(subscription)

                subscribers = await self.db.get_subscribers(subscription.id)
                for subscriber in subscribers:
                    if (subscriber.personal_config or {}).get("stop", False):
                        continue

                    message = MessageChain().message(
                        f"⚠️ 订阅 **{subscription.name}** 已因连续 {max_error_count} 次抓取失败而停止。\n"
                        f"请检查订阅源是否正常，或使用 `/rsstrigger {subscription.id}` 手动触发恢复。"
                    )
                    await self._send_to_subscriber(subscriber.umo, message)

                return

            result = await self.fetcher.fetch_feed(
                url=subscription.url,
                etag=subscription.etag,
                last_modified=subscription.last_modified,
                cookies=subscription.cookies,
            )

            if not result.success:
                subscription.error_count += 1
                await self.db.update_subscription(subscription)
                logger.error(f"Failed to fetch {subscription.name}: {result.error}")
            else:
                result.articles = self._filter_expired_articles(
                    result.articles, retention_days
                )
                subscription.etag = result.etag
                subscription.last_modified = result.last_modified
                subscription.last_fetch = (
                    result.articles[0].fetched_at if result.articles else None
                )
                subscription.error_count = 0
                await self.db.update_subscription(subscription)

                new_count = 0
                for article in result.articles:
                    exists = await self.db.article_exists(
                        subscription_id, article.guid, article.link
                    )
                    if not exists:
                        article.subscription_id = subscription_id
                        await self.db.add_article(article)
                        new_count += 1
                if new_count > 0:
                    logger.info(
                        f"Fetched {len(result.articles)} articles, {new_count} new for {subscription.name}"
                    )
                else:
                    logger.debug(
                        f"Fetched {len(result.articles)} articles, None new for {subscription.name}"
                    )
            if not subscription.ai_summary_enabled:
                await self._send_articles_to_subscribers(subscription)

        except Exception as e:
            logger.error(
                f"Error in fetch handler for subscription {subscription_id}: {e}",
                exc_info=True,
            )

    async def _send_articles_to_subscribers(
        self, subscription: RSSSubscription
    ) -> None:
        """Send all unsent articles to each subscriber.

        Gets unsent articles for each subscriber from database and sends
        with personalization applied.
        """
        from .models import get_effective_bool, get_effective_text

        subscribers = await self.db.get_subscribers(subscription.id)
        if not subscribers:
            return

        for subscriber in subscribers:
            if (subscriber.personal_config or {}).get("stop", False):
                continue

            articles = await self.db.get_unsent_articles_for_subscriber(
                subscription.id, subscriber.id
            )
            if not articles:
                continue

            for article in articles:
                black_keyword = get_effective_text(
                    subscriber, "black_keyword", subscription
                )
                if black_keyword:
                    keywords = [
                        k.strip() for k in black_keyword.split(",") if k.strip()
                    ]
                    title_lower = (article.title or "").lower()
                    content_lower = (article.content or "").lower()
                    if any(
                        kw.lower() in title_lower or kw.lower() in content_lower
                        for kw in keywords
                    ):
                        await self.db.mark_articles_sent_to_subscriber(
                            subscriber.id, [article.id]
                        )
                        continue

                only_has_pic = get_effective_bool(
                    subscriber, "only_has_pic", subscription
                )
                if only_has_pic and not article.image_urls:
                    await self.db.mark_articles_sent_to_subscriber(
                        subscriber.id, [article.id]
                    )
                    continue

                only_title = get_effective_bool(subscriber, "only_title", subscription)
                only_pic = get_effective_bool(subscriber, "only_pic", subscription)
                enable_spoiler = get_effective_bool(
                    subscriber, "enable_spoiler", subscription
                )

                message_chain = self._build_article_message(
                    article,
                    subscription,
                    only_title=only_title,
                    only_pic=only_pic,
                    enable_spoiler=enable_spoiler,
                )

                text_content = self._build_article_text(
                    article,
                    subscription,
                    only_title=only_title,
                )

                success = await self._send_to_subscriber(
                    subscriber.umo, message_chain, text_content
                )
                if success:
                    await self.db.mark_articles_sent_to_subscriber(
                        subscriber.id, [article.id]
                    )
                else:
                    logger.warning(
                        f"Failed to send article {article.id} to {subscriber.umo}"
                    )

    def _add_image(self, message_chain, url: str, use_spoiler: bool = False) -> None:
        """Add image to message chain, compatible with old AstrBot versions."""
        try:
            message_chain.url_image(url=url, use_spoiler=use_spoiler)
        except TypeError:
            message_chain.url_image(url=url)

    def _build_article_message(
        self,
        article: RSSArticle,
        subscription: RSSSubscription,
        only_title: bool = False,
        only_pic: bool = False,
        enable_spoiler: bool = False,
    ):
        """Build a message chain for an article."""
        from astrbot.core.message.message_event_result import MessageChain

        message_chain = MessageChain()
        via_name = article.author if article.author else subscription.name
        via_line = f"via [{via_name}]({article.link})"
        if only_pic and article.image_urls:
            for img_url in article.image_urls:
                self._add_image(message_chain, img_url, enable_spoiler)
            message_chain.message(via_line)
            return message_chain

        title = article.title or "Untitled"

        if only_title:
            message_chain.message(f"📰 **{title}**\n{via_line}")
        else:
            content = article.content or ""
            max_content_len = 500
            if len(content) > max_content_len:
                content = content[:max_content_len] + "..."

            message_chain.message(f"📰 **{title}**\n\n{content}\n\n🔗 {via_line}")

            if article.image_urls:
                max_images = subscription.max_image_number or self.config.get(
                    "fetch_config", {}
                ).get("max_image_number", 0)
                images_to_send = (
                    article.image_urls[:max_images]
                    if max_images > 0
                    else article.image_urls
                )
                for img_url in images_to_send:
                    self._add_image(message_chain, img_url, enable_spoiler)

        return message_chain

    def _build_article_text(
        self,
        article: RSSArticle,
        subscription: RSSSubscription,
        only_title: bool = False,
    ) -> str:
        """Build text content for webhook (WeCom markdown format)."""
        title = article.title or "Untitled"
        link = article.link or ""
        via_name = article.author if article.author else subscription.name
        via_line = f"[{via_name}]({link})"

        if only_title:
            return f"📰 **{title}**\nvia {via_line}"

        content = article.content or ""
        max_content_len = 500
        if len(content) > max_content_len:
            content = content[:max_content_len] + "..."

        text = f"📰 **{title}**\n\n{content}\n\n🔗 via {via_line}"

        if article.image_urls:
            text += f"\n\n📷 {len(article.image_urls)} image(s)"

        return text

    async def remove_subscription_job(self, subscription_id: int) -> None:
        """Remove fetch job for a subscription."""
        job_name = self._make_fetch_job_name(subscription_id)
        await self._delete_job_by_name(job_name)

    async def schedule_digest(self, group: RSSGroup, time_str: str) -> None:
        """Schedule a digest job for a group at specific time."""
        job_name = self._make_digest_job_name(group.id, time_str)

        await self._delete_job_by_name(job_name)

        cron_expr = self._schedule_to_cron(time_str)

        await self.context.cron_manager.add_basic_job(
            name=job_name,
            cron_expression=cron_expr,
            timezone="Asia/Shanghai",
            handler=self._digest_handler,
            payload={"group_id": group.id, "schedule": time_str},
            description=f"RSS分组摘要推送: {group.name} @ {time_str}",
            persistent=True,
        )

        logger.info(
            f"Scheduled digest job for group {group.id} ({group.name}) at {time_str}"
        )

    async def _digest_handler(self, group_id: int, schedule: str) -> None:
        """Handler for digest job execution."""
        try:
            group = await self.db.get_group(group_id)
            if not group:
                logger.warning(f"Group {group_id} not found, skipping digest")
                return

            # Get all subscriptions in this group
            subscriptions = await self.db.get_subscriptions_by_group(group_id)
            if not subscriptions:
                logger.info(f"No subscriptions in group {group_id}, skipping digest")
                return

            recent_days = self.config.get("ai_config", {}).get(
                "ai_digest_recent_days", 0
            )

            # Collect unsent articles per-subscriber, deduplicated by article ID.
            # Must use get_unsent_articles_for_subscriber (checks article_sent table)
            # instead of get_unsent_articles (checks legacy is_sent column, never updated).
            # The recent-days window is applied here so retrieval, digest input, and sent
            # marking all operate on the same article set.
            all_articles: list[RSSArticle] = []
            seen_article_ids: set[int] = set()
            for sub in subscriptions:
                sub_subscribers = await self.db.get_subscribers(sub.id)
                for subscriber in sub_subscribers:
                    unsent = await self.db.get_unsent_articles_for_subscriber(
                        sub.id, subscriber.id, recent_days=recent_days
                    )
                    for article in unsent:
                        if article.id not in seen_article_ids:
                            all_articles.append(article)
                            seen_article_ids.add(article.id)

            if not all_articles:
                logger.info(f"No new articles for digest in group {group_id}")
                return

            # Import digest service here to avoid circular import
            from .digest import DigestService

            digest_service = DigestService(self.context, self.db, self.config)

            # Generate digest
            digest_content, trimmed_count = await digest_service.generate_digest(
                all_articles, group_id
            )

            # Send to all subscribers
            await self._send_digest_to_subscribers(
                group, subscriptions, all_articles, digest_content
            )

            logger.info(
                f"Sent digest for group {group_id} with {trimmed_count} articles"
            )

        except Exception as e:
            logger.error(
                f"Error in digest handler for group {group_id}: {e}", exc_info=True
            )

    async def _send_digest_to_subscribers(
        self,
        group: RSSGroup,
        subscriptions: list[RSSSubscription],
        articles: list[RSSArticle],
        digest_content: str,
    ) -> None:
        """Send digest to all subscribers of the group and mark articles as sent.

        One AI digest is generated for the same article group and sent to all
        related subscribers. After sending, articles are marked as sent per-subscriber.
        """
        # Build a map of UMO -> all subscriber records across subscriptions.
        # One UMO may subscribe to multiple feeds, and each subscription has its
        # own subscriber id for per-subscription article_sent tracking.
        umo_to_subscribers: dict[str, list[Subscriber]] = {}
        for sub in subscriptions:
            subscribers = await self.db.get_subscribers(sub.id)
            for sub_obj in subscribers:
                umo_to_subscribers.setdefault(sub_obj.umo, []).append(sub_obj)

        article_ids_by_subscription: dict[int, list[int]] = {}
        for article in articles:
            if article.id:
                article_ids_by_subscription.setdefault(
                    article.subscription_id, []
                ).append(article.id)

        output_config = self.config.get("output_config", {})
        t2i_webhook = output_config.get("t2i_webhook_enabled", False)
        t2i_platform = output_config.get("t2i_platform_enabled", False)

        # Send to each subscriber
        for umo, subscriber_records in umo_to_subscribers.items():
            active_subscribers = [
                subscriber
                for subscriber in subscriber_records
                if not (subscriber.personal_config or {}).get("stop", False)
            ]

            if not active_subscribers:
                # Still mark as sent even if stopped to prevent stale backlogs.
                await self._mark_digest_articles_sent(
                    subscriber_records, article_ids_by_subscription
                )
                continue

            if self._is_webhook(umo):
                if t2i_webhook:
                    success = await self._send_webhook_image(umo, digest_content)
                else:
                    success = await self._send_webhook(umo, digest_content)
            else:
                if t2i_platform:
                    try:
                        image_url = await self._render_digest_image(
                            digest_content, return_url=True
                        )
                        message_chain = MessageChain().url_image(image_url)
                        success = await self._send_to_subscriber(umo, message_chain)
                    except Exception as e:
                        logger.warning(
                            f"Platform t2i render failed for {umo}: {e}, falling back to text"
                        )
                        message_chain = MessageChain().message(digest_content)
                        success = await self._send_to_subscriber(umo, message_chain)
                else:
                    message_chain = MessageChain().message(digest_content)
                    success = await self._send_to_subscriber(umo, message_chain)

            if success:
                await self._mark_digest_articles_sent(
                    subscriber_records, article_ids_by_subscription
                )
            else:
                logger.warning(f"Failed to send digest to {umo}")

    async def _mark_digest_articles_sent(
        self,
        subscribers: list[Subscriber],
        article_ids_by_subscription: dict[int, list[int]],
    ) -> None:
        """Mark digest articles sent for each subscriber's own subscription."""
        for subscriber in subscribers:
            article_ids = article_ids_by_subscription.get(
                subscriber.subscription_id, []
            )
            if article_ids:
                await self.db.mark_articles_sent_to_subscriber(
                    subscriber.id, article_ids
                )

    async def remove_digest_job(self, group_id: int, time_str: str) -> None:
        """Remove a digest job."""
        job_name = self._make_digest_job_name(group_id, time_str)
        await self._delete_job_by_name(job_name)

    async def remove_all_digest_jobs(self, group_id: int) -> None:
        """Remove all digest jobs for a group."""
        group = await self.db.get_group(group_id)
        if group:
            for schedule in group.schedules:
                await self.remove_digest_job(group_id, schedule)
