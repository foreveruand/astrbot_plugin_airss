"""
Command handlers for RSS plugin.
"""

import logging
import re
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent, MessageEventResult

from .database import Database
from .fetcher import RSSFetcher
from .models import (
    GLOBAL_CONFIGURABLE_FIELDS,
    PERSONAL_CONFIG_KEYS,
    TELEGRAM_ADAPTER,
    WEBHOOK_ADAPTER,
    WECOM_ADAPTER,
    RSSGroup,
    RSSSubscription,
    Subscriber,
)
from .scheduler import RSSScheduler

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")

# Personal config descriptions for display
PERSONAL_CONFIG_DESCRIPTIONS = {
    "only_title": "仅发送标题，不发送内容",
    "only_pic": "仅发送图片",
    "only_has_pic": "仅发送有图片的文章",
    "enable_spoiler": "图片使用剧透标签（隐藏）",
    "stop": "暂停订阅",
    "black_keyword": "关键词黑名单，多个用逗号分隔",
}

# Global config descriptions for display
GLOBAL_CONFIG_DESCRIPTIONS = {
    "name": "订阅名称",
    "url": "订阅地址",
    "black_keyword": "关键词黑名单，多个用逗号分隔",
    "cookies": "请求时携带的 Cookies",
    "content_to_remove": "正则表达式，移除匹配的内容",
    "max_image_number": "每篇文章最大图片数，0 为不限制",
    "interval": "抓取间隔（分钟）",
    "ai_summary_enabled": "是否启用 AI 摘要",
    "source_group_id": "所属分组 ID，用于定时摘要推送",
}

# Boolean fields that can be toggled
PERSONAL_CONFIG_BOOL_FIELDS = [
    "only_title",
    "only_pic",
    "only_has_pic",
    "enable_spoiler",
    "stop",
]

GLOBAL_CONFIG_BOOL_FIELDS = [
    "ai_summary_enabled",
    "enable_proxy",
    "stop",
]

# Text fields for global config
GLOBAL_CONFIG_TEXT_FIELDS = [
    "name",
    "url",
    "black_keyword",
    "cookies",
    "content_to_remove",
]

