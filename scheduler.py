"""
Scheduler service for RSS plugin using AstrBot's CronJobManager.
"""

import logging
from typing import TYPE_CHECKING

import aiohttp

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

            config = self.context.get_config() or {}
            max_error_count = config.get("max_error_count", 100)

            if subscription.error_count >= max_error_count:
                logger.warning(
                    f"Subscription {subscription_id} ({subscription.name}) exceeded max errors "
                    f"({subscription.error_count}/{max_error_count}), skipping fetch"
                )
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

                logger.info(
                    f"Fetched {len(result.articles)} articles, {new_count} new for {subscription.name}"
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
            if subscriber.personal_config.get("stop", False):
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
                    "max_image_number", 0
                )
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

            # Collect unsent articles from all subscriptions
            all_articles: list[RSSArticle] = []
            for sub in subscriptions:
                articles = await self.db.get_unsent_articles(sub.id)
                all_articles.extend(articles)

            if not all_articles:
                logger.info(f"No new articles for digest in group {group_id}")
                return

            # Import digest service here to avoid circular import
            from .digest import DigestService

            digest_service = DigestService(self.context, self.db, self.config)

            # Generate digest
            digest_content = await digest_service.generate_digest(
                all_articles, group_id
            )

            # Send to all subscribers
            await self._send_digest_to_subscribers(
                group, subscriptions, all_articles, digest_content
            )

            logger.info(
                f"Sent digest for group {group_id} with {len(all_articles)} articles"
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
        # Build a map of UMO -> Subscriber for all subscribers across subscriptions
        umo_to_subscriber: dict[str, Subscriber] = {}
        for sub in subscriptions:
            subscribers = await self.db.get_subscribers(sub.id)
            for sub_obj in subscribers:
                if sub_obj.umo not in umo_to_subscriber:
                    umo_to_subscriber[sub_obj.umo] = sub_obj

        article_ids = [a.id for a in articles if a.id]

        # Send to each subscriber
        for umo, subscriber in umo_to_subscriber.items():
            if subscriber.personal_config.get("stop", False):
                # Still mark as sent even if stopped
                await self.db.mark_articles_sent_to_subscriber(
                    subscriber.id, article_ids
                )
                continue

            message_chain = MessageChain().message(digest_content)
            success = await self._send_to_subscriber(umo, message_chain, digest_content)

            if success:
                await self.db.mark_articles_sent_to_subscriber(
                    subscriber.id, article_ids
                )
            else:
                logger.warning(f"Failed to send digest to {umo}")

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
