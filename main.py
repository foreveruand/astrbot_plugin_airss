"""
AstrBot RSS Plugin - RSS subscription with AI digest support.

This plugin provides RSS subscription management, automatic fetching,
AI-powered digest generation, and multi-platform message delivery.
"""

import logging
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .commands import GroupCommands, RSSCommands
from .database import Database
from .fetcher import RSSFetcher
from .models import TELEGRAM_ADAPTER
from .scheduler import RSSScheduler

logger = logging.getLogger("astrbot")


class Main(star.Star):
    """Main class for the RSS plugin."""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._initialized = False

    async def initialize(self) -> None:
        """Called when the plugin is activated."""
        if self._initialized:
            return

        # Setup database
        data_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_rss"
        data_path.mkdir(parents=True, exist_ok=True)
        db_path = data_path / "rss.db"

        self.db = Database(db_path)
        await self.db.init_db()

        # Setup fetcher
        config = self.context.get_config() or {}
        self.fetcher = RSSFetcher(
            proxy=config.get("proxy") if config.get("enable_proxy") else None,
            timeout=30,
            rsshub_url=config.get("rsshub_url"),
            rsshub_key=config.get("rsshub_key"),
        )

        # Setup scheduler
        self.scheduler = RSSScheduler(self.context, self.db, self.fetcher)
        await self.scheduler.start()

        # Setup commands
        self.rss_commands = RSSCommands(
            self.context, self.db, self.scheduler, self.fetcher
        )
        self.group_commands = GroupCommands(self.context, self.db, self.scheduler)

        self._initialized = True
        logger.info("RSS plugin initialized successfully")

    async def terminate(self) -> None:
        """Called when the plugin is disabled or reloaded."""
        logger.info("RSS plugin terminated")

    def _parse_args(self, message: str) -> list[str]:
        """Parse message into arguments."""
        parts = message.strip().split()
        return parts if parts else []

    @filter.command("rssadd")
    async def rssadd(self, event: AstrMessageEvent) -> None:
        """Add an RSS subscription.

        Usage:
            /rssadd <url> [name] - Add RSS subscription
            /rssadd -g <group_id> - Subscribe to entire group
            /rssadd -p <rsshub_path> - Print RSSHub URL only
            /rssadd <subscription_id> <user/group_id> [adapter] [is_group] - Add subscriber (admin)
        """
        await self.initialize()

        message = event.message_str.strip()
        args_text = message.replace("/rssadd", "").strip()
        parts = self._parse_args(args_text)

        if not parts:
            await event.send_event_result(
                event.make_result().message(
                    "Usage:\n"
                    "  /rssadd <url> [name] - Add RSS subscription\n"
                    "  /rssadd -g <group_id> - Subscribe to entire group\n"
                    "  /rssadd -p <rsshub_path> - Print RSSHub URL only\n"
                    "  /rssadd <sub_id> <user_id> [adapter] [is_group] - Add subscriber (admin)"
                )
            )
            return

        # Handle -g flag: subscribe to group
        if parts[0] == "-g":
            if len(parts) < 2:
                await event.send_event_result(
                    event.make_result().message("Usage: /rssadd -g <group_id>")
                )
                return
            try:
                group_id = int(parts[1])
                await self.rss_commands.rssadd_group(event, group_id)
            except ValueError:
                await event.send_event_result(
                    event.make_result().message("❌ Group ID must be a number")
                )
            return

        # Handle -p flag: print RSSHub URL only
        if parts[0] == "-p":
            if len(parts) < 2:
                await event.send_event_result(
                    event.make_result().message("Usage: /rssadd -p <rsshub_path>")
                )
                return
            rsshub_path = parts[1]
            if self.fetcher.rsshub_url:
                url = self.fetcher.build_rsshub_url(rsshub_path)
                await event.send_event_result(
                    event.make_result().message(f"RSSHub URL: {url}")
                )
            else:
                await event.send_event_result(
                    event.make_result().message("❌ RSSHub URL not configured")
                )
            return

        # Check if first arg is a subscription ID (admin: add subscriber)
        try:
            subscription_id = int(parts[0])
            # Admin mode: add subscriber
            if len(parts) < 2:
                await event.send_event_result(
                    event.make_result().message(
                        "Usage: /rssadd <subscription_id> <user/group_id> [adapter] [is_group]"
                    )
                )
                return
            target_id = parts[1]
            adapter = parts[2] if len(parts) > 2 else TELEGRAM_ADAPTER
            is_group = (
                parts[3].lower() in ("true", "1", "yes") if len(parts) > 3 else False
            )
            await self.rss_commands.rssadd_subscriber(
                event, subscription_id, target_id, adapter, is_group
            )
            return
        except ValueError:
            pass  # Not an ID, continue with normal flow

        # Normal flow: add subscription
        url = parts[0]
        name = parts[1] if len(parts) > 1 else None
        await self.rss_commands.rssadd(event, url, name)

    @filter.command("rssdel")
    async def rssdel(self, event: AstrMessageEvent) -> None:
        """Delete an RSS subscription.

        Usage:
            /rssdel <name|id> - Delete subscription (or unsubscribe)
            /rssdel <subscription_id> <user/group_id> - Remove subscriber (admin)
        """
        await self.initialize()

        message = event.message_str.strip()
        args_text = message.replace("/rssdel", "").strip()
        parts = self._parse_args(args_text)

        if not parts:
            await event.send_event_result(
                event.make_result().message("Usage: /rssdel <name|id>")
            )
            return

        # Check if first arg is a subscription ID and second arg exists (admin: remove subscriber)
        if len(parts) >= 2:
            try:
                subscription_id = int(parts[0])
                target_id = parts[1]
                await self.rss_commands.rssdel_subscriber(
                    event, subscription_id, target_id
                )
                return
            except ValueError:
                pass  # Not an ID, continue with normal flow

        # Normal flow: delete subscription
        name_or_id = parts[0]
        await self.rss_commands.rssdel(event, name_or_id)

    @filter.command("rsslist")
    async def rsslist(self, event: AstrMessageEvent) -> None:
        """List all RSS subscriptions."""
        await self.initialize()
        await self.rss_commands.rsslist(event)

    @filter.command("rssupdate")
    async def rssupdate(self, event: AstrMessageEvent) -> None:
        """Update subscription configuration.

        Usage:
            /rssupdate <name|id> - Show personalization config
            /rssupdate <name|id> <key> <value> - Update personalization
            /rssupdate global - List all subscriptions (admin)
            /rssupdate global <sub_id> <config> <value> - Update global config (admin)
            /rssupdate list_sub <sub_id> - List all subscribers (admin)
        """
        await self.initialize()

        message = event.message_str.strip()
        args_text = message.replace("/rssupdate", "").strip()
        parts = self._parse_args(args_text)

        if not parts:
            await event.send_event_result(
                event.make_result().message(
                    "Usage:\n"
                    "  /rssupdate <name|id> - Show personalization config\n"
                    "  /rssupdate <name|id> <key> <value> - Update personalization\n"
                    "  /rssupdate global - List all subscriptions (admin)\n"
                    "  /rssupdate global <sub_id> <config> <value> - Update global config (admin)\n"
                    "  /rssupdate list_sub <sub_id> - List all subscribers (admin)"
                )
            )
            return

        # Handle 'global' subcommand (admin)
        if parts[0] == "global":
            if len(parts) == 1:
                # List all subscriptions
                await self.rss_commands.rssupdate_global_list(event)
                return

            if len(parts) < 4:
                await event.send_event_result(
                    event.make_result().message(
                        "Usage: /rssupdate global <subscription_id> <config> <value>"
                    )
                )
                return

            try:
                subscription_id = int(parts[1])
                config_key = parts[2]
                config_value = " ".join(
                    parts[3:]
                )  # Join remaining parts for values with spaces
                await self.rss_commands.rssupdate_global(
                    event, subscription_id, config_key, config_value
                )
            except ValueError:
                await event.send_event_result(
                    event.make_result().message("❌ Subscription ID must be a number")
                )
            return

        # Handle 'list_sub' subcommand (admin)
        if parts[0] == "list_sub":
            if len(parts) < 2:
                await event.send_event_result(
                    event.make_result().message(
                        "Usage: /rssupdate list_sub <subscription_id>"
                    )
                )
                return
            try:
                subscription_id = int(parts[1])
                await self.rss_commands.rssupdate_list_sub(event, subscription_id)
            except ValueError:
                await event.send_event_result(
                    event.make_result().message("❌ Subscription ID must be a number")
                )
            return

        # Normal flow: update personal config
        name_or_id = parts[0]
        config_key = parts[1] if len(parts) > 1 else None
        config_value = parts[2] if len(parts) > 2 else None

        await self.rss_commands.rssupdate(event, name_or_id, config_key, config_value)

    @filter.command("rsstrigger")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def rsstrigger(self, event: AstrMessageEvent) -> None:
        """Manually trigger RSS update (admin only). Usage: rsstrigger [name|id]"""
        await self.initialize()

        message = event.message_str.strip()
        name_or_id = message.replace("/rsstrigger", "").strip() or None

        await self.rss_commands.rsstrigger(event, name_or_id)

    @filter.command_group("rssgroup")
    @filter.permission_type(filter.PermissionType.ADMIN)
    def rssgroup(self) -> None:
        """RSS group management commands (admin only)."""

    @rssgroup.command("add")
    async def rssgroup_add(self, event: AstrMessageEvent) -> None:
        """Create a new RSS group. Usage: rssgroup add <name>"""
        await self.initialize()

        message = event.message_str.strip()
        name = message.replace("/rssgroup add", "").strip()

        if not name:
            await event.send_event_result(
                event.make_result().message("Usage: /rssgroup add <name>")
            )
            return

        await self.group_commands.group_add(event, name)

    @rssgroup.command("rename")
    async def rssgroup_rename(self, event: AstrMessageEvent) -> None:
        """Rename a group. Usage: rssgroup rename <id> <new_name>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("/rssgroup rename", "").strip().split(maxsplit=1)

        if len(parts) < 2:
            await event.send_event_result(
                event.make_result().message("Usage: /rssgroup rename <id> <new_name>")
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            await event.send_event_result(
                event.make_result().message("Group ID must be a number")
            )
            return

        await self.group_commands.group_rename(event, group_id, parts[1])

    @rssgroup.command("list")
    async def rssgroup_list(self, event: AstrMessageEvent) -> None:
        """List all RSS groups."""
        await self.initialize()
        await self.group_commands.group_list(event)

    @rssgroup.command("timeadd")
    async def rssgroup_timeadd(self, event: AstrMessageEvent) -> None:
        """Add a digest schedule. Usage: rssgroup timeadd <group_id> <HH:MM>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("/rssgroup timeadd", "").strip().split()

        if len(parts) < 2:
            await event.send_event_result(
                event.make_result().message(
                    "Usage: /rssgroup timeadd <group_id> <HH:MM>"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            await event.send_event_result(
                event.make_result().message("Group ID must be a number")
            )
            return

        await self.group_commands.group_timeadd(event, group_id, parts[1])

    @rssgroup.command("timedel")
    async def rssgroup_timedel(self, event: AstrMessageEvent) -> None:
        """Remove a digest schedule. Usage: rssgroup timedel <group_id> <HH:MM>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("/rssgroup timedel", "").strip().split()

        if len(parts) < 2:
            await event.send_event_result(
                event.make_result().message(
                    "Usage: /rssgroup timedel <group_id> <HH:MM>"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            await event.send_event_result(
                event.make_result().message("Group ID must be a number")
            )
            return

        await self.group_commands.group_timedel(event, group_id, parts[1])

    @rssgroup.command("subadd")
    async def rssgroup_subadd(self, event: AstrMessageEvent) -> None:
        """Add a subscriber to a group.

        Usage: rssgroup subadd <group_id> <target_id> [adapter] [is_group]
        - target_id: User/group ID or webhook URL
        - adapter: telegram (default), wecom, or webhook
        - is_group: true/false (default false, ignored for webhooks)
        """
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("/rssgroup subadd", "").strip().split()

        if len(parts) < 2:
            await event.send_event_result(
                event.make_result().message(
                    "Usage: /rssgroup subadd <group_id> <target_id> [adapter] [is_group]\n"
                    "  adapter: telegram (default), wecom, or webhook\n"
                    "  is_group: true/false (default false, ignored for webhooks)"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            await event.send_event_result(
                event.make_result().message("Group ID must be a number")
            )
            return

        target_id = parts[1]
        adapter = parts[2] if len(parts) > 2 else TELEGRAM_ADAPTER
        is_group = parts[3].lower() in ("true", "1", "yes") if len(parts) > 3 else False

        await self.group_commands.group_subadd(
            event, group_id, target_id, adapter, is_group
        )

    @rssgroup.command("subdel")
    async def rssgroup_subdel(self, event: AstrMessageEvent) -> None:
        """Remove a subscriber from a group. Usage: rssgroup subdel <group_id> <target_id>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("/rssgroup subdel", "").strip().split(maxsplit=1)

        if len(parts) < 2:
            await event.send_event_result(
                event.make_result().message(
                    "Usage: /rssgroup subdel <group_id> <target_id>"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            await event.send_event_result(
                event.make_result().message("Group ID must be a number")
            )
            return

        await self.group_commands.group_subdel(event, group_id, parts[1])
