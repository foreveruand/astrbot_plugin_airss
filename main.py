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

from .commands import GroupCommands, RSSCommands, RSSSubCommands, RSSUtilCommands
from .database import Database
from .fetcher import RSSFetcher
from .models import RSSSubscription
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
            Path(get_astrbot_data_path()) / "plugin_data" / self.name
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
        self.sub_commands = RSSSubCommands(self.context, self.db, self.scheduler)
        self.util_commands = RSSUtilCommands(
            self.context,
            self.db,
            self.scheduler,
            self.config.get("rsshub_config", {}),
        )

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
        """
        await self.initialize()

        message = event.message_str.strip()
        args_text = message.replace("rssadd", "").strip()
        parts = self._parse_args(args_text)
        if not parts:
            event.set_result(
                event.make_result().message(
                    "Usage: /rssadd <url> [name] - Add RSS subscription"
                )
            )
            return

        # Parse URL and optional name
        url = parts[0]
        name = parts[1] if len(parts) > 1 else None
        await self.rss_commands.rssadd(event, url, name)

    @filter.command("rssdel")
    async def rssdel(self, event: AstrMessageEvent) -> None:
        """Delete an RSS subscription.

        Usage:
            /rssdel <name|id> - Delete subscription (or unsubscribe)
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

        if not parts and event.get_platform_name() == "telegram":
            await self._show_rssupdate_keyboard(event)
            return

        if not parts:
            await self._show_rssupdate_selection(event)
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

        # Mode B: Show config options when only subscription identifier provided
        if len(parts) == 1:
            name_or_id = parts[0]

            # On Telegram, show config keyboard
            if event.get_platform_name() == "telegram":
                await self._show_config_keyboard_mode_b(event, name_or_id)
                return

            # On other platforms, show config table
            await self._show_config_table_mode_b(event, name_or_id)
            return

        name_or_id = parts[0]
        config_key_or_value = parts[1] if len(parts) > 1 else None
        config_value = parts[2] if len(parts) > 2 else None

        if len(parts) == 2 and config_key_or_value:
            from .models import CONFIG_NAME_MAP, CONFIG_NUMBER_MAP

            is_config_number = config_key_or_value in CONFIG_NUMBER_MAP
            is_config_name = config_key_or_value in CONFIG_NAME_MAP

            if is_config_number or is_config_name:
                config_key = CONFIG_NUMBER_MAP.get(
                    config_key_or_value, config_key_or_value
                )
                await self._handle_mode_c(event, name_or_id, config_key)
                return

        config_key = config_key_or_value

        # Mode D: Full parameters - direct config modification
        if len(parts) >= 3 and config_key and config_value:
            await self._handle_mode_d(event, name_or_id, config_key, config_value)
            return

        await self.rss_commands.rssupdate(event, name_or_id, config_key, config_value)

    async def _handle_mode_d(
        self,
        event: AstrMessageEvent,
        name_or_id: str,
        config_key: str,
        config_value: str,
    ) -> None:
        """Handle Mode D: Direct config modification with full parameters.

        Args:
            event: The message event.
            name_or_id: Subscription identifier (ID or name).
            config_key: Configuration key (name or number).
            config_value: New value for the configuration.
        """
        from .models import (
            CONFIG_NUMBER_MAP,
            GLOBAL_CONFIGURABLE_FIELDS,
            PERSONAL_CONFIG_KEYS,
        )

        # Resolve config key from number if needed
        if config_key in CONFIG_NUMBER_MAP:
            config_key = CONFIG_NUMBER_MAP[config_key]

        # Parse subscription identifier
        subscription = await self._parse_subscription_identifier(name_or_id)
        if not subscription or subscription.id is None:
            event.set_result(
                MessageEventResult().message(f"❌ 订阅未找到: {name_or_id}")
            )
            return

        sub_id = subscription.id
        umo = event.unified_msg_origin
        subscriber = await self.db.get_subscriber(sub_id, umo)

        # Check permissions
        is_admin = event.role in ("admin", "superuser")
        is_personal = config_key in PERSONAL_CONFIG_KEYS
        is_global = config_key in GLOBAL_CONFIGURABLE_FIELDS

        # Personal config: must be subscriber
        if is_personal and not subscriber:
            event.set_result(
                MessageEventResult().message(f"❌ 您未订阅: {subscription.name}")
            )
            return

        # Global config: must be admin
        if is_global and not is_personal and not is_admin:
            event.set_result(MessageEventResult().message("❌ 全局配置需要管理员权限"))
            return

        if not is_personal and not is_global:
            event.set_result(
                MessageEventResult().message(
                    f"❌ 未知的配置项: `{config_key}`\n"
                    "可用配置项: "
                    + ", ".join(
                        list(PERSONAL_CONFIG_KEYS.keys())
                        + [
                            f
                            for f in GLOBAL_CONFIGURABLE_FIELDS
                            if f not in ("name", "url")
                        ]
                    )
                )
            )
            return

        # Get current value and validate new value
        old_value: bool | int | str | None
        new_value: bool | int | str

        if is_personal:
            if subscriber is None:
                event.set_result(
                    MessageEventResult().message(f"❌ 您未订阅: {subscription.name}")
                )
                return
            personal_config = subscriber.personal_config or {}
            old_value = personal_config.get(
                config_key, PERSONAL_CONFIG_KEYS[config_key]
            )
        else:
            old_value = getattr(subscription, config_key, None)

        # Parse and validate value based on field type
        try:
            parsed_value = self._parse_config_value(config_key, config_value)
        except ValueError as e:
            event.set_result(MessageEventResult().message(f"❌ 值验证失败: {e}"))
            return

        new_value = parsed_value

        # Save configuration
        if is_personal:
            if subscriber is None:
                event.set_result(
                    MessageEventResult().message(f"❌ 您未订阅: {subscription.name}")
                )
                return
            if subscriber.personal_config is None:
                subscriber.personal_config = {}
            subscriber.personal_config[config_key] = new_value
            await self.db.update_subscriber(subscriber)
        else:
            setattr(subscription, config_key, new_value)
            await self.db.update_subscription(subscription)
            # Refresh scheduler for relevant fields
            if config_key in ("interval", "stop"):
                await self.scheduler.schedule_subscription_fetch(subscription)

        # Build result table
        old_display = self._format_config_value(old_value)
        new_display = self._format_config_value(new_value)

        lines = [
            f"✅ **{subscription.name}** - 配置已更新\n",
            "| 参数 | 原值 | 新值 |",
            "|------|------|------|",
            f"| `{config_key}` | {old_display} | {new_display} |",
            "\n💡 可继续配置其他参数",
        ]

        event.set_result(MessageEventResult().message("\n".join(lines)))

    def _parse_config_value(self, config_key: str, value: str) -> bool | int | str:
        """Parse and validate config value based on key type.

        Args:
            config_key: The configuration key.
            value: The raw value string.

        Returns:
            Parsed value (bool, int, or str).

        Raises:
            ValueError: If value is invalid for the key type.
        """
        from .commands import GLOBAL_CONFIG_BOOL_FIELDS, GLOBAL_CONFIG_INT_FIELDS
        from .models import PERSONAL_CONFIG_KEYS

        # Boolean fields
        bool_keys = list(PERSONAL_CONFIG_KEYS.keys())
        # Remove non-bool keys from personal config
        bool_keys = [k for k in bool_keys if k != "black_keyword"]
        bool_keys.extend(GLOBAL_CONFIG_BOOL_FIELDS)

        if config_key in bool_keys:
            value_lower = value.lower().strip()
            if value_lower in ("true", "1", "yes", "on", "enable", "开启"):
                return True
            if value_lower in ("false", "0", "no", "off", "disable", "关闭"):
                return False
            raise ValueError(
                f"布尔值格式错误: '{value}'\n"
                "支持的格式: true/false, yes/no, 1/0, on/off, 开启/关闭"
            )

        # Integer fields
        if config_key in GLOBAL_CONFIG_INT_FIELDS:
            try:
                int_value = int(value)
            except ValueError as e:
                raise ValueError(f"必须是整数: '{value}'") from e

            # Validate ranges
            if config_key == "interval" and int_value < 1:
                raise ValueError("抓取间隔必须 >= 1 分钟")
            if config_key == "max_image_number" and int_value < 0:
                raise ValueError("最大图片数必须 >= 0")

            return int_value

        # Text fields (default)
        return value

    def _format_config_value(self, value: bool | int | str | None) -> str:
        """Format config value for display.

        Args:
            value: The value to format.

        Returns:
            Formatted string for display.
        """
        if value is None:
            return "`无`"
        if isinstance(value, bool):
            return "✅" if value else "❌"
        if isinstance(value, int):
            return f"`{value}`"
        if isinstance(value, str):
            return f"`{value}`" if value else "`空`"
        return f"`{str(value)}`"

    @filter.command_group("rssutil")
    @filter.permission_type(filter.PermissionType.ADMIN)
    def rssutil(self) -> None:
        """RSS utility commands (admin only)."""

    @rssutil.command("rsshub")
    async def rssutil_rsshub(self, event: AstrMessageEvent) -> None:
        """Print RSSHub URL. Usage: rssutil rsshub [path]"""
        await self.initialize()

        message = event.message_str.strip()
        path = message.replace("rssutil rsshub", "").strip() or None

        await self.util_commands.util_rsshub(event, path)

    @rssutil.command("test")
    async def rssutil_test(self, event: AstrMessageEvent) -> None:
        """Test RSS feed accessibility. Usage: rssutil test <url>"""
        await self.initialize()

        message = event.message_str.strip()
        url = message.replace("rssutil test", "").strip()

        if not url:
            event.set_result(event.make_result().message("Usage: /rssutil test <url>"))
            return

        await self.util_commands.util_test(event, url)

    @rssutil.command("trigger")
    async def rssutil_trigger(self, event: AstrMessageEvent) -> None:
        """Manually trigger RSS update. Usage: rssutil trigger [name|id]"""
        await self.initialize()

        message = event.message_str.strip()
        name_or_id = message.replace("rssutil trigger", "").strip() or None

        await self.util_commands.util_trigger(event, name_or_id)

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

    @rssgroup.command("time")
    async def rssgroup_time(self, event: AstrMessageEvent) -> None:
        """Manage digest schedule. Usage: rssgroup time <group_id> add/del <HH:MM>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rssgroup time", "").strip().split()

        if len(parts) < 3:
            event.set_result(
                event.make_result().message(
                    "Usage: /rssgroup time <group_id> add/del <HH:MM>"
                )
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            event.set_result(event.make_result().message("Group ID must be a number"))
            return

        subcmd = parts[1].lower()
        if subcmd not in ("add", "del"):
            event.set_result(
                event.make_result().message("Subcommand must be 'add' or 'del'")
            )
            return

        await self.group_commands.group_time(event, group_id, subcmd, parts[2])

    @filter.command_group("rsssub")
    def rsssub(self) -> None:
        """RSS subscriber management commands."""

    @rsssub.command("join")
    async def rsssub_join(self, event: AstrMessageEvent) -> None:
        """Subscribe to all feeds in a group. Usage: rsssub join <group_id>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rsssub join", "").strip().split()

        if len(parts) < 1:
            event.set_result(
                event.make_result().message("Usage: /rsssub join <group_id>")
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            event.set_result(event.make_result().message("Group ID must be a number"))
            return

        await self.sub_commands.sub_join(event, group_id)

    @rsssub.command("leave")
    async def rsssub_leave(self, event: AstrMessageEvent) -> None:
        """Unsubscribe from all feeds in a group. Usage: rsssub leave <group_id>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rsssub leave", "").strip().split()

        if len(parts) < 1:
            event.set_result(
                event.make_result().message("Usage: /rsssub leave <group_id>")
            )
            return

        try:
            group_id = int(parts[0])
        except ValueError:
            event.set_result(event.make_result().message("Group ID must be a number"))
            return

        await self.sub_commands.sub_leave(event, group_id)

    @rsssub.command("add")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def rsssub_add(self, event: AstrMessageEvent) -> None:
        """Add subscriber to subscription (admin). Usage: rsssub add <sub_id> <umo>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rsssub add", "").strip().split()

        if len(parts) < 2:
            event.set_result(
                event.make_result().message(
                    "Usage: /rsssub add <subscription_id> <umo>"
                )
            )
            return

        try:
            subscription_id = int(parts[0])
        except ValueError:
            event.set_result(
                event.make_result().message("Subscription ID must be a number")
            )
            return

        umo = parts[1]
        await self.sub_commands.sub_add(event, subscription_id, umo)

    @rsssub.command("del")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def rsssub_del(self, event: AstrMessageEvent) -> None:
        """Delete subscriber from subscription (admin). Usage: rsssub del <sub_id> <umo>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rsssub del", "").strip().split()

        if len(parts) < 2:
            event.set_result(
                event.make_result().message(
                    "Usage: /rsssub del <subscription_id> <umo>"
                )
            )
            return

        try:
            subscription_id = int(parts[0])
        except ValueError:
            event.set_result(
                event.make_result().message("Subscription ID must be a number")
            )
            return

        umo = parts[1]
        await self.sub_commands.sub_del(event, subscription_id, umo)

    @rsssub.command("list")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def rsssub_list(self, event: AstrMessageEvent) -> None:
        """List subscribers for subscription (admin). Usage: rsssub list <sub_id>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = message.replace("rsssub list", "").strip().split()

        if len(parts) < 1:
            event.set_result(
                event.make_result().message("Usage: /rsssub list <subscription_id>")
            )
            return

        try:
            subscription_id = int(parts[0])
        except ValueError:
            event.set_result(
                event.make_result().message("Subscription ID must be a number")
            )
            return

        await self.sub_commands.sub_list(event, subscription_id)

    async def _parse_subscription_identifier(
        self, name_or_id: str
    ) -> RSSSubscription | None:
        """Parse subscription identifier and return subscription.

        Args:
            name_or_id: Subscription ID (as int) or name (as str).

        Returns:
            RSSSubscription or None if not found.
        """
        try:
            sub_id = int(name_or_id)
            return await self.db.get_subscription(sub_id)
        except ValueError:
            all_subs = await self.db.get_all_subscriptions()
            for sub in all_subs:
                if sub.name == name_or_id:
                    return sub
            return None

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
            "type": "rssupdate_select",
            "user_id": user_id,
        }

        # Build keyboard buttons
        buttons = []
        for sub in user_subs:
            buttons.append(
                [
                    {
                        "text": f"📰 {sub.name} (ID: {sub.id})",
                        "callback_data": f"rssupdate_select:{session_id}:{sub.id}:{user_id}",
                    }
                ]
            )

        result = MessageEventResult()
        result.message("⚙️ Select a subscription to configure:")
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _refresh_config_keyboard_mode_b(
        self, event: TelegramCallbackQueryEvent, session_id: str, sub_id: int
    ) -> None:
        """Refresh the config keyboard after toggle (Mode B)."""
        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            result = MessageEventResult()
            result.message("❌ Session expired")
            event.set_result(result)
            return

        subscription = await self.db.get_subscription(sub_id)
        if not subscription:
            result = MessageEventResult()
            result.message("❌ 订阅未找到")
            event.set_result(result)
            return

        subscriber = await self.db.get_subscriber(sub_id, session["umo"])
        if not subscriber:
            result = MessageEventResult()
            result.message("❌ 您未订阅此源")
            event.set_result(result)
            return

        personal_config = subscriber.personal_config or {}

        buttons = []

        personal_config_layout = [
            [("only_title", "📄"), ("only_pic", "🖼️")],
            [("only_has_pic", "📸"), ("enable_spoiler", "👁️")],
            [("stop", "⏸️")],
        ]

        for row in personal_config_layout:
            row_buttons = []
            for key, emoji in row:
                current_value = personal_config.get(key, False)
                status = "✅" if current_value else "❌"
                button_text = f"{emoji} {status}"
                row_buttons.append(
                    {
                        "text": button_text,
                        "callback_data": f"rssupdate_toggle:{session_id}:{sub_id}:{key}",
                    }
                )
            buttons.append(row_buttons)

        global_config_layout = [
            [("ai_summary_enabled", "🤖"), ("enable_proxy", "🌐")],
        ]

        for row in global_config_layout:
            row_buttons = []
            for key, emoji in row:
                current_value = getattr(subscription, key, False)
                status = "✅" if current_value else "❌"
                button_text = f"{emoji} {status}"
                row_buttons.append(
                    {
                        "text": button_text,
                        "callback_data": f"rssupdate_toggle:{session_id}:{sub_id}:{key}",
                    }
                )
            buttons.append(row_buttons)

        numeric_configs = [("interval", "⏱️"), ("max_image_number", "📷")]
        for key, emoji in numeric_configs:
            current_value = getattr(subscription, key, 0)
            button_text = f"{emoji} {current_value}"
            buttons.append(
                [
                    {
                        "text": button_text,
                        "callback_data": f"rssupdate_edit:{session_id}:{sub_id}:{key}",
                    }
                ]
            )

        buttons.append(
            [
                {
                    "text": "⬅️ 返回列表",
                    "callback_data": f"rssupdate_back:{session_id}",
                }
            ]
        )

        result = MessageEventResult()
        result.message(
            f"⚙️ **{subscription.name}** - [{subscription.url}]({subscription.url})\n\n"
            f"📋 **个人配置** (点击切换)\n"
            f"🔧 **全局配置** (点击切换/编辑)\n\n"
            f"请点击按钮进行配置:"
        )
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _refresh_rssupdate_keyboard(
        self, event: TelegramCallbackQueryEvent, session_id: str
    ) -> None:
        """Refresh the subscription selection keyboard (return from Mode B)."""
        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            result = MessageEventResult()
            result.message("❌ Session expired")
            event.set_result(result)
            return

        umo = session.get("umo", "")
        all_subs = await self.db.get_all_subscriptions()

        user_subs = []
        for sub in all_subs:
            if sub.id is None:
                continue
            subscribers = await self.db.get_subscribers(sub.id)
            if any(s.umo == umo for s in subscribers):
                user_subs.append(sub)

        if not user_subs:
            result = MessageEventResult()
            result.message("📭 You have no subscriptions to configure.")
            event.set_result(result)
            return

        user_id = session.get("user_id", "")

        buttons = []
        for sub in user_subs:
            buttons.append(
                [
                    {
                        "text": f"📰 {sub.name} (ID: {sub.id})",
                        "callback_data": f"rssupdate_select:{session_id}:{sub.id}:{user_id}",
                    }
                ]
            )

        result = MessageEventResult()
        result.message("⚙️ Select a subscription to configure:")
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _show_rssupdate_selection(self, event: AstrMessageEvent) -> None:
        """Show text-based subscription selection list for non-Telegram platforms."""
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

        from .models import CONFIG_NUMBER_MAP

        number_symbols = list(CONFIG_NUMBER_MAP.keys())
        lines = ["⚙️ 请选择订阅进行配置:\n"]

        for idx, sub in enumerate(user_subs[:12]):
            number = number_symbols[idx] if idx < len(number_symbols) else str(idx + 1)
            status = "⭕ 已暂停" if sub.stop else "✅ 运行中"
            lines.append(f"{number} {sub.name} - {status}")
            lines.append(f"   ID: {sub.id} | {sub.url}")

        if len(user_subs) > 12:
            lines.append(f"\n... 还有 {len(user_subs) - 12} 个订阅未显示")
            lines.append("请直接输入订阅 ID 或名称")

        lines.append("\n请回复订阅 ID/名称/序号")

        event.set_result(MessageEventResult().message("\n".join(lines)))

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

        subscriber = await self.db.get_subscriber(sub_id, session["umo"])
        if not subscriber:
            await event.answer_callback_query(text="❌ You are not subscribed")
            return

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

        buttons.append(
            [
                {
                    "text": "⬅️ 返回列表",
                    "callback_data": f"rssupdate_back:{session_id}",
                }
            ]
        )

        result = MessageEventResult()
        result.message(
            f"⚙️ **{subscription.name}** - [{subscription.url}]({subscription.url}) 配置\n\n点击按钮切换开关状态:"
        )
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _show_config_keyboard_mode_b(
        self, event: AstrMessageEvent, name_or_id: str
    ) -> None:
        """Show config keyboard for a subscription (Mode B, Telegram only).

        Args:
            event: The message event.
            name_or_id: Subscription identifier (ID or name).
        """
        await self.initialize()
        umo = event.unified_msg_origin

        subscription = await self._parse_subscription_identifier(name_or_id)
        if not subscription or subscription.id is None:
            event.set_result(
                MessageEventResult().message(f"❌ 订阅未找到: {name_or_id}")
            )
            return

        sub_id = subscription.id
        subscriber = await self.db.get_subscriber(sub_id, umo)
        if not subscriber:
            event.set_result(
                MessageEventResult().message(f"❌ 您未订阅: {subscription.name}")
            )
            return

        session_id = uuid.uuid4().hex[:8]
        user_id = event.get_sender_id()
        KEYBOARD_SESSIONS[session_id] = {
            "umo": umo,
            "type": "rssupdate_mode_b",
            "user_id": user_id,
        }

        personal_config = subscriber.personal_config or {}

        buttons = []

        personal_config_layout = [
            [("only_title", "📄"), ("only_pic", "🖼️")],
            [("only_has_pic", "📸"), ("enable_spoiler", "👁️")],
            [("stop", "⏸️")],
        ]

        for row in personal_config_layout:
            row_buttons = []
            for key, emoji in row:
                current_value = personal_config.get(key, False)
                status = "✅" if current_value else "❌"
                button_text = f"{emoji} {status}"
                row_buttons.append(
                    {
                        "text": button_text,
                        "callback_data": f"rssupdate_toggle:{session_id}:{sub_id}:{key}",
                    }
                )
            buttons.append(row_buttons)

        global_config_layout = [
            [("ai_summary_enabled", "🤖"), ("enable_proxy", "🌐")],
        ]

        for row in global_config_layout:
            row_buttons = []
            for key, emoji in row:
                current_value = getattr(subscription, key, False)
                status = "✅" if current_value else "❌"
                button_text = f"{emoji} {status}"
                row_buttons.append(
                    {
                        "text": button_text,
                        "callback_data": f"rssupdate_toggle:{session_id}:{sub_id}:{key}",
                    }
                )
            buttons.append(row_buttons)

        numeric_configs = [("interval", "⏱️"), ("max_image_number", "📷")]
        for key, emoji in numeric_configs:
            current_value = getattr(subscription, key, 0)
            button_text = f"{emoji} {current_value}"
            buttons.append(
                [
                    {
                        "text": button_text,
                        "callback_data": f"rssupdate_edit:{session_id}:{sub_id}:{key}",
                    }
                ]
            )

        buttons.append(
            [
                {
                    "text": "⬅️ 返回列表",
                    "callback_data": f"rssupdate_back:{session_id}",
                }
            ]
        )

        result = MessageEventResult()
        result.message(
            f"⚙️ **{subscription.name}** - [{subscription.url}]({subscription.url})\n\n"
            f"📋 **个人配置** (点击切换)\n"
            f"🔧 **全局配置** (点击切换/编辑)\n\n"
            f"请点击按钮进行配置:"
        )
        result.inline_keyboard(buttons)
        event.set_result(result)

    async def _show_config_table_mode_b(
        self, event: AstrMessageEvent, name_or_id: str
    ) -> None:
        """Show config table for a subscription (Mode B, non-Telegram platforms).

        Args:
            event: The message event.
            name_or_id: Subscription identifier (ID or name).
        """
        await self.initialize()
        umo = event.unified_msg_origin

        subscription = await self._parse_subscription_identifier(name_or_id)
        if not subscription or subscription.id is None:
            event.set_result(
                MessageEventResult().message(f"❌ 订阅未找到: {name_or_id}")
            )
            return

        sub_id = subscription.id
        subscriber = await self.db.get_subscriber(sub_id, umo)
        if not subscriber:
            event.set_result(
                MessageEventResult().message(f"❌ 您未订阅: {subscription.name}")
            )
            return

        personal_config = subscriber.personal_config or {}

        from .models import (
            GLOBAL_CONFIG_NUMBERS,
            PERSONAL_CONFIG_NUMBERS,
        )

        lines = [
            f"⚙️ **{subscription.name}** - [{subscription.url}]({subscription.url})\n\n",
            "📋 **个人配置**",
            "| 序号 | 参数 | 当前值 | 说明 |",
            "|------|------|--------|------|",
        ]

        personal_configs = [
            ("only_title", "仅发送标题"),
            ("only_pic", "仅发送图片"),
            ("only_has_pic", "仅发送有图片的文章"),
            ("enable_spoiler", "图片剧透标签"),
            ("stop", "暂停订阅"),
            ("black_keyword", "关键词黑名单"),
        ]

        for idx, (key, desc) in enumerate(personal_configs):
            number = (
                PERSONAL_CONFIG_NUMBERS[idx]
                if idx < len(PERSONAL_CONFIG_NUMBERS)
                else str(idx + 1)
            )
            current = personal_config.get(key, False if key != "black_keyword" else "")
            if key == "black_keyword":
                current_str = f"`{current}`" if current else "`无`"
            else:
                current_str = "✅" if current else "❌"
            lines.append(f"| {number} | `{key}` | {current_str} | {desc} |")

        lines.append("")
        lines.append("🔧 **全局配置**")
        lines.append("| 序号 | 参数 | 当前值 | 说明 |")
        lines.append("|------|------|--------|------|")

        global_configs = [
            ("interval", "抓取间隔（分钟）", subscription.interval),
            ("max_image_number", "最大图片数", subscription.max_image_number),
            ("ai_summary_enabled", "AI摘要开关", subscription.ai_summary_enabled),
            ("enable_proxy", "使用代理", subscription.enable_proxy),
            ("source_group_id", "所属分组ID", subscription.source_group_id),
        ]

        for idx, (key, desc, value) in enumerate(global_configs):
            number = (
                GLOBAL_CONFIG_NUMBERS[idx]
                if idx < len(GLOBAL_CONFIG_NUMBERS)
                else str(idx + 7)
            )
            if isinstance(value, bool):
                value_str = "✅" if value else "❌"
            else:
                value_str = f"`{value}`"
            lines.append(f"| {number} | `{key}` | {value_str} | {desc} |")

        lines.append("\n**用法**: `/rssupdate <订阅ID> <序号|参数名> <值>`")
        lines.append(
            "\n示例: `/rssupdate {sub_id} ① true` 或 `/rssupdate {sub_id} only_title true`"
        )

        event.set_result(MessageEventResult().message("\n".join(lines)))

    async def _handle_mode_c(
        self, event: AstrMessageEvent, name_or_id: str, config_key: str
    ) -> None:
        from .models import (
            CONFIG_DESCRIPTIONS,
            CONFIG_NAME_MAP,
            GLOBAL_CONFIGURABLE_FIELDS,
            PERSONAL_CONFIG_KEYS,
        )

        subscription = await self._parse_subscription_identifier(name_or_id)
        if not subscription or subscription.id is None:
            event.set_result(
                MessageEventResult().message(f"❌ 订阅未找到: {name_or_id}")
            )
            return

        umo = event.unified_msg_origin
        subscriber = await self.db.get_subscriber(subscription.id, umo)
        if not subscriber:
            event.set_result(
                MessageEventResult().message(f"❌ 您未订阅: {subscription.name}")
            )
            return

        is_personal = config_key in PERSONAL_CONFIG_KEYS
        is_global = config_key in GLOBAL_CONFIGURABLE_FIELDS

        if not is_personal and not is_global:
            event.set_result(
                MessageEventResult().message(
                    f"❌ 未知的配置项: `{config_key}`\n"
                    "可用配置项: "
                    + ", ".join(
                        list(PERSONAL_CONFIG_KEYS.keys())
                        + [
                            f
                            for f in GLOBAL_CONFIGURABLE_FIELDS
                            if f != "name" and f != "url"
                        ]
                    )
                )
            )
            return

        if is_personal:
            personal_config = subscriber.personal_config or {}
            current_value = personal_config.get(
                config_key, PERSONAL_CONFIG_KEYS[config_key]
            )
        else:
            current_value = getattr(subscription, config_key, None)

        if config_key == "black_keyword":
            desc_key = (
                "black_keyword_personal" if is_personal else "black_keyword_global"
            )
        else:
            desc_key = config_key

        number = CONFIG_NAME_MAP.get(config_key, "")
        config_desc = CONFIG_DESCRIPTIONS.get(desc_key, f"{number} {config_key}")

        if isinstance(current_value, bool):
            current_display = "✅ 开启" if current_value else "❌ 关闭"
            type_hint = "类型: 开关 (true/false)"
            example = "示例: `true` 或 `false`"
        elif isinstance(current_value, int):
            current_display = f"{current_value}"
            type_hint = "类型: 数字"
            example = "示例: `10`"
        else:
            current_display = f"`{current_value}`" if current_value else "无"
            type_hint = "类型: 文本"
            example = "示例: `keyword1,keyword2`"

        scope = "📋 个人配置" if is_personal else "🔧 全局配置"

        message = (
            f"⚙️ **{subscription.name}** - 配置详情\n\n"
            f"{scope}\n"
            f"配置项: `{config_key}`\n"
            f"说明: {config_desc.format(value=current_display)}\n"
            f"{type_hint}\n\n"
            f"请输入新值:\n"
            f"{example}"
        )

        event.set_result(MessageEventResult().message(message))

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

            # Handle rssupdate_select callback (show config keyboard)
            if action == "rssupdate_select" and len(parts) >= 4:
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

            # Handle rssupdate_toggle callback (Mode B toggle)
            if action == "rssupdate_toggle" and len(parts) >= 4:
                session_id = parts[1]
                sub_id = int(parts[2])
                config_key = parts[3]

                session = KEYBOARD_SESSIONS.get(session_id)
                if not session:
                    await event.answer_callback_query(text="❌ Session expired")
                    return

                subscription = await self.db.get_subscription(sub_id)
                if not subscription:
                    await event.answer_callback_query(text="❌ 订阅未找到")
                    return

                subscriber = await self.db.get_subscriber(sub_id, session["umo"])
                if not subscriber:
                    await event.answer_callback_query(text="❌ 您未订阅此源")
                    return

                personal_config_keys = [
                    "only_title",
                    "only_pic",
                    "only_has_pic",
                    "enable_spoiler",
                    "stop",
                    "black_keyword",
                ]
                global_config_keys = ["ai_summary_enabled", "enable_proxy"]

                if config_key in personal_config_keys:
                    if subscriber.personal_config is None:
                        subscriber.personal_config = {}
                    current_value = subscriber.personal_config.get(config_key, False)
                    subscriber.personal_config[config_key] = not current_value
                    await self.db.update_subscriber(subscriber)
                    new_value = subscriber.personal_config[config_key]
                    status_text = "已开启" if new_value else "已关闭"
                    await event.answer_callback_query(
                        text=f"✅ {config_key} {status_text}"
                    )
                elif config_key in global_config_keys:
                    current_value = getattr(subscription, config_key, False)
                    setattr(subscription, config_key, not current_value)
                    await self.db.update_subscription(subscription)
                    await self.scheduler.schedule_subscription_fetch(subscription)
                    new_value = getattr(subscription, config_key, False)
                    status_text = "已开启" if new_value else "已关闭"
                    await event.answer_callback_query(
                        text=f"✅ {config_key} {status_text}"
                    )
                else:
                    await event.answer_callback_query(text="❌ 无效配置项")
                    return

                await self._refresh_config_keyboard_mode_b(event, session_id, sub_id)
                return

            # Handle rssupdate_back callback (return to list)
            if action == "rssupdate_back" and len(parts) >= 2:
                session_id = parts[1]
                await self._refresh_rssupdate_keyboard(event, session_id)
                return

            # Handle rssupdate_edit callback (edit numeric configs)
            if action == "rssupdate_edit" and len(parts) >= 4:
                session_id = parts[1]
                sub_id = int(parts[2])
                config_key = parts[3]

                session = KEYBOARD_SESSIONS.get(session_id)
                if not session:
                    await event.answer_callback_query(text="❌ Session expired")
                    return

                subscription = await self.db.get_subscription(sub_id)
                if not subscription:
                    await event.answer_callback_query(text="❌ 订阅未找到")
                    return

                numeric_config_keys = ["interval", "max_image_number"]
                if config_key not in numeric_config_keys:
                    await event.answer_callback_query(text="❌ 无效配置项")
                    return

                current_value = getattr(subscription, config_key, 0)
                config_names = {
                    "interval": "抓取间隔（分钟）",
                    "max_image_number": "最大图片数",
                }
                config_name = config_names.get(config_key, config_key)

                await event.answer_callback_query(
                    text=f"当前值: {current_value}。请回复新的数值"
                )
                result = MessageEventResult()
                result.message(
                    f"📝 **编辑配置项**\n\n"
                    f"订阅: {subscription.name}\n"
                    f"配置: {config_name}\n"
                    f"当前值: {current_value}\n\n"
                    f"请使用命令设置新值:\n"
                    f"`/rssupdate {sub_id} {config_key} <新数值>`"
                )
                event.set_result(result)
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
