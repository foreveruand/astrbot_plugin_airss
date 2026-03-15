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
        self.rss_commands = RSSCommands(self.context, self.db, self.scheduler, self.fetcher)
        self.group_commands = GroupCommands(self.context, self.db, self.scheduler)

        self._initialized = True
        logger.info("RSS plugin initialized successfully")

    async def terminate(self) -> None:
        """Called when the plugin is disabled or reloaded."""
        logger.info("RSS plugin terminated")

    def _parse_args(self, message: str) -> list[str]:
        """Parse message into arguments."""
        parts = message.strip().split(maxsplit=2)
        return parts if parts else []

    @filter.command("rssadd")
    async def rssadd(self, event: AstrMessageEvent) -> None:
        """Add an RSS subscription. Usage: rssadd <url> [name]"""
        await self.initialize()

        message = event.message_str.strip()
        parts = self._parse_args(message.replace("/rssadd", "").strip())

        if not parts:
            await event.send_event_result(
                event.make_result().message("Usage: /rssadd <url> [name]")
            )
            return

        url = parts[0]
        name = parts[1] if len(parts) > 1 else None

        await self.rss_commands.rssadd(event, url, name)

    @filter.command("rssdel")
    async def rssdel(self, event: AstrMessageEvent) -> None:
        """Delete an RSS subscription. Usage: rssdel <name|id>"""
        await self.initialize()

        message = event.message_str.strip()
        name_or_id = message.replace("/rssdel", "").strip()

        if not name_or_id:
            await event.send_event_result(
                event.make_result().message("Usage: /rssdel <name|id>")
            )
            return

        await self.rss_commands.rssdel(event, name_or_id)

    @filter.command("rsslist")
    async def rsslist(self, event: AstrMessageEvent) -> None:
        """List all RSS subscriptions."""
        await self.initialize()
        await self.rss_commands.rsslist(event)

    @filter.command("rssupdate")
    async def rssupdate(self, event: AstrMessageEvent) -> None:
        """Update subscription configuration. Usage: rssupdate <name|id> [config] [value]"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("/rssupdate", "").strip().split(maxsplit=2)

        if not parts:
            await event.send_event_result(
                event.make_result().message("Usage: /rssupdate <name|id> [config] [value]")
            )
            return

        name_or_id = parts[0]
        config_key = parts[1] if len(parts) > 1 else None
        config_value = parts[2] if len(parts) > 2 else None

        await self.rss_commands.rssupdate(event, name_or_id, config_key, config_value)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("rsstrigger")
    async def rsstrigger(self, event: AstrMessageEvent) -> None:
        """Manually trigger RSS update (admin only). Usage: rsstrigger [name|id]"""
        await self.initialize()

        message = event.message_str.strip()
        name_or_id = message.replace("/rsstrigger", "").strip() or None

        await self.rss_commands.rsstrigger(event, name_or_id)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command_group("rssgroup")
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
                event.make_result().message("Usage: /rssgroup timeadd <group_id> <HH:MM>")
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
                event.make_result().message("Usage: /rssgroup timedel <group_id> <HH:MM>")
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
        """Add a subscriber to a group. Usage: rssgroup subadd <group_id> <session_id>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("/rssgroup subadd", "").strip().split(maxsplit=1)

        if len(parts) < 2:
            await event.send_event_result(
                event.make_result().message("Usage: /rssgroup subadd <group_id> <session_id>")
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            await event.send_event_result(
                event.make_result().message("Group ID must be a number")
            )
            return

        await self.group_commands.group_subadd(event, group_id, parts[1])

    @rssgroup.command("subdel")
    async def rssgroup_subdel(self, event: AstrMessageEvent) -> None:
        """Remove a subscriber from a group. Usage: rssgroup subdel <group_id> <session_id>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("/rssgroup subdel", "").strip().split(maxsplit=1)

        if len(parts) < 2:
            await event.send_event_result(
                event.make_result().message("Usage: /rssgroup subdel <group_id> <session_id>")
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