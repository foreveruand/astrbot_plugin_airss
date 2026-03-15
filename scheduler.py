"""
Scheduler service for RSS plugin using AstrBot's CronJobManager.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from .database import Database
from .fetcher import RSSFetcher
from .models import RSSArticle, RSSGroup, RSSSubscription, Subscriber

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")


class RSSScheduler:
    """Manages RSS fetch and digest scheduling using AstrBot's CronJobManager."""

    JOB_PREFIX_FETCH = "rss_fetch_"
    JOB_PREFIX_DIGEST = "rss_digest_"

    def __init__(self, context: "Context", db: Database, fetcher: RSSFetcher):
        self.context = context
        self.db = db
        self.fetcher = fetcher

    async def start(self) -> None:
        """Initialize scheduler and restore all jobs from database."""
        await self._restore_jobs()

    async def _restore_jobs(self) -> None:
        """Restore all jobs from database on plugin startup."""
        # Restore fetch jobs
        subscriptions = await self.db.get_all_subscriptions()
        for sub in subscriptions:
            await self._schedule_fetch_job(sub)

        # Restore digest jobs
        groups = await self.db.get_all_groups()
        for group in groups:
            for schedule in group.schedules:
                await self._schedule_digest_job(group, schedule)

        logger.info(
            f"Restored {len(subscriptions)} fetch jobs and digest jobs for {len(groups)} groups"
        )

    def _make_fetch_job_id(self, subscription_id: int) -> str:
        return f"{self.JOB_PREFIX_FETCH}{subscription_id}"

    def _make_digest_job_id(self, group_id: int, schedule: str) -> str:
        # Replace : with _ for job ID
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

    async def schedule_subscription_fetch(self, subscription: RSSSubscription) -> None:
        """Schedule or update fetch job for a subscription."""
        job_id = self._make_fetch_job_id(subscription.id)

        # Remove existing job if any
        await self.remove_subscription_job(subscription.id)

        # Create new job
        cron_expr = self._interval_to_cron(subscription.interval)

        await self.context.cron_manager.add_basic_job(
            name=f"RSS Fetch: {subscription.name}",
            cron_expression=cron_expr,
            handler=self._fetch_subscription_handler,
            payload={"subscription_id": subscription.id},
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

            # Check max error count threshold
            config = self.context.get_config() or {}
            max_error_count = config.get("max_error_count", 100)

            if subscription.error_count >= max_error_count:
                logger.warning(
                    f"Subscription {subscription_id} ({subscription.name}) exceeded max errors "
                    f"({subscription.error_count}/{max_error_count}), skipping fetch"
                )
                return

            # Fetch the feed
            result = await self.fetcher.fetch_feed(
                url=subscription.url,
                etag=subscription.etag,
                last_modified=subscription.last_modified,
                cookies=subscription.cookies,
            )

            if not result.success:
                # Increment error count
                subscription.error_count += 1
                await self.db.update_subscription(subscription)
                logger.error(f"Failed to fetch {subscription.name}: {result.error}")
                return

            # Update ETag/Last-Modified
            subscription.etag = result.etag
            subscription.last_modified = result.last_modified
            subscription.last_fetch = (
                result.articles[0].fetched_at if result.articles else None
            )
            subscription.error_count = 0
            await self.db.update_subscription(subscription)

            # Store new articles
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

        except Exception as e:
            logger.error(
                f"Error in fetch handler for subscription {subscription_id}: {e}",
                exc_info=True,
            )

    async def remove_subscription_job(self, subscription_id: int) -> None:
        """Remove fetch job for a subscription."""
        job_id = self._make_fetch_job_id(subscription_id)
        try:
            await self.context.cron_manager.delete_job(job_id)
            logger.info(f"Removed fetch job for subscription {subscription_id}")
        except Exception:
            pass  # Job might not exist

    async def schedule_digest(self, group: RSSGroup, time_str: str) -> None:
        """Schedule a digest job for a group at specific time."""
        job_id = self._make_digest_job_id(group.id, time_str)

        # Remove existing job for this time if any
        await self.remove_digest_job(group.id, time_str)

        cron_expr = self._schedule_to_cron(time_str)

        await self.context.cron_manager.add_basic_job(
            name=f"RSS Digest: {group.name} at {time_str}",
            cron_expression=cron_expr,
            handler=self._digest_handler,
            payload={"group_id": group.id, "schedule": time_str},
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

            digest_service = DigestService(self.context, self.db)

            # Generate digest
            digest_content = await digest_service.generate_digest(
                all_articles, group_id
            )

            # Send to all subscribers
            await self._send_digest_to_subscribers(
                group, subscriptions, all_articles, digest_content
            )

            # Mark articles as sent
            article_ids = [a.id for a in all_articles if a.id]
            await self.db.mark_articles_sent(article_ids)

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
        """Send digest to all subscribers of the group with personalization."""
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform.message_session import MessageSession

        # Build a map of UMO -> Subscriber for all subscribers across subscriptions
        umo_to_subscriber: dict[str, Subscriber] = {}
        for sub in subscriptions:
            subscribers = await self.db.get_subscribers(sub.id)
            for sub_obj in subscribers:
                # Only keep first occurrence (prefer earlier subscriptions)
                if sub_obj.umo not in umo_to_subscriber:
                    umo_to_subscriber[sub_obj.umo] = sub_obj

        # Send to each subscriber with personalization
        tasks = []
        for umo, subscriber in umo_to_subscriber.items():
            # For digest, only check stop flag (full personalization applies to single articles)
            if subscriber.personal_config.get("stop", False):
                continue

            try:
                session = MessageSession.from_str(umo)
                message_chain = MessageChain().message(digest_content)
                tasks.append(self.context.send_message(session, message_chain))
            except Exception as e:
                logger.warning(f"Failed to create session for {umo}: {e}")

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def remove_digest_job(self, group_id: int, time_str: str) -> None:
        """Remove a digest job."""
        job_id = self._make_digest_job_id(group_id, time_str)
        try:
            await self.context.cron_manager.delete_job(job_id)
            logger.info(f"Removed digest job for group {group_id} at {time_str}")
        except Exception:
            pass  # Job might not exist

    async def remove_all_digest_jobs(self, group_id: int) -> None:
        """Remove all digest jobs for a group."""
        group = await self.db.get_group(group_id)
        if group:
            for schedule in group.schedules:
                await self.remove_digest_job(group_id, schedule)
