"""
AstrBot RSS Plugin - RSS subscription with AI digest support.

This plugin provides RSS subscription management, automatic fetching,
AI-powered digest generation, and multi-platform message delivery.
"""

import logging
import uuid
from pathlib import Path

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.core.platform.sources.telegram.tg_event import TelegramCallbackQueryEvent
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .commands import GroupCommands, RSSCommands
from .database import Database
from .fetcher import RSSFetcher
from .models import TELEGRAM_ADAPTER
from .scheduler import RSSScheduler

logger = logging.getLogger("astrbot")

# Store keyboard session data for callback handling
KEYBOARD_SESSIONS: dict[str, dict] = {}


class Main(star.Star):
    """Main class for the RSS plugin."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        self.context = context
        self.config = config
        self._initialized = False

    async def initialize(self) -> None:
        """Called when the plugin is activated."""
        if self._initialized:
            return

        await self._init()

        self._initialized = True
        logger.info("RSS plugin initialized successfully")

    async def _init(self) -> None:
        data_path = (
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_airss"
        )
        data_path.mkdir(parents=True, exist_ok=True)
        db_path = data_path / "rss.db"

        self.db = Database(db_path)
        await self.db.init_db()

        proxy_config = self.config.get("proxy_config", {})
        self.fetcher = RSSFetcher(
            proxy=proxy_config.get("proxy")
            if proxy_config.get("enable_proxy")
            else None,
            timeout=30,
            rsshub_url=self.config.get("rsshub_config", {}).get("rsshub_url"),
            rsshub_key=self.config.get("rsshub_config", {}).get("rsshub_key"),
        )

        self.scheduler = RSSScheduler(self.context, self.db, self.fetcher, self.config)
        await self.scheduler.start()

        self.rss_commands = RSSCommands(
            self.context, self.db, self.scheduler, self.fetcher
        )
        self.group_commands = GroupCommands(self.context, self.db, self.scheduler)

        await self._init_cleanup_job()

    async def _init_cleanup_job(self) -> None:
        retention_days = self.config.get("storage_config", {}).get(
            "article_retention_days", 30
        )

        async def _cleanup_handler() -> None:
            deleted = await self.db.cleanup_old_articles(retention_days)
            logger.info(f"RSS article cleanup: deleted {deleted} old articles")

        job_name = "RSS Article Cleanup"

        # Delete existing cleanup job first
        jobs = await self.context.cron_manager.list_jobs(job_type="basic")
        for job in jobs:
            if job.name == job_name:
                await self.context.cron_manager.delete_job(job.job_id)
                logger.info(f"Deleted existing cleanup job: {job.job_id}")
                break

        await self.context.cron_manager.add_basic_job(
            name=job_name,
            cron_expression="0 3 * * *",
            handler=_cleanup_handler,
            description="RSS插件: 清理过期文章",
            persistent=False,
            enabled=True,
        )

    async def terminate(self) -> None:
        """Called when the plugin is disabled or reloaded."""
        await self.db.close()
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
            /rssadd <subscription_id> <umo> - Add subscriber (admin)
        """
        await self.initialize()

        message = event.message_str.strip()
        args_text = message.replace("rssadd", "").strip()
        parts = self._parse_args(args_text)
        if not parts:
            event.set_result(
                event.make_result().message(
                    "Usage:\n"
                    "  /rssadd <url> [name] - Add RSS subscription\n"
                    "  /rssadd -g <group_id> - Subscribe to entire group\n"
                    "  /rssadd -p <rsshub_path> - Print RSSHub URL only\n"
                    "  /rssadd <sub_id> <umo> - Add subscriber (admin)"
                )
            )
            return

        # Handle -g flag: subscribe to group
        if parts[0] == "-g":
            if len(parts) < 2:
                event.set_result(
                    event.make_result().message("Usage: /rssadd -g <group_id>")
                )
                return
            try:
                group_id = int(parts[1])
                await self.rss_commands.rssadd_group(event, group_id)
            except ValueError:
                event.set_result(
                    event.make_result().message("❌ Group ID must be a number")
                )
            return

        # Handle -p flag: print RSSHub URL only
        if parts[0] == "-p":
            if len(parts) < 2:
                event.set_result(
                    event.make_result().message("Usage: /rssadd -p <rsshub_path>")
                )
                return
            rsshub_path = parts[1]
            if self.fetcher.rsshub_url:
                url = self.fetcher.build_rsshub_url(rsshub_path)
                event.set_result(event.make_result().message(f"RSSHub URL: {url}"))
            else:
                event.set_result(
                    event.make_result().message("❌ RSSHub URL not configured")
                )
            return

        # Check if first arg is a subscription ID (admin: add subscriber)
        try:
            subscription_id = int(parts[0])
            # Admin mode: add subscriber
            if len(parts) < 2:
                event.set_result(
                    event.make_result().message(
                        "Usage: /rssadd <subscription_id> <umo>\n"
                        "  umo: e.g., telegram:FriendMessage:xxxxx"
                    )
                )
                return
            # Join remaining parts to handle UMO with colons
            umo = " ".join(parts[1:])
            await self.rss_commands.rssadd_subscriber(event, subscription_id, umo)
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
        args_text = message.replace("rssdel", "").strip()
        parts = self._parse_args(args_text)

        # On Telegram, show keyboard when no args provided
        if not parts and event.get_platform_name() == "telegram":
            await self._show_rssdel_keyboard(event)
            return

        if not parts:
            event.set_result(event.make_result().message("Usage: /rssdel <name|id>"))
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
        args_text = message.replace("rssupdate", "").strip()
        parts = self._parse_args(args_text)

        # On Telegram, show keyboard when no args provided
        if not parts and event.get_platform_name() == "telegram":
            await self._show_rssupdate_keyboard(event)
            return

        if not parts:
            event.set_result(
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
                # List all subscriptions - use keyboard on Telegram
                if event.get_platform_name() == "telegram":
                    await self._show_global_list_keyboard(event)
                else:
                    await self.rss_commands.rssupdate_global_list(event)
                return

            if len(parts) < 2:
                event.set_result(
                    event.make_result().message(
                        "Usage: /rssupdate global <subscription_id> <config> <value>"
                    )
                )
                return

            try:
                subscription_id = int(parts[1])
                config_key = parts[2] if len(parts) > 2 else None
                config_value = " ".join(
                    parts[3:] if len(parts) > 3 else []
                )  # Join remaining parts for values with spaces

                # On Telegram with only sub_id, show config keyboard
                if not config_key and event.get_platform_name() == "telegram":
                    await self._show_global_config_keyboard(event, subscription_id)
                    return

                await self.rss_commands.rssupdate_global(
                    event, subscription_id, config_key, config_value
                )
            except ValueError:
                event.set_result(
                    event.make_result().message("❌ Subscription ID must be a number")
                )
            return

        # Handle 'list_sub' subcommand (admin)
        if parts[0] == "list_sub":
            if len(parts) < 2:
                event.set_result(
                    event.make_result().message(
                        "Usage: /rssupdate list_sub <subscription_id>"
                    )
                )
                return
            try:
                subscription_id = int(parts[1])
                await self.rss_commands.rssupdate_list_sub(event, subscription_id)
            except ValueError:
                event.set_result(
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
        name_or_id = message.replace("rsstrigger", "").strip() or None

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
        name = message.replace("rssgroup add", "").strip()

        if not name:
            event.set_result(event.make_result().message("Usage: /rssgroup add <name>"))
            return

        await self.group_commands.group_add(event, name)

    @rssgroup.command("rename")
    async def rssgroup_rename(self, event: AstrMessageEvent) -> None:
        """Rename a group. Usage: rssgroup rename <id> <new_name>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rssgroup rename", "").strip().split(maxsplit=1)

        if len(parts) < 2:
            event.set_result(
                event.make_result().message("Usage: /rssgroup rename <id> <new_name>")
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            event.set_result(event.make_result().message("Group ID must be a number"))
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
        parts = message.replace("rssgroup timeadd", "").strip().split()

        if len(parts) < 2:
            event.set_result(
                event.make_result().message(
                    "Usage: /rssgroup timeadd <group_id> <HH:MM>"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            event.set_result(event.make_result().message("Group ID must be a number"))
            return

        await self.group_commands.group_timeadd(event, group_id, parts[1])

    @rssgroup.command("timedel")
    async def rssgroup_timedel(self, event: AstrMessageEvent) -> None:
        """Remove a digest schedule. Usage: rssgroup timedel <group_id> <HH:MM>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rssgroup timedel", "").strip().split()

        if len(parts) < 2:
            event.set_result(
                event.make_result().message(
                    "Usage: /rssgroup timedel <group_id> <HH:MM>"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            event.set_result(event.make_result().message("Group ID must be a number"))
            return

        await self.group_commands.group_timedel(event, group_id, parts[1])

    @rssgroup.command("subadd")
    async def rssgroup_subadd(self, event: AstrMessageEvent) -> None:
        """Add a subscriber to a group.

        Usage: rssgroup subadd <group_id> <umo>
        - umo: Unified Message Origin, e.g., telegram:FriendMessage:xxxxx
        """
        await self.initialize()

        message = event.message_str.strip()
        args_text = message.replace("rssgroup subadd", "").strip()
        parts = args_text.split(None, 1)  # Split into max 2 parts

        if len(parts) < 2:
            event.set_result(
                event.make_result().message(
                    "Usage: /rssgroup subadd <group_id> <umo>\n"
                    "  umo: e.g., telegram:FriendMessage:xxxxx"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            event.set_result(event.make_result().message("Group ID must be a number"))
            return

        umo = parts[1].strip()

        await self.group_commands.group_subadd(event, group_id, umo)

    @rssgroup.command("subdel")
    async def rssgroup_subdel(self, event: AstrMessageEvent) -> None:
        """Remove a subscriber from a group. Usage: rssgroup subdel <group_id> <target_id>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rssgroup subdel", "").strip().split(maxsplit=1)

        if len(parts) < 2:
            event.set_result(
                event.make_result().message(
                    "Usage: /rssgroup subdel <group_id> <target_id>"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            event.set_result(event.make_result().message("Group ID must be a number"))
            return

        await self.group_commands.group_subdel(event, group_id, parts[1])

    async def _show_rssdel_keyboard(self, event: AstrMessageEvent) -> None:
        """Show inline keyboard with subscriptions for deletion (Telegram only)."""
        await self.initialize()
        umo = event.unified_msg_origin
        all_subs = await self.db.get_all_subscriptions()

        if not all_subs:
            event.set_result(
                MessageEventResult().message(
                    "📭 No subscriptions yet.\nUse /rssadd <url> to add one."
                )
            )
            return

        # Filter to show only user's subscribed feeds
        user_subs = []
        for sub in all_subs:
            if sub.id is None:
                continue
            subscribers = await self.db.get_subscribers(sub.id)
            if any(s.umo == umo for s in subscribers):
                user_subs.append(sub)

        if not user_subs:
            event.set_result(
                MessageEventResult().message("📭 You have no subscriptions to delete.")
            )
            return

        # Create session for keyboard
        session_id = uuid.uuid4().hex[:8]
        KEYBOARD_SESSIONS[session_id] = {
            "umo": umo,
            "type": "rssdel",
        }

        # Build keyboard buttons
        buttons = []
        for sub in user_subs:
            buttons.append(
                [
                    {
                        "text": f"📰 {sub.name} (ID: {sub.id})",
                        "callback_data": f"rssdel:{session_id}:{sub.id}",
                    }
                ]
            )

        result = MessageEventResult()
        result.message("🗑️ Select a subscription to unsubscribe:")
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _show_rssupdate_keyboard(self, event: AstrMessageEvent) -> None:
        """Show inline keyboard with subscriptions for config (Telegram only)."""
        await self.initialize()
        umo = event.unified_msg_origin
        all_subs = await self.db.get_all_subscriptions()

        if not all_subs:
            event.set_result(
                MessageEventResult().message(
                    "📭 No subscriptions yet.\nUse /rssadd <url> to add one."
                )
            )
            return

        # Filter to show only user's subscribed feeds
        user_subs = []
        for sub in all_subs:
            if sub.id is None:
                continue
            subscribers = await self.db.get_subscribers(sub.id)
            if any(s.umo == umo for s in subscribers):
                user_subs.append(sub)

        if not user_subs:
            event.set_result(
                MessageEventResult().message(
                    "📭 You have no subscriptions to configure."
                )
            )
            return

        # Create session for keyboard
        session_id = uuid.uuid4().hex[:8]
        user_id = event.get_sender_id()
        KEYBOARD_SESSIONS[session_id] = {
            "umo": umo,
            "type": "rssconfig",
            "user_id": user_id,
        }

        # Build keyboard buttons
        buttons = []
        for sub in user_subs:
            buttons.append(
                [
                    {
                        "text": f"📰 {sub.name} (ID: {sub.id})",
                        "callback_data": f"rssconfig:{session_id}:{sub.id}:{user_id}",
                    }
                ]
            )

        result = MessageEventResult()
        result.message("⚙️ Select a subscription to configure:")
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _show_config_keyboard(
        self,
        event: TelegramCallbackQueryEvent,
        session_id: str,
        sub_id: int,
        user_id: str,
    ) -> None:
        """Show config toggle keyboard for a subscription."""
        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            await event.answer_callback_query(text="❌ Session expired")
            return

        subscription = await self.db.get_subscription(sub_id)
        if not subscription:
            await event.answer_callback_query(text="❌ Subscription not found")
            return

        # Get subscriber
        subscriber = await self.db.get_subscriber(sub_id, session["umo"])
        if not subscriber:
            await event.answer_callback_query(text="❌ You are not subscribed")
            return

        # Build config buttons with toggle status
        config_layout = [
            [
                ("only_title", "仅标题", "📄"),
                ("only_pic", "仅图片", "🖼️"),
                ("only_has_pic", "有图片才发", "📸"),
            ],
            [("enable_spoiler", "图片遮挡", "👁️"), ("stop", "暂停订阅", "⏸️")],
        ]

        buttons = []
        config = subscriber.personal_config or {}
        for row in config_layout:
            row_buttons = []
            for key, text, emoji in row:
                current_value = config.get(key, False)
                status = "✅" if current_value else "⭕"
                button_text = f"{emoji} {text} {status}"
                row_buttons.append(
                    {
                        "text": button_text,
                        "callback_data": f"toggle:{session_id}:{sub_id}:{user_id}:{key}",
                    }
                )
            buttons.append(row_buttons)

        # Add back button
        buttons.append(
            [
                {
                    "text": "⬅️ 返回列表",
                    "callback_data": f"rsslist:{session_id}:{user_id}",
                }
            ]
        )

        result = MessageEventResult()
        result.message(
            f"⚙️ **{subscription.name}** - [{subscription.url}]({subscription.url}) 配置\n\n点击按钮切换开关状态:"
        )
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _show_global_list_keyboard(
        self, event: AstrMessageEvent | TelegramCallbackQueryEvent
    ) -> None:
        """Show inline keyboard with all subscriptions for global config (admin, Telegram only)."""
        await self.initialize()

        # Check admin permission
        if event.role not in ("admin", "superuser"):
            result = MessageEventResult().message(
                "❌ This command requires admin privileges"
            )
            event.set_result(result)
            return

        all_subs = await self.db.get_all_subscriptions()

        if not all_subs:
            result = MessageEventResult().message("📭 No subscriptions yet.")
            event.set_result(result)
            return

        # Create session for keyboard
        session_id = uuid.uuid4().hex[:8]
        KEYBOARD_SESSIONS[session_id] = {
            "type": "globalconfig",
        }

        # Build keyboard buttons
        buttons = []
        for sub in all_subs:
            if sub.id is None:
                continue
            subscribers = await self.db.get_subscribers(sub.id)
            buttons.append(
                [
                    {
                        "text": f"📰 {sub.name} (ID: {sub.id}) [{len(subscribers)} 订阅者]",
                        "callback_data": f"globalconfig:{session_id}:{sub.id}",
                    }
                ]
            )

        result = MessageEventResult()
        result.message("🔧 **全局配置** - 选择订阅进行配置:")
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _show_global_config_keyboard(
        self, event: AstrMessageEvent | TelegramCallbackQueryEvent, sub_id: int
    ) -> None:
        """Show global config toggle keyboard for a subscription (admin only)."""
        await self.initialize()

        subscription = await self.db.get_subscription(sub_id)
        if not subscription:
            msg = f"❌ Subscription ID {sub_id} not found"
            if isinstance(event, TelegramCallbackQueryEvent):
                await event.answer_callback_query(text=msg)
            else:
                event.set_result(MessageEventResult().message(msg))
            return

        # Global config toggle fields (from commands.py)
        global_config_layout = [
            [("ai_summary_enabled", "AI摘要", "🤖"), ("enable_proxy", "代理", "🌐")],
            [("stop", "暂停", "⏸️")],
        ]

        buttons = []
        for row in global_config_layout:
            row_buttons = []
            for key, text, emoji in row:
                current_value = getattr(subscription, key, False)
                status = "✅" if current_value else "⭕"
                button_text = f"{emoji} {text} {status}"
                row_buttons.append(
                    {
                        "text": button_text,
                        "callback_data": f"globaltoggle:{sub_id}:{key}",
                    }
                )
            buttons.append(row_buttons)

        # Add back button
        buttons.append(
            [
                {
                    "text": "⬅️ 返回列表",
                    "callback_data": "globallist:0:nop",
                }
            ]
        )

        result = MessageEventResult()
        result.message(
            f"🔧 **{subscription.name}** - [{subscription.url}]({subscription.url}) 全局配置\n\n"
            f"间隔: {subscription.interval} 分钟\n\n"
            f"点击按钮切换开关状态:"
        )
        result.inline_keyboard(buttons)

        if isinstance(event, TelegramCallbackQueryEvent):
            event.set_result(result)
        else:
            event.set_result(result)

    @filter.callback_query()
    async def handle_callback(self, event: TelegramCallbackQueryEvent) -> None:
        """Handle button click callbacks for RSS keyboard interactions."""
        try:
            data = event.data
            if not data:
                return

            parts = data.split(":")
            if len(parts) < 2:
                return

            action = parts[0]

            # Handle rssdel callback
            if action == "rssdel" and len(parts) >= 3:
                session_id = parts[1]
                sub_id = int(parts[2])

                session = KEYBOARD_SESSIONS.get(session_id)
                if not session:
                    await event.answer_callback_query(text="❌ Session expired")
                    return

                # Get subscription
                subscription = await self.db.get_subscription(sub_id)
                if not subscription:
                    await event.answer_callback_query(text="❌ Subscription not found")
                    return

                # Delete subscriber
                await self.db.delete_subscriber(sub_id, session["umo"])

                # Check if any subscribers left
                remaining = await self.db.get_subscribers(sub_id)
                if not remaining:
                    await self.scheduler.remove_subscription_job(sub_id)
                    await self.db.delete_subscription(sub_id)
                    await event.answer_callback_query(
                        text=f"✅ Deleted: {subscription.name}"
                    )
                else:
                    await event.answer_callback_query(
                        text=f"✅ Unsubscribed from: {subscription.name}"
                    )

                # Refresh keyboard
                await self._refresh_rssdel_keyboard(event, session_id)
                return

            # Handle rssconfig callback (show config keyboard)
            if action == "rssconfig" and len(parts) >= 4:
                session_id = parts[1]
                sub_id = int(parts[2])
                user_id = parts[3]
                await self._show_config_keyboard(event, session_id, sub_id, user_id)
                return

            # Handle toggle callback
            if action == "toggle" and len(parts) >= 5:
                session_id = parts[1]
                sub_id = int(parts[2])
                user_id = parts[3]
                config_key = parts[4]

                session = KEYBOARD_SESSIONS.get(session_id)
                if not session:
                    await event.answer_callback_query(text="❌ Session expired")
                    return

                # Get subscription and subscriber
                subscription = await self.db.get_subscription(sub_id)
                if not subscription:
                    await event.answer_callback_query(text="❌ Subscription not found")
                    return

                subscriber = await self.db.get_subscriber(sub_id, session["umo"])
                if not subscriber:
                    await event.answer_callback_query(text="❌ You are not subscribed")
                    return

                # Toggle the config value
                if subscriber.personal_config is None:
                    subscriber.personal_config = {}
                current_value = subscriber.personal_config.get(config_key, False)
                subscriber.personal_config[config_key] = not current_value
                await self.db.update_subscriber(subscriber)

                # Show toast
                new_value = subscriber.personal_config[config_key]
                status_text = "enabled" if new_value else "disabled"
                await event.answer_callback_query(text=f"✅ {config_key} {status_text}")

                # Refresh config keyboard
                await self._show_config_keyboard(event, session_id, sub_id, user_id)
                return

            # Handle rsslist callback (back to list)
            if action == "rsslist" and len(parts) >= 3:
                session_id = parts[1]
                await self._refresh_rssdel_keyboard(event, session_id)
                return

            # Handle globalconfig callback (show global config keyboard)
            if action == "globalconfig" and len(parts) >= 3:
                session_id = parts[1]
                sub_id = int(parts[2])
                await self._show_global_config_keyboard(event, sub_id)
                return

            # Handle globaltoggle callback (toggle global config)
            if action == "globaltoggle" and len(parts) >= 3:
                sub_id = int(parts[1])
                config_key = parts[2]

                subscription = await self.db.get_subscription(sub_id)
                if not subscription:
                    await event.answer_callback_query(text="❌ Subscription not found")
                    return

                # Toggle the config value
                current_value = getattr(subscription, config_key, False)
                setattr(subscription, config_key, not current_value)
                await self.db.update_subscription(subscription)

                # Refresh scheduler
                await self.scheduler.schedule_subscription_fetch(subscription)

                # Show toast
                new_value = getattr(subscription, config_key, False)
                status_text = "已开启" if new_value else "已关闭"
                await event.answer_callback_query(text=f"✅ {config_key} {status_text}")

                # Refresh config keyboard
                await self._show_global_config_keyboard(event, sub_id)
                return

            # Handle globallist callback (back to global list)
            if action == "globallist":
                await self._show_global_list_keyboard(event)
                return

        except Exception as e:
            logger.error(f"Error handling callback: {e}")
            await event.answer_callback_query(text="❌ An error occurred")

    async def _refresh_rssdel_keyboard(
        self, event: TelegramCallbackQueryEvent, session_id: str
    ) -> None:
        """Refresh the rssdel keyboard after deletion."""
        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            result = MessageEventResult()
            result.message("❌ Session expired")
            event.set_result(result)
            return

        umo = session["umo"]
        all_subs = await self.db.get_all_subscriptions()

        # Filter to show only user's subscribed feeds
        user_subs = []
        for sub in all_subs:
            if sub.id is None:
                continue
            subscribers = await self.db.get_subscribers(sub.id)
            if any(s.umo == umo for s in subscribers):
                user_subs.append(sub)

        if not user_subs:
            result = MessageEventResult()
            result.message("📭 You have no more subscriptions.")
            event.set_result(result)
            return

        # Build keyboard buttons
        buttons = []
        for sub in user_subs:
            buttons.append(
                [
                    {
                        "text": f"📰 {sub.name} (ID: {sub.id})",
                        "callback_data": f"rssdel:{session_id}:{sub.id}",
                    }
                ]
            )

        result = MessageEventResult()
        result.message("🗑️ Select a subscription to unsubscribe:")
        result.inline_keyboard(buttons)
        event.set_result(result)
