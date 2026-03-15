"""
Command handlers for RSS plugin.
"""

import logging
import re
from typing import TYPE_CHECKING, Optional

from astrbot.api.event import AstrMessageEvent, filter

from .database import Database
from .fetcher import RSSFetcher
from .models import RSSGroup, RSSSubscription, Subscriber
from .scheduler import RSSScheduler

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")


class RSSCommands:
    """RSS subscription commands."""

    def __init__(
        self,
        context: "Context",
        db: Database,
        scheduler: RSSScheduler,
        fetcher: RSSFetcher,
    ):
        self.context = context
        self.db = db
        self.scheduler = scheduler
        self.fetcher = fetcher

    async def rssadd(self, event: AstrMessageEvent, url: str, name: Optional[str] = None) -> None:
        """Add an RSS subscription."""
        umo = event.unified_msg_origin

        # Check if URL is valid
        if not url.startswith(("http://", "https://")):
            # Maybe it's an RSSHub path
            if self.fetcher.rsshub_url:
                url = self.fetcher.build_rsshub_url(url)
            else:
                await event.send_event_result(
                    event.make_result().message("❌ Invalid URL. Must start with http:// or https://")
                )
                return

        # Check if already exists
        existing = await self.db.get_subscription_by_url(url)
        if existing:
            # Add subscriber to existing subscription
            subscriber = Subscriber(subscription_id=existing.id, umo=umo)
            result = await self.db.add_subscriber(subscriber)
            if result:
                await event.send_event_result(
                    event.make_result().message(f"✅ You have been subscribed to: {existing.name}")
                )
            else:
                await event.send_event_result(
                    event.make_result().message(f"ℹ️ You are already subscribed to: {existing.name}")
                )
            return

        # Fetch feed to get title if name not provided
        if not name:
            result = await self.fetcher.fetch_feed(url)
            if result.success and result.articles:
                # Try to get feed title from parsed feed
                name = url.split("/")[-1] or "Untitled"
            else:
                name = url.split("/")[-1] or "Untitled"

        # Create subscription
        config = self.context.get_config() or {}
        interval = config.get("default_interval", 5)

        subscription = RSSSubscription(
            name=name,
            url=url,
            interval=interval,
        )
        sub_id = await self.db.add_subscription(subscription)
        subscription.id = sub_id

        # Add subscriber
        subscriber = Subscriber(subscription_id=sub_id, umo=umo)
        await self.db.add_subscriber(subscriber)

        # Schedule fetch job
        await self.scheduler.schedule_subscription_fetch(subscription)

        await event.send_event_result(
            event.make_result().message(f"✅ Subscription added: {name}\nURL: {url}\nInterval: {interval} minutes")
        )

    async def rssdel(self, event: AstrMessageEvent, name_or_id: str) -> None:
        """Delete an RSS subscription or remove subscriber."""
        umo = event.unified_msg_origin

        # Try to find by ID first
        try:
            sub_id = int(name_or_id)
            subscription = await self.db.get_subscription(sub_id)
        except ValueError:
            # Search by name
            subscription = None
            all_subs = await self.db.get_all_subscriptions()
            for sub in all_subs:
                if sub.name == name_or_id:
                    subscription = sub
                    break

        if not subscription:
            await event.send_event_result(
                event.make_result().message(f"❌ Subscription not found: {name_or_id}")
            )
            return

        # Check if user is subscriber
        subscriber = await self.db.get_subscriber(subscription.id, umo)
        if subscriber:
            await self.db.delete_subscriber(subscription.id, umo)
            # Check if any subscribers left
            remaining = await self.db.get_subscribers(subscription.id)
            if not remaining:
                # No more subscribers, delete the subscription
                await self.scheduler.remove_subscription_job(subscription.id)
                await self.db.delete_subscription(subscription.id)
                await event.send_event_result(
                    event.make_result().message(f"✅ Subscription deleted: {subscription.name} (no more subscribers)")
                )
            else:
                await event.send_event_result(
                    event.make_result().message(f"✅ You have been unsubscribed from: {subscription.name}")
                )
        else:
            await event.send_event_result(
                event.make_result().message(f"❌ You are not subscribed to: {subscription.name}")
            )

    async def rsslist(self, event: AstrMessageEvent) -> None:
        """List all RSS subscriptions."""
        umo = event.unified_msg_origin
        all_subs = await self.db.get_all_subscriptions()

        if not all_subs:
            await event.send_event_result(
                event.make_result().message("📭 No subscriptions yet.\nUse /rssadd <url> to add one.")
            )
            return

        lines = ["📰 Your RSS Subscriptions:\n"]

        for sub in all_subs:
            subscribers = await self.db.get_subscribers(sub.id)
            is_subscribed = any(s.umo == umo for s in subscribers)
            status = "✅" if is_subscribed else "⚪"

            lines.append(f"{status} **{sub.name}** (ID: {sub.id})")
            lines.append(f"   URL: {sub.url}")
            lines.append(f"   Interval: {sub.interval} min, Subscribers: {len(subscribers)}")
            lines.append("")

        await event.send_event_result(
            event.make_result().message("\n".join(lines))
        )

    async def rssupdate(
        self,
        event: AstrMessageEvent,
        name_or_id: str,
        config_key: Optional[str] = None,
        config_value: Optional[str] = None,
    ) -> None:
        """Update subscription configuration."""
        umo = event.unified_msg_origin

        # Find subscription
        try:
            sub_id = int(name_or_id)
            subscription = await self.db.get_subscription(sub_id)
        except ValueError:
            subscription = None
            all_subs = await self.db.get_all_subscriptions()
            for sub in all_subs:
                if sub.name == name_or_id:
                    subscription = sub
                    break

        if not subscription:
            await event.send_event_result(
                event.make_result().message(f"❌ Subscription not found: {name_or_id}")
            )
            return

        # Check if user is subscriber
        subscriber = await self.db.get_subscriber(subscription.id, umo)
        if not subscriber:
            await event.send_event_result(
                event.make_result().message(f"❌ You are not subscribed to: {subscription.name}")
            )
            return

        if not config_key:
            # Show current config
            config = subscriber.personal_config or {}
            lines = [f"⚙️ Configuration for **{subscription.name}**:\n"]
            lines.append(f"- only_title: {config.get('only_title', False)}")
            lines.append(f"- only_pic: {config.get('only_pic', False)}")
            lines.append(f"- only_has_pic: {config.get('only_has_pic', False)}")
            lines.append(f"- enable_spoiler: {config.get('enable_spoiler', False)}")
            lines.append(f"- stop: {config.get('stop', False)}")
            lines.append(f"- black_keyword: {config.get('black_keyword', '')}")
            lines.append("\nUsage: /rssupdate <name> <key> <value>")
            await event.send_event_result(
                event.make_result().message("\n".join(lines))
            )
            return

        # Update config
        valid_keys = ["only_title", "only_pic", "only_has_pic", "enable_spoiler", "stop", "black_keyword"]
        if config_key not in valid_keys:
            await event.send_event_result(
                event.make_result().message(f"❌ Invalid config key. Valid keys: {', '.join(valid_keys)}")
            )
            return

        # Parse value
        if config_key == "black_keyword":
            value = config_value or ""
        else:
            value = config_value.lower() in ("true", "1", "yes", "on") if config_value else False

        subscriber.personal_config[config_key] = value
        await self.db.update_subscriber(subscriber)

        await event.send_event_result(
            event.make_result().message(f"✅ Updated {config_key} = {value} for {subscription.name}")
        )

    async def rsstrigger(self, event: AstrMessageEvent, name_or_id: Optional[str] = None) -> None:
        """Manually trigger RSS update (admin only)."""
        if name_or_id:
            # Trigger specific subscription
            try:
                sub_id = int(name_or_id)
                subscription = await self.db.get_subscription(sub_id)
            except ValueError:
                subscription = None
                all_subs = await self.db.get_all_subscriptions()
                for sub in all_subs:
                    if sub.name == name_or_id:
                        subscription = sub
                        break

            if not subscription:
                await event.send_event_result(
                    event.make_result().message(f"❌ Subscription not found: {name_or_id}")
                )
                return

            # Trigger fetch
            await self.scheduler._fetch_subscription_handler(subscription.id)
            await event.send_event_result(
                event.make_result().message(f"✅ Triggered fetch for: {subscription.name}")
            )
        else:
            # Trigger all subscriptions
            all_subs = await self.db.get_all_subscriptions()
            for sub in all_subs:
                await self.scheduler._fetch_subscription_handler(sub.id)
            await event.send_event_result(
                event.make_result().message(f"✅ Triggered fetch for all {len(all_subs)} subscriptions")
            )