# Integer fields for global config
GLOBAL_CONFIG_INT_FIELDS = [
    "max_image_number",
    "interval",
    "source_group_id",
]


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

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """Check if the user is an admin."""
        # AstrBot uses role-based permissions
        return event.role in ("admin", "superuser")

    def _build_umo(
        self,
        target_id: str,
        adapter: str = TELEGRAM_ADAPTER,
        is_group: bool = False,
    ) -> str:
        """Build unified message origin string.

        Args:
            target_id: The user/group ID or webhook URL
            adapter: Adapter type (telegram, wecom, webhook)
            is_group: Whether this is a group

        Returns:
            UMO string in format "platform:message_type:session_id" or webhook URL
        """
        # If target starts with http, it's a webhook URL
        if target_id.startswith("http://") or target_id.startswith("https://"):
            return target_id

        # Build UMO from components
        message_type = "GroupMessage" if is_group else "PrivateMessage"
        return f"{adapter}:{message_type}:{target_id}"

    async def rssadd(
        self,
        event: AstrMessageEvent,
        url: str,
        name: str | None = None,
    ) -> None:
        """Add an RSS subscription.

        Usage:
            /rssadd <url> [name] - Add RSS subscription
            /rssadd -g <group_id> - Subscribe to entire group
            /rssadd -p <rsshub_path> - Print RSSHub URL only
        """
        umo = event.unified_msg_origin

        # Check if URL is valid
        if not url.startswith(("http://", "https://")):
            # Maybe it's an RSSHub path
            if self.fetcher.rsshub_url:
                url = self.fetcher.build_rsshub_url(url)
            else:
                event.set_result(MessageEventResult().message("❌ Invalid URL. Must start with http:// or https://"))
                return

        # Check if already exists
        existing = await self.db.get_subscription_by_url(url)
        if existing and existing.id is not None:
            # Add subscriber to existing subscription
            subscriber = Subscriber(subscription_id=existing.id, umo=umo)
            result = await self.db.add_subscriber(subscriber)
            if result:
                event.set_result(MessageEventResult().message(f"✅ You have been subscribed to: {existing.name}"))
            else:
                event.set_result(MessageEventResult().message(f"ℹ️ You are already subscribed to: {existing.name}"))
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

        event.set_result(MessageEventResult().message(f"✅ Subscription added: {name}\nURL: {url}\nInterval: {interval} minutes"))

    async def rssadd_group(self, event: AstrMessageEvent, group_id: int) -> None:
        """Subscribe to all feeds in a group."""
        umo = event.unified_msg_origin

        # Get all subscriptions in this group
        subscriptions = await self.db.get_subscriptions_by_group(group_id)
        if not subscriptions:
            event.set_result(MessageEventResult().message(f"❌ Group {group_id} has no subscriptions or doesn't exist"))
            return

        # Get group info
        group = await self.db.get_group(group_id)
        group_name = group.name if group else str(group_id)

        added_count = 0
        skipped_count = 0

        for sub in subscriptions:
            if sub.id is None:
                continue

            # Check if already subscribed
            existing = await self.db.get_subscriber(sub.id, umo)
            if existing:
                skipped_count += 1
                continue

            # Add subscriber
            subscriber = Subscriber(subscription_id=sub.id, umo=umo)
            await self.db.add_subscriber(subscriber)
            added_count += 1

        msg = f"✅ Subscribed to group **{group_name}**\n"
        msg += f"Added: {added_count} subscriptions\n"
        if skipped_count > 0:
            msg += f"Skipped (already subscribed): {skipped_count}"

        event.set_result(MessageEventResult().message(msg))

    async def rssadd_subscriber(
        self,
        event: AstrMessageEvent,
        subscription_id: int,
        target_id: str,
        adapter: str = TELEGRAM_ADAPTER,
        is_group: bool = False,
    ) -> None:
        """Add a subscriber to an existing subscription (admin only)."""
        # Check admin permission
        if not self._is_admin(event):
            event.set_result(MessageEventResult().message("❌ This command requires admin privileges"))
            return

        # Validate adapter
        if adapter not in (TELEGRAM_ADAPTER, WECOM_ADAPTER, WEBHOOK_ADAPTER):
            event.set_result(MessageEventResult().message(f"❌ Unsupported adapter: {adapter}. Use telegram, wecom, or webhook"))
            return

        # Get subscription
        subscription = await self.db.get_subscription(subscription_id)
        if not subscription:
            event.set_result(MessageEventResult().message(f"❌ Subscription ID {subscription_id} not found"))
            return

        # Build UMO
        umo = self._build_umo(target_id, adapter, is_group)

        # Check if already exists
        existing = await self.db.get_subscriber(subscription_id, umo)
        if existing:
            event.set_result(MessageEventResult().message(f"❌ {target_id} is already subscribed to {subscription.name}"))
            return

        # Add subscriber
        subscriber = Subscriber(subscription_id=subscription_id, umo=umo)
        await self.db.add_subscriber(subscriber)

        # Refresh scheduler
        await self.scheduler.schedule_subscription_fetch(subscription)

        adapter_type = (
            "webhook"
            if adapter == WEBHOOK_ADAPTER
            else ("group" if is_group else "user")
        )
        event.set_result(MessageEventResult().message(f"✅ Added {adapter_type} {target_id} to subscription '{subscription.name}'"))

    async def rssdel(self, event: AstrMessageEvent, name_or_id: str) -> None:
        """Delete an RSS subscription or remove subscriber.

        Usage:
            /rssdel <name|id> - Delete subscription (or unsubscribe)
            /rssdel <subscription_id> <user/group_id> - Remove subscriber (admin)
        """
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

        if not subscription or subscription.id is None:
            event.set_result(MessageEventResult().message(f"❌ Subscription not found: {name_or_id}"))
            return

        sub_id = subscription.id

        # Check if user is subscriber
        subscriber = await self.db.get_subscriber(sub_id, umo)
        if subscriber:
            await self.db.delete_subscriber(sub_id, umo)
            # Check if any subscribers left
            remaining = await self.db.get_subscribers(sub_id)
            if not remaining:
                # No more subscribers, delete the subscription
                await self.scheduler.remove_subscription_job(sub_id)
                await self.db.delete_subscription(sub_id)
                event.set_result(MessageEventResult().message(f"✅ Subscription deleted: {subscription.name} (no more subscribers)"))
            else:
                event.set_result(MessageEventResult().message(f"✅ You have been unsubscribed from: {subscription.name}"))
        else:
            event.set_result(MessageEventResult().message(f"❌ You are not subscribed to: {subscription.name}"))

    async def rssdel_subscriber(
        self,
        event: AstrMessageEvent,
        subscription_id: int,
        target_id: str,
    ) -> None:
        """Remove a subscriber from a subscription (admin only)."""
        # Check admin permission
        if not self._is_admin(event):
            event.set_result(MessageEventResult().message("❌ This command requires admin privileges"))
            return

        # Get subscription
        subscription = await self.db.get_subscription(subscription_id)
        if not subscription:
            event.set_result(MessageEventResult().message(f"❌ Subscription ID {subscription_id} not found"))
            return

        # Try to find subscriber by UMO (exact match or by ID component)
        subscribers = await self.db.get_subscribers(subscription_id)
        found_umo = None

        # Check if target_id is a webhook URL
        if target_id.startswith("http://") or target_id.startswith("https://"):
            found_umo = target_id
        else:
            # Search for subscriber with matching ID in UMO
            for sub in subscribers:
                # UMO format: platform:message_type:session_id
                parts = sub.umo.split(":")
                if len(parts) >= 3 and parts[-1] == target_id:
                    found_umo = sub.umo
                    break

        if not found_umo:
            event.set_result(MessageEventResult().message(f"❌ Subscriber {target_id} not found in '{subscription.name}'"))
            return

        # Delete subscriber
        await self.db.delete_subscriber(subscription_id, found_umo)

        # Refresh scheduler
        await self.scheduler.schedule_subscription_fetch(subscription)

        event.set_result(MessageEventResult().message(f"✅ Removed {target_id} from subscription '{subscription.name}'"))

    async def rsslist(self, event: AstrMessageEvent) -> None:
        """List all RSS subscriptions."""
        umo = event.unified_msg_origin
        all_subs = await self.db.get_all_subscriptions()

        if not all_subs:
            event.set_result(MessageEventResult().message("📭 No subscriptions yet.\nUse /rssadd <url> to add one."))
            return

        lines = ["📰 Your RSS Subscriptions:\n"]

        for sub in all_subs:
            if sub.id is None:
                continue
            subscribers = await self.db.get_subscribers(sub.id)
            is_subscribed = any(s.umo == umo for s in subscribers)
            status = "✅" if is_subscribed else "⚪"

            lines.append(f"{status} **{sub.name}** (ID: {sub.id})")
            lines.append(f"   URL: {sub.url}")
            lines.append(
                f"   Interval: {sub.interval} min, Subscribers: {len(subscribers)}"
            )
            lines.append("")

        event.set_result(MessageEventResult().message("\n".join(lines)))

    async def rssupdate(
        self,
        event: AstrMessageEvent,
        name_or_id: str,
        config_key: str | None = None,
        config_value: str | None = None,
    ) -> None:
        """Update subscription configuration.

        Usage:
            /rssupdate <name|id> - Show personalization config
            /rssupdate <name|id> <key> <value> - Update personalization
        """
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

        if not subscription or subscription.id is None:
            event.set_result(MessageEventResult().message(f"❌ Subscription not found: {name_or_id}"))
            return

        sub_id = subscription.id

        # Check if user is subscriber
        subscriber = await self.db.get_subscriber(sub_id, umo)
        if not subscriber:
            event.set_result(MessageEventResult().message(f"❌ You are not subscribed to: {subscription.name}"))
            return

        if not config_key:
            # Show current config with descriptions
            config = subscriber.personal_config or {}
            lines = [
                f"⚙️ **[{subscription.name}]({subscription.url})** 配置\n",
                "| 参数 | 当前值 | 说明 |",
                "|------|--------|------|",
            ]
            for key, default in PERSONAL_CONFIG_KEYS.items():
                current = config.get(key, default)
                desc = PERSONAL_CONFIG_DESCRIPTIONS.get(key, "")
                if isinstance(current, bool):
                    current_str = "✅" if current else "❌"
                else:
                    current_str = f"`{current}`" if current else "`无`"
                lines.append(f"| `{key}` | {current_str} | {desc} |")

            lines.append("\n**用法**: `/rssupdate <名称|ID> <参数> <值>`")
            event.set_result(MessageEventResult().message("\n".join(lines)))
            return

        # Update config
        valid_keys = list(PERSONAL_CONFIG_KEYS.keys())
        if config_key not in valid_keys:
            lines = [
                f"❌ 无效参数: `{config_key}`\n",
                "**可用参数列表**:",
                "| 参数 | 说明 |",
                "|------|------|",
            ]
            for key, _ in PERSONAL_CONFIG_KEYS.items():
                desc = PERSONAL_CONFIG_DESCRIPTIONS.get(key, "")
                lines.append(f"| `{key}` | {desc} |")
            lines.append("\n**用法**: `/rssupdate <名称|ID> <参数> <值>`")
            event.set_result(MessageEventResult().message("\n".join(lines)))
            return

        # Parse value
        if config_key == "black_keyword":
            value = config_value or ""
        else:
            value = (
                config_value.lower() in ("true", "1", "yes", "on")
                if config_value
                else False
            )

        if subscriber.personal_config is None:
            subscriber.personal_config = {}
        subscriber.personal_config[config_key] = value
        await self.db.update_subscriber(subscriber)

        event.set_result(MessageEventResult().message(f"✅ Updated {config_key} = {value} for {subscription.name}"))

    async def rssupdate_global_list(self, event: AstrMessageEvent) -> None:
        """List all subscriptions for global config management (admin only)."""
        if not self._is_admin(event):
            event.set_result(MessageEventResult().message("❌ This command requires admin privileges"))
            return

        all_subs = await self.db.get_all_subscriptions()

        if not all_subs:
            event.set_result(MessageEventResult().message("📭 No subscriptions yet."))
            return

        lines = ["📋 **所有订阅（全局配置）**\n"]
        for sub in all_subs:
            if sub.id is None:
                continue
            subscribers = await self.db.get_subscribers(sub.id)
            lines.append(f"**{sub.id}. [{sub.name}]({sub.url})**")
            lines.append(
                f"   抓取间隔: `{sub.interval}` 分钟 | 订阅者: `{len(subscribers)}`"
            )
            lines.append("")

        lines.append("**用法**: `/rssupdate global <订阅ID> <参数> <值>`")
        event.set_result(MessageEventResult().message("\n".join(lines)))

    async def rssupdate_global(
        self,
        event: AstrMessageEvent,
        subscription_id: int,
        config_key: str,
        config_value: str,
    ) -> None:
        """Update subscription global configuration (admin only)."""
        if not self._is_admin(event):
            event.set_result(MessageEventResult().message("❌ This command requires admin privileges"))
            return

        # Validate config key
        if config_key not in GLOBAL_CONFIGURABLE_FIELDS:
            lines = [
                f"❌ 无效参数: `{config_key}`\n",
                "**可用参数列表**:",
                "| 参数 | 说明 |",
                "|------|------|",
            ]
            for key in GLOBAL_CONFIGURABLE_FIELDS:
                desc = GLOBAL_CONFIG_DESCRIPTIONS.get(key, "")
                lines.append(f"| `{key}` | {desc} |")
            lines.append("\n**用法**: `/rssupdate global <订阅ID> <参数> <值>`")
            event.set_result(MessageEventResult().message("\n".join(lines)))
            return

        # Get subscription
        subscription = await self.db.get_subscription(subscription_id)
        if not subscription:
            event.set_result(MessageEventResult().message(f"❌ Subscription ID {subscription_id} not found"))
            return

        # Parse value based on field type
        try:
            value: bool | int | str
            if config_key in GLOBAL_CONFIG_BOOL_FIELDS:
                value = config_value.lower() in ("true", "1", "yes", "on")
            elif config_key in GLOBAL_CONFIG_INT_FIELDS:
                value = int(config_value)
            else:
                value = config_value

            # Special handling for source_group_id - validate group exists
            if config_key == "source_group_id" and isinstance(value, int):
                group = await self.db.get_group(value)
                if not group:
                    event.set_result(MessageEventResult().message(f"❌ Group ID {value} not found"))
                    return

            # Update subscription
            setattr(subscription, config_key, value)
            await self.db.update_subscription(subscription)

            # Refresh scheduler
            await self.scheduler.schedule_subscription_fetch(subscription)

            event.set_result(MessageEventResult().message(f"✅ Updated {config_key} = {value} for '{subscription.name}'"))
        except ValueError:
            event.set_result(MessageEventResult().message(f"❌ Invalid value for {config_key}: {config_value}"))

    async def rssupdate_list_sub(
        self, event: AstrMessageEvent, subscription_id: int
    ) -> None:
        """List all subscribers for a subscription (admin only)."""
        if not self._is_admin(event):
            event.set_result(MessageEventResult().message("❌ This command requires admin privileges"))
            return

        # Get subscription
        subscription = await self.db.get_subscription(subscription_id)
        if not subscription:
            event.set_result(MessageEventResult().message(f"❌ Subscription ID {subscription_id} not found"))
            return

        # Get subscribers
        subscribers = await self.db.get_subscribers(subscription_id)

        lines = [
            f"📋 **Subscribers for '{subscription.name}' (ID: {subscription_id})**\n"
        ]

        if not subscribers:
            lines.append("📭 No subscribers")
        else:
            for sub in subscribers:
                # Parse UMO to get adapter and session info
                if sub.is_webhook():
                    adapter_emoji = "🔗"
                    adapter_name = "webhook"
                    session_id = sub.umo
                else:
                    parts = sub.umo.split(":")
                    adapter_name = parts[0] if parts else "unknown"
                    session_id = parts[-1] if parts else sub.umo

                    if adapter_name == TELEGRAM_ADAPTER:
                        adapter_emoji = "📱"
                    elif adapter_name == WECOM_ADAPTER:
                        adapter_emoji = "💼"
                    else:
                        adapter_emoji = "❓"

                status = "⏸️ Paused" if sub.personal_config.get("stop") else "✅ Active"
                lines.append(
                    f"  {adapter_emoji} `{session_id}` ({adapter_name}) - {status}"
                )

        event.set_result(MessageEventResult().message("\n".join(lines)))

    async def rsstrigger(
        self, event: AstrMessageEvent, name_or_id: str | None = None
    ) -> None:
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

            if not subscription or subscription.id is None:
                event.set_result(MessageEventResult().message(f"❌ Subscription not found: {name_or_id}"))
                return

            # Trigger fetch
            await self.scheduler._fetch_subscription_handler(subscription.id)
            event.set_result(MessageEventResult().message(f"✅ Triggered fetch for: {subscription.name}"))
        else:
            # Trigger all subscriptions
            all_subs = await self.db.get_all_subscriptions()
            for sub in all_subs:
                if sub.id is not None:
                    await self.scheduler._fetch_subscription_handler(sub.id)
            event.set_result(MessageEventResult().message(f"✅ Triggered fetch for all {len(all_subs)} subscriptions"))


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
                system_prompt="You are an RSS article summary assistant. Please organize and summarize subscribed articles for users.",
            )

        event.set_result(MessageEventResult().message(f"✅ Group created: {name} (ID: {group_id})\nPersona: {persona_id}"))

    async def group_rename(
        self, event: AstrMessageEvent, group_id: int, new_name: str
    ) -> None:
        """Rename a group."""
        group = await self.db.get_group(group_id)
        if not group:
            event.set_result(MessageEventResult().message(f"❌ Group not found: {group_id}"))
            return

        old_name = group.name
        group.name = new_name
        await self.db.update_group(group)

        event.set_result(MessageEventResult().message(f"✅ Group renamed: {old_name} → {new_name}"))

    async def group_list(self, event: AstrMessageEvent) -> None:
        """List all groups."""
        groups = await self.db.get_all_groups()

        if not groups:
            event.set_result(MessageEventResult().message("📭 No groups created yet.\nUse /rssgroup add <name> to create one."))
            return

        lines = ["📂 RSS Groups:\n"]

        for group in groups:
            if group.id is None:
                continue
            subscriptions = await self.db.get_subscriptions_by_group(group.id)
            lines.append(f"**{group.name}** (ID: {group.id})")
            lines.append(f"   Schedules: {', '.join(group.schedules) or 'None'}")
            lines.append(f"   Subscriptions: {len(subscriptions)}")
            lines.append(f"   Persona: {group.persona_id or 'Default'}")
            lines.append("")

        event.set_result(MessageEventResult().message("\n".join(lines)))

    async def group_timeadd(
        self, event: AstrMessageEvent, group_id: int, time_str: str
    ) -> None:
        """Add a digest schedule to a group."""
        # Validate time format
        if not re.match(r"^\d{1,2}:\d{2}$", time_str):
            event.set_result(MessageEventResult().message("❌ Invalid time format. Use HH:MM (e.g., 09:00)"))
            return

        group = await self.db.get_group(group_id)
        if not group:
            event.set_result(MessageEventResult().message(f"❌ Group not found: {group_id}"))
            return

        if time_str in group.schedules:
            event.set_result(MessageEventResult().message(f"ℹ️ Schedule {time_str} already exists for group {group.name}"))
            return

        group.schedules.append(time_str)
        await self.db.update_group(group)

        # Schedule the digest job
        await self.scheduler.schedule_digest(group, time_str)

        event.set_result(MessageEventResult().message(f"✅ Added schedule {time_str} to group {group.name}"))

    async def group_timedel(
        self, event: AstrMessageEvent, group_id: int, time_str: str
    ) -> None:
        """Remove a digest schedule from a group."""
        group = await self.db.get_group(group_id)
        if not group:
            event.set_result(MessageEventResult().message(f"❌ Group not found: {group_id}"))
            return

        if time_str not in group.schedules:
            event.set_result(MessageEventResult().message(f"❌ Schedule {time_str} not found in group {group.name}"))
            return

        group.schedules.remove(time_str)
        await self.db.update_group(group)

        # Remove the digest job
        if group.id is not None:
            await self.scheduler.remove_digest_job(group.id, time_str)

        event.set_result(MessageEventResult().message(f"✅ Removed schedule {time_str} from group {group.name}"))

    async def group_subadd(
        self,
        event: AstrMessageEvent,
        group_id: int,
        target_id: str,
        adapter: str = TELEGRAM_ADAPTER,
        is_group: bool = False,
    ) -> None:
        """Add a subscriber to a group.

        Args:
            target_id: User/group ID or webhook URL
            adapter: Adapter type (telegram, wecom, webhook)
            is_group: Whether target is a group (ignored for webhooks)
        """
        # Validate adapter
        if adapter not in (TELEGRAM_ADAPTER, WECOM_ADAPTER, WEBHOOK_ADAPTER):
            event.set_result(MessageEventResult().message(f"❌ Unsupported adapter: {adapter}. Use telegram, wecom, or webhook"))
            return

        group = await self.db.get_group(group_id)
        if not group:
            event.set_result(MessageEventResult().message(f"❌ Group not found: {group_id}"))
            return

        # Get all subscriptions in the group and add subscriber to each
        subscriptions = await self.db.get_subscriptions_by_group(group_id)
        if not subscriptions:
            event.set_result(MessageEventResult().message(f"❌ No subscriptions in group {group.name}"))
            return

        # Build UMO
        # If target_id starts with http, it's a webhook URL
        if target_id.startswith("http://") or target_id.startswith("https://"):
            umo = target_id
        else:
            message_type = "GroupMessage" if is_group else "PrivateMessage"
            umo = f"{adapter}:{message_type}:{target_id}"

        added_count = 0
        skipped_count = 0

        for sub in subscriptions:
            if sub.id is None:
                continue

            # Check if already subscribed
            existing = await self.db.get_subscriber(sub.id, umo)
            if existing:
                skipped_count += 1
                continue

            subscriber = Subscriber(subscription_id=sub.id, umo=umo)
            await self.db.add_subscriber(subscriber)
            added_count += 1

        result_msg = f"✅ Added subscriber to group **{group.name}**\n"
        result_msg += f"Added: {added_count} subscriptions\n"
        if skipped_count > 0:
            result_msg += f"Skipped (already subscribed): {skipped_count}"
        result_msg += f"\nSession: {umo}"

        event.set_result(MessageEventResult().message(result_msg))

    async def group_subdel(
        self,
        event: AstrMessageEvent,
        group_id: int,
        target_id: str,
    ) -> None:
        """Remove a subscriber from a group.

        Args:
            target_id: User/group ID or webhook URL
        """
        group = await self.db.get_group(group_id)
        if not group:
            event.set_result(MessageEventResult().message(f"❌ Group not found: {group_id}"))
            return

        # Remove subscriber from all subscriptions in the group
        subscriptions = await self.db.get_subscriptions_by_group(group_id)

        removed_count = 0
        not_found_count = 0

        for sub in subscriptions:
            if sub.id is None:
                continue

            # Try to find subscriber by UMO
            subscribers = await self.db.get_subscribers(sub.id)
            found_umo = None

            # Check if target_id is a webhook URL
            if target_id.startswith("http://") or target_id.startswith("https://"):
                for s in subscribers:
                    if s.umo == target_id:
                        found_umo = s.umo
                        break
            else:
                # Search for subscriber with matching ID in UMO
                for s in subscribers:
                    parts = s.umo.split(":")
                    if len(parts) >= 3 and parts[-1] == target_id:
                        found_umo = s.umo
                        break

            if found_umo:
                await self.db.delete_subscriber(sub.id, found_umo)
                removed_count += 1
            else:
                not_found_count += 1

        result_msg = f"✅ Removed subscriber from group **{group.name}**\n"
        result_msg += f"Removed: {removed_count} subscriptions\n"
        if not_found_count > 0:
            result_msg += f"Not found: {not_found_count}"

        event.set_result(MessageEventResult().message(result_msg))