class GroupCommands:
    """RSS group management commands (admin only)."""

    def __init__(
        self,
        context: "Context",
        db: Database,
        scheduler: RSSScheduler,
    ):
        self.context = context
        self.db = db
        self.scheduler = scheduler

    async def group_add(self, event: AstrMessageEvent, name: str) -> None:
        """Create a new RSS group."""
        group = RSSGroup(name=name)
        group_id = await self.db.add_group(group)

        # Create persona for the group
        persona_id = f"rss_group_{group_id}"
        group.persona_id = persona_id
        group.id = group_id
        await self.db.update_group(group)

        # Check if persona already exists
        existing_persona = self.context.persona_manager.get_persona(persona_id)
        if not existing_persona:
            self.context.persona_manager.create_persona(
                name=persona_id,
                system_prompt="你是一个RSS文章摘要助手，请为用户整理和总结订阅的文章。",
            )

        await event.send_event_result(
            event.make_result().message(f"✅ Group created: {name} (ID: {group_id})\nPersona: {persona_id}")
        )

    async def group_rename(self, event: AstrMessageEvent, group_id: int, new_name: str) -> None:
        """Rename a group."""
        group = await self.db.get_group(group_id)
        if not group:
            await event.send_event_result(
                event.make_result().message(f"❌ Group not found: {group_id}")
            )
            return

        old_name = group.name
        group.name = new_name
        await self.db.update_group(group)

        await event.send_event_result(
            event.make_result().message(f"✅ Group renamed: {old_name} → {new_name}")
        )

    async def group_list(self, event: AstrMessageEvent) -> None:
        """List all groups."""
        groups = await self.db.get_all_groups()

        if not groups:
            await event.send_event_result(
                event.make_result().message("📭 No groups created yet.\nUse /rssgroup add <name> to create one.")
            )
            return

        lines = ["📂 RSS Groups:\n"]

        for group in groups:
            subscriptions = await self.db.get_subscriptions_by_group(group.id)
            lines.append(f"**{group.name}** (ID: {group.id})")
            lines.append(f"   Schedules: {', '.join(group.schedules) or 'None'}")
            lines.append(f"   Subscriptions: {len(subscriptions)}")
            lines.append(f"   Persona: {group.persona_id or 'Default'}")
            lines.append("")

        await event.send_event_result(
            event.make_result().message("\n".join(lines))
        )

    async def group_timeadd(self, event: AstrMessageEvent, group_id: int, time_str: str) -> None:
        """Add a digest schedule to a group."""
        # Validate time format
        if not re.match(r"^\d{1,2}:\d{2}$", time_str):
            await event.send_event_result(
                event.make_result().message("❌ Invalid time format. Use HH:MM (e.g., 09:00)")
            )
            return

        group = await self.db.get_group(group_id)
        if not group:
            await event.send_event_result(
                event.make_result().message(f"❌ Group not found: {group_id}")
            )
            return

        if time_str in group.schedules:
            await event.send_event_result(
                event.make_result().message(f"ℹ️ Schedule {time_str} already exists for group {group.name}")
            )
            return

        group.schedules.append(time_str)
        await self.db.update_group(group)

        # Schedule the digest job
        await self.scheduler.schedule_digest(group, time_str)

        await event.send_event_result(
            event.make_result().message(f"✅ Added schedule {time_str} to group {group.name}")
        )

    async def group_timedel(self, event: AstrMessageEvent, group_id: int, time_str: str) -> None:
        """Remove a digest schedule from a group."""
        group = await self.db.get_group(group_id)
        if not group:
            await event.send_event_result(
                event.make_result().message(f"❌ Group not found: {group_id}")
            )
            return

        if time_str not in group.schedules:
            await event.send_event_result(
                event.make_result().message(f"❌ Schedule {time_str} not found in group {group.name}")
            )
            return

        group.schedules.remove(time_str)
        await self.db.update_group(group)

        # Remove the digest job
        await self.scheduler.remove_digest_job(group_id, time_str)

        await event.send_event_result(
            event.make_result().message(f"✅ Removed schedule {time_str} from group {group.name}")
        )

    async def group_subadd(
        self,
        event: AstrMessageEvent,
        group_id: int,
        session_id: str,
    ) -> None:
        """Add a subscriber to a group."""
        group = await self.db.get_group(group_id)
        if not group:
            await event.send_event_result(
                event.make_result().message(f"❌ Group not found: {group_id}")
            )
            return

        # Get all subscriptions in the group and add subscriber to each
        subscriptions = await self.db.get_subscriptions_by_group(group_id)
        if not subscriptions:
            await event.send_event_result(
                event.make_result().message(f"❌ No subscriptions in group {group.name}")
            )
            return

        added_count = 0
        for sub in subscriptions:
            subscriber = Subscriber(subscription_id=sub.id, umo=session_id)
            result = await self.db.add_subscriber(subscriber)
            if result:
                added_count += 1

        await event.send_event_result(
            event.make_result().message(
                f"✅ Added subscriber to {added_count} subscriptions in group {group.name}\n"
                f"Session: {session_id}"
            )
        )

    async def group_subdel(
        self,
        event: AstrMessageEvent,
        group_id: int,
        session_id: str,
    ) -> None:
        """Remove a subscriber from a group."""
        group = await self.db.get_group(group_id)
        if not group:
            await event.send_event_result(
                event.make_result().message(f"❌ Group not found: {group_id}")
            )
            return

        # Remove subscriber from all subscriptions in the group
        subscriptions = await self.db.get_subscriptions_by_group(group_id)
        removed_count = 0
        for sub in subscriptions:
            await self.db.delete_subscriber(sub.id, session_id)
            removed_count += 1

        await event.send_event_result(
            event.make_result().message(
                f"✅ Removed subscriber from {removed_count} subscriptions in group {group.name}"
            )
        )