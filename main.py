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
from astrbot.core.platform.sources.telegram.tg_event import (
    TelegramCallbackQueryEvent,
    TelegramPlatformEvent,
)
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
        data_path = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        data_path.mkdir(parents=True, exist_ok=True)
        db_path = data_path / "rss.db"

        self.db = Database(db_path)
        await self.db.init_db()

        proxy_config = self.config.get("proxy_config", {})
        fetch_config = self.config.get("fetch_config", {})
        self.fetcher = RSSFetcher(
            proxy=proxy_config.get("proxy") or None,
            proxy_enabled=proxy_config.get("enable_proxy", False),
            timeout=fetch_config.get("request_timeout", 30),
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
            self.fetcher,
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

    def _is_admin(self, event: AstrMessageEvent, session_id: str | None = None) -> bool:
        """Check admin role, falling back to the keyboard session creator."""
        if event.role in ("admin", "superuser"):
            return True
        if not session_id:
            return False

        session = KEYBOARD_SESSIONS.get(session_id)
        if not session or not session.get("is_admin", False):
            return False

        session_user_id = session.get("user_id")
        return not session_user_id or session_user_id == event.get_sender_id()

    async def _send_or_set_result(
        self,
        event: AstrMessageEvent | TelegramCallbackQueryEvent,
        result: MessageEventResult,
    ) -> None:
        """Edit callback messages immediately; use normal result flow otherwise."""
        event.set_result(result)
        if isinstance(event, TelegramCallbackQueryEvent):
            await event.send(result)
            event.clear_result()

    async def _delete_telegram_user_message(self, event: AstrMessageEvent) -> None:
        """Delete the Telegram message that supplied a pending config value.

        Args:
            event: Telegram message event containing the user reply.
        """
        if not isinstance(event, TelegramPlatformEvent):
            return
        raw_message = getattr(event.message_obj, "raw_message", None)
        telegram_message = getattr(raw_message, "message", None)
        if not telegram_message:
            return
        try:
            await event.client.delete_message(
                chat_id=telegram_message.chat.id,
                message_id=telegram_message.message_id,
            )
        except Exception as e:
            logger.debug(f"Failed to delete Telegram pending input message: {e}")

    def _build_pending_callback_event(
        self, event: AstrMessageEvent, pending_edit: dict
    ) -> TelegramCallbackQueryEvent:
        """Create a callback-like event to edit the original Telegram menu.

        Args:
            event: Telegram message event that completed the pending edit.
            pending_edit: Pending edit state saved from the original callback.

        Returns:
            Callback query event targeting the original menu message.
        """
        return TelegramCallbackQueryEvent(
            callback_query_id="",
            data="",
            from_user_id=event.get_sender_id(),
            from_username=event.get_sender_name(),
            message=pending_edit.get("message"),
            inline_message_id=pending_edit.get("inline_message_id"),
            platform_meta=event.platform_meta,
            session_id=event.session_id,
            client=event.client,
        )

    async def _refresh_pending_edit_menu(
        self,
        event: AstrMessageEvent,
        session_id: str,
        pending_edit: dict,
        status_line: str | None = None,
    ) -> None:
        """Refresh the menu that started a pending Telegram config edit.

        Args:
            event: Telegram message event that completed or cancelled the edit.
            session_id: Keyboard session ID.
            pending_edit: Pending edit state saved from the original callback.
            status_line: Optional status message to include above the panel.
        """
        callback_event = self._build_pending_callback_event(event, pending_edit)
        sub_id = pending_edit["sub_id"]
        if pending_edit["scope"] == "global":
            session = KEYBOARD_SESSIONS.get(session_id)
            await self._show_global_config_keyboard(
                callback_event,
                sub_id,
                session_id=session_id if session_id != "0" else None,
                return_to_personal=bool(
                    session and session.get("type") != "globalconfig"
                ),
                status_line=status_line,
            )
        else:
            await self._refresh_config_keyboard_mode_b(
                callback_event, session_id, sub_id, status_line=status_line
            )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=999999)
    async def handle_pending_telegram_config_input(
        self, event: AstrMessageEvent
    ) -> None:
        """Handle the next Telegram message for a pending config edit.

        Args:
            event: Incoming message event.
        """
        if event.get_platform_name() != "telegram":
            return

        session_id = None
        session = None
        for current_session_id, current_session in KEYBOARD_SESSIONS.items():
            pending_edit = current_session.get("pending_edit")
            if (
                pending_edit
                and current_session.get("user_id") == event.get_sender_id()
                and current_session.get("umo") == event.unified_msg_origin
            ):
                session_id = current_session_id
                session = current_session
                break

        if not session_id or not session:
            return

        pending_edit = session["pending_edit"]
        new_value = event.message_str.strip()
        if not new_value:
            return

        event.stop_event()
        await self._delete_telegram_user_message(event)

        cancel_words = {"cancel", "取消", "退出", "返回", "/cancel"}
        if new_value.lower() in cancel_words:
            session.pop("pending_edit", None)
            await self._refresh_pending_edit_menu(
                event, session_id, pending_edit, "已取消编辑。"
            )
            return

        await self._handle_mode_d(
            event,
            str(pending_edit["sub_id"]),
            pending_edit["config_key"],
            new_value,
            pending_edit["scope"],
            session_id=session_id,
        )
        result = event.get_result()
        status_line = None
        if result:
            status_line = result.get_plain_text(with_other_comps_mark=True).split(
                "\n", 1
            )[0]
            event.clear_result()

        if status_line and status_line.startswith("✅"):
            session.pop("pending_edit", None)
            await self._refresh_pending_edit_menu(
                event, session_id, pending_edit, status_line
            )
        else:
            callback_event = self._build_pending_callback_event(event, pending_edit)
            await self._show_config_prompt(
                callback_event,
                session_id,
                pending_edit["sub_id"],
                pending_edit["scope"],
                pending_edit["config_key"],
                status_line=status_line,
            )

    def _parse_args(self, message: str) -> list[str]:
        """Parse message into arguments."""
        parts = message.strip().split()
        return parts if parts else []

    def _strip_command(self, message: str, command: str) -> str:
        """Return text after the matched command without touching arguments."""
        text = message.strip()
        if text.startswith("/"):
            text = text[1:]
        if text == command:
            return ""
        if text.startswith(f"{command} "):
            return text[len(command) :].strip()
        return text

    @staticmethod
    def _resolve_update_config_key(raw_key: str) -> tuple[str, str | None]:
        """Resolve numbered config keys and return an optional forced scope."""
        from .models import CONFIG_NUMBER_MAP, GLOBAL_CONFIG_NUMBERS

        if raw_key in CONFIG_NUMBER_MAP:
            scope = "global" if raw_key in GLOBAL_CONFIG_NUMBERS else "personal"
            return CONFIG_NUMBER_MAP[raw_key], scope
        return raw_key, None

    @filter.command("rssadd")
    async def rssadd(self, event: AstrMessageEvent) -> None:
        """Add an RSS subscription.

        Usage:
            /rssadd <url> [name] - Add RSS subscription
        """
        await self.initialize()

        message = event.message_str.strip()
        args_text = self._strip_command(message, "rssadd")
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
        name = " ".join(parts[1:]) if len(parts) > 1 else None
        await self.rss_commands.rssadd(event, url, name)

    @filter.command("rssdel")
    async def rssdel(self, event: AstrMessageEvent) -> None:
        """Delete an RSS subscription.

        Usage:
            /rssdel <name|id> - Delete subscription (or unsubscribe)
        """
        await self.initialize()

        message = event.message_str.strip()
        args_text = self._strip_command(message, "rssdel")
        parts = self._parse_args(args_text)

        # On Telegram, show keyboard when no args provided
        if not parts and event.get_platform_name() == "telegram":
            await self._show_rssdel_keyboard(event)
            return

        if not parts:
            event.set_result(event.make_result().message("Usage: /rssdel <name|id>"))
            return

        # Normal flow: delete subscription
        name_or_id = args_text
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
        args_text = self._strip_command(message, "rssupdate")
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
        config_value = " ".join(parts[2:]) if len(parts) > 2 else None

        if len(parts) == 2 and config_key_or_value:
            from .models import CONFIG_NAME_MAP, CONFIG_NUMBER_MAP

            is_config_number = config_key_or_value in CONFIG_NUMBER_MAP
            is_config_name = config_key_or_value in CONFIG_NAME_MAP

            if is_config_number or is_config_name:
                config_key, forced_scope = self._resolve_update_config_key(
                    config_key_or_value
                )
                await self._handle_mode_c(event, name_or_id, config_key, forced_scope)
                return

        config_key = config_key_or_value

        # Mode D: Full parameters - direct config modification
        if len(parts) >= 3 and config_key and config_value:
            config_key, forced_scope = self._resolve_update_config_key(config_key)
            await self._handle_mode_d(
                event, name_or_id, config_key, config_value, forced_scope
            )
            return

        await self.rss_commands.rssupdate(event, name_or_id, config_key, config_value)

    async def _handle_mode_d(
        self,
        event: AstrMessageEvent,
        name_or_id: str,
        config_key: str,
        config_value: str,
        forced_scope: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Handle Mode D: Direct config modification with full parameters.

        Args:
            event: The message event.
            name_or_id: Subscription identifier (ID or name).
            config_key: Configuration key (name or number).
            config_value: New value for the configuration.
            forced_scope: Optional scope forced by numbered config keys.
            session_id: Optional keyboard session for Telegram callback permissions.
        """
        from .models import GLOBAL_CONFIGURABLE_FIELDS, PERSONAL_CONFIG_KEYS

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
        is_admin = self._is_admin(event, session_id)
        key_is_personal = config_key in PERSONAL_CONFIG_KEYS
        key_is_global = config_key in GLOBAL_CONFIGURABLE_FIELDS
        is_personal = key_is_personal and forced_scope != "global"
        is_global = key_is_global and forced_scope != "personal"

        # Personal config: must be subscriber
        if is_personal and not subscriber:
            event.set_result(
                MessageEventResult().message(f"❌ 您未订阅: {subscription.name}")
            )
            return

        # Global config: must be admin
        if is_global and not is_admin:
            event.set_result(MessageEventResult().message("❌ 全局配置需要管理员权限"))
            return

        if (
            forced_scope == "personal"
            and not key_is_personal
            or forced_scope == "global"
            and not key_is_global
            or not is_personal
            and not is_global
        ):
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

        if is_global and config_key == "source_group_id" and isinstance(new_value, int):
            group = await self.db.get_group(new_value)
            if not group:
                event.set_result(
                    MessageEventResult().message(f"❌ Group ID {new_value} not found")
                )
                return

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
            if config_key in ("interval", "max_image_number", "enable_proxy", "stop"):
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
        bool_keys = [
            k for k in bool_keys if k not in ("black_keyword", "white_keyword")
        ]
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

        message = event.message_str.strip().lstrip("/")
        path = self._strip_command(message, "rssutil rsshub") or None

        await self.util_commands.util_rsshub(event, path)

    @rssutil.command("test")
    async def rssutil_test(self, event: AstrMessageEvent) -> None:
        """Test RSS feed accessibility. Usage: rssutil test <url>"""
        await self.initialize()

        message = event.message_str.strip()
        url = self._strip_command(message, "rssutil test")

        if not url:
            event.set_result(event.make_result().message("Usage: /rssutil test <url>"))
            return

        await self.util_commands.util_test(event, url)

    @rssutil.command("trigger")
    async def rssutil_trigger(self, event: AstrMessageEvent) -> None:
        """Manually trigger RSS update. Usage: rssutil trigger [name|id]"""
        await self.initialize()

        message = event.message_str.strip()
        name_or_id = self._strip_command(message, "rssutil trigger") or None

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
        name = self._strip_command(message, "rssgroup add")

        if not name:
            event.set_result(event.make_result().message("Usage: /rssgroup add <name>"))
            return

        await self.group_commands.group_add(event, name)

    @rssgroup.command("rename")
    async def rssgroup_rename(self, event: AstrMessageEvent) -> None:
        """Rename a group. Usage: rssgroup rename <id> <new_name>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = self._strip_command(message, "rssgroup rename").split(maxsplit=1)

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
        """Manage digest schedule. Usage: rssgroup time <group_id> add/del <schedule>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = self._strip_command(message, "rssgroup time").split()

        if len(parts) < 3:
            event.set_result(
                event.make_result().message(
                    "Usage: /rssgroup time <group_id> add/del <HH:MM|cron>"
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

        schedule_str = " ".join(parts[2:])
        await self.group_commands.group_time(event, group_id, subcmd, schedule_str)

    @filter.command_group("rsssub")
    def rsssub(self) -> None:
        """RSS subscriber management commands."""

    @rsssub.command("join")
    async def rsssub_join(self, event: AstrMessageEvent) -> None:
        """Subscribe to all feeds in a group. Usage: rsssub join <group_id>"""
        await self.initialize()

        message = event.message_str.strip()
        parts = self._strip_command(message, "rsssub join").split()

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
        parts = self._strip_command(message, "rsssub leave").split()

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
        parts = self._strip_command(message, "rsssub add").split()

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
        parts = self._strip_command(message, "rsssub del").split()

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
        parts = self._strip_command(message, "rsssub list").split()

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
        await self._send_or_set_result(event, result)

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
            "is_admin": self._is_admin(event),
        }

        # Build keyboard buttons
        buttons = []
        for sub in user_subs:
            buttons.append(
                [
                    {
                        "text": f"📰 {sub.name} (ID: {sub.id})",
                        "callback_data": f"rus:{session_id}:{sub.id}",
                    }
                ]
            )

        result = MessageEventResult()
        result.message("⚙️ Select a subscription to configure:")
        result.inline_keyboard(buttons)
        await self._send_or_set_result(event, result)

    def _build_rssupdate_config_buttons(
        self,
        session_id: str,
        sub_id: int,
        subscription: RSSSubscription,
        personal_config: dict,
        is_admin: bool,
    ) -> list[list[dict[str, str]]]:
        """Build the personal rssupdate keyboard with short readable labels."""
        buttons: list[list[dict[str, str]]] = []

        personal_config_layout = [
            [("only_title", "标题", "📄"), ("only_pic", "图片", "🖼️")],
            [("only_has_pic", "有图", "📸"), ("enable_spoiler", "剧透", "👁️")],
            [("ai_filter_enabled", "AI过滤", "🤖"), ("stop", "暂停", "⏸️")],
        ]

        for row in personal_config_layout:
            row_buttons = []
            for key, label, emoji in row:
                current_value = personal_config.get(key, False)
                status = "✅" if current_value else "❌"
                row_buttons.append(
                    {
                        "text": f"{emoji} {label} {status}",
                        "callback_data": f"rut:{session_id}:{sub_id}:{key}",
                    }
                )
            buttons.append(row_buttons)

        buttons.append(
            [
                {
                    "text": "🚫 黑词",
                    "callback_data": f"rup:{session_id}:{sub_id}:personal:black_keyword",
                },
                {
                    "text": "🔎 白词",
                    "callback_data": f"rup:{session_id}:{sub_id}:personal:white_keyword",
                },
            ]
        )

        if is_admin:
            buttons.append(
                [
                    {
                        "text": "🔧 全局",
                        "callback_data": f"rug:{session_id}:{sub_id}",
                    }
                ]
            )

        buttons.append(
            [
                {
                    "text": "⬅️ 返回列表",
                    "callback_data": f"rub:{session_id}",
                }
            ]
        )

        return buttons

    def _build_global_config_buttons(
        self,
        sub_id: int,
        subscription: RSSSubscription,
        session_id: str | None = None,
        return_to_personal: bool = False,
    ) -> list[list[dict[str, str]]]:
        """Build the global config keyboard for admin users."""
        callback_session = session_id or "0"
        buttons: list[list[dict[str, str]]] = []

        global_bool_layout = [
            [("ai_summary_enabled", "摘要", "🤖"), ("enable_proxy", "代理", "🌐")],
            [("stop", "暂停", "⏸️")],
        ]
        for row in global_bool_layout:
            row_buttons = []
            for key, label, emoji in row:
                current_value = getattr(subscription, key, False)
                status = "✅" if current_value else "❌"
                row_buttons.append(
                    {
                        "text": f"{emoji} {label} {status}",
                        "callback_data": f"rgt:{callback_session}:{sub_id}:{key}",
                    }
                )
            buttons.append(row_buttons)

        buttons.append(
            [
                {
                    "text": f"⏱️ 间隔 {subscription.interval}",
                    "callback_data": f"rup:{callback_session}:{sub_id}:global:interval",
                },
                {
                    "text": f"📷 图数 {subscription.max_image_number}",
                    "callback_data": f"rup:{callback_session}:{sub_id}:global:max_image_number",
                },
            ]
        )
        buttons.append(
            [
                {
                    "text": f"🧩 分组 {subscription.source_group_id}",
                    "callback_data": f"rup:{callback_session}:{sub_id}:global:source_group_id",
                },
                {
                    "text": "🚫 黑词",
                    "callback_data": f"rup:{callback_session}:{sub_id}:global:black_keyword",
                },
            ]
        )

        if session_id and return_to_personal:
            buttons.append(
                [
                    {
                        "text": "📋 个人",
                        "callback_data": f"rups:{session_id}:{sub_id}",
                    },
                    {
                        "text": "⬅️ 列表",
                        "callback_data": f"rub:{session_id}",
                    },
                ]
            )
        else:
            back_session = session_id or "0"
            buttons.append(
                [
                    {
                        "text": "⬅️ 返回列表",
                        "callback_data": f"globallist:{back_session}:nop",
                    }
                ]
            )

        return buttons

    async def _refresh_config_keyboard_mode_b(
        self,
        event: TelegramCallbackQueryEvent,
        session_id: str,
        sub_id: int,
        status_line: str | None = None,
    ) -> None:
        """Refresh the config keyboard after toggle (Mode B)."""
        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            result = MessageEventResult()
            result.message("❌ Session expired")
            await self._send_or_set_result(event, result)
            return

        subscription = await self.db.get_subscription(sub_id)
        if not subscription:
            result = MessageEventResult()
            result.message("❌ 订阅未找到")
            await self._send_or_set_result(event, result)
            return

        subscriber = await self.db.get_subscriber(sub_id, session["umo"])
        if not subscriber:
            result = MessageEventResult()
            result.message("❌ 您未订阅此源")
            await self._send_or_set_result(event, result)
            return

        personal_config = subscriber.personal_config or {}

        is_admin = self._is_admin(event, session_id)
        buttons = self._build_rssupdate_config_buttons(
            session_id, sub_id, subscription, personal_config, is_admin
        )

        result = MessageEventResult()
        status_text = f"{status_line}\n\n" if status_line else ""
        result.message(
            f"{status_text}"
            f"⚙️ **{subscription.name}** - [{subscription.url}]({subscription.url})\n\n"
            f"📋 **个人配置**\n"
            f"{'管理员可点 🔧 全局 切换全局配置。' if is_admin else ''}"
        )
        result.inline_keyboard(buttons)
        await self._send_or_set_result(event, result)

    async def _refresh_rssupdate_keyboard(
        self, event: TelegramCallbackQueryEvent, session_id: str
    ) -> None:
        """Refresh the subscription selection keyboard (return from Mode B)."""
        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            result = MessageEventResult()
            result.message("❌ Session expired")
            await self._send_or_set_result(event, result)
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
            await self._send_or_set_result(event, result)
            return

        buttons = []
        for sub in user_subs:
            buttons.append(
                [
                    {
                        "text": f"📰 {sub.name} (ID: {sub.id})",
                        "callback_data": f"rus:{session_id}:{sub.id}",
                    }
                ]
            )

        result = MessageEventResult()
        result.message("⚙️ Select a subscription to configure:")
        result.inline_keyboard(buttons)
        await self._send_or_set_result(event, result)

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
        _user_id: str,
    ) -> None:
        """Show config toggle keyboard for a subscription."""
        await self._refresh_config_keyboard_mode_b(event, session_id, sub_id)

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
            "is_admin": self._is_admin(event),
        }

        personal_config = subscriber.personal_config or {}

        is_admin = event.role in ("admin", "superuser")
        buttons = self._build_rssupdate_config_buttons(
            session_id, sub_id, subscription, personal_config, is_admin
        )

        result = MessageEventResult()
        result.message(
            f"⚙️ **{subscription.name}** - [{subscription.url}]({subscription.url})\n\n"
            f"📋 **个人配置**\n"
            f"{'管理员可点 🔧 全局 切换全局配置。' if is_admin else ''}"
        )
        result.inline_keyboard(buttons)
        await self._send_or_set_result(event, result)

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
            ("white_keyword", "关键词白名单"),
            ("ai_filter_enabled", "AI筛选过滤"),
        ]

        for idx, (key, desc) in enumerate(personal_configs):
            number = (
                PERSONAL_CONFIG_NUMBERS[idx]
                if idx < len(PERSONAL_CONFIG_NUMBERS)
                else str(idx + 1)
            )
            current = personal_config.get(
                key, "" if key in ("black_keyword", "white_keyword") else False
            )
            if key in ("black_keyword", "white_keyword"):
                current_str = f"`{current}`" if current else "`无`"
            else:
                current_str = "✅" if current else "❌"
            lines.append(f"| {number} | `{key}` | {current_str} | {desc} |")

        is_admin = event.role in ("admin", "superuser")
        if is_admin:
            lines.append("")
            lines.append("🔧 **全局配置（管理员）**")
            lines.append("| 序号 | 参数 | 当前值 | 说明 |")
            lines.append("|------|------|--------|------|")

            global_configs = [
                ("interval", "抓取间隔（分钟）", subscription.interval),
                ("max_image_number", "最大图片数", subscription.max_image_number),
                ("ai_summary_enabled", "AI摘要开关", subscription.ai_summary_enabled),
                ("enable_proxy", "使用代理", subscription.enable_proxy),
                ("source_group_id", "所属分组ID", subscription.source_group_id),
                ("black_keyword", "关键词黑名单（全局）", subscription.black_keyword),
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
                    value_str = f"`{value}`" if value else "`无`"
                lines.append(f"| {number} | `{key}` | {value_str} | {desc} |")

        lines.append("\n**用法**: `/rssupdate <订阅ID> <序号|参数名> <值>`")
        lines.append(
            f"\n示例: `/rssupdate {sub_id} ① true` 或 `/rssupdate {sub_id} only_title true`"
        )

        event.set_result(MessageEventResult().message("\n".join(lines)))

    async def _handle_mode_c(
        self,
        event: AstrMessageEvent,
        name_or_id: str,
        config_key: str,
        forced_scope: str | None = None,
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

        key_is_personal = config_key in PERSONAL_CONFIG_KEYS
        key_is_global = config_key in GLOBAL_CONFIGURABLE_FIELDS
        is_personal = key_is_personal and forced_scope != "global"
        is_global = key_is_global and forced_scope != "personal"

        if (
            forced_scope == "personal"
            and not key_is_personal
            or forced_scope == "global"
            and not key_is_global
            or not is_personal
            and not is_global
        ):
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

        if is_global and event.role not in ("admin", "superuser"):
            event.set_result(MessageEventResult().message("❌ 全局配置需要管理员权限"))
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
        elif config_key == "white_keyword":
            desc_key = "white_keyword"
        else:
            desc_key = config_key

        number = (
            "⑫"
            if is_global and config_key == "black_keyword"
            else CONFIG_NAME_MAP.get(config_key, "")
        )
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
            await self._send_or_set_result(event, result)
            return

        all_subs = await self.db.get_all_subscriptions()

        if not all_subs:
            result = MessageEventResult().message("📭 No subscriptions yet.")
            await self._send_or_set_result(event, result)
            return

        # Create session for keyboard
        session_id = uuid.uuid4().hex[:8]
        KEYBOARD_SESSIONS[session_id] = {
            "type": "globalconfig",
            "umo": event.unified_msg_origin,
            "user_id": event.get_sender_id(),
            "is_admin": self._is_admin(event),
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
        await self._send_or_set_result(event, result)

    async def _refresh_global_list_keyboard(
        self, event: TelegramCallbackQueryEvent, session_id: str
    ) -> None:
        """Refresh global config subscription list using an existing session."""
        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            result = MessageEventResult()
            result.message("❌ Session expired")
            await self._send_or_set_result(event, result)
            return

        all_subs = await self.db.get_all_subscriptions()
        if not all_subs:
            result = MessageEventResult()
            result.message("📭 No subscriptions yet.")
            await self._send_or_set_result(event, result)
            return

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
        await self._send_or_set_result(event, result)

    async def _show_global_config_keyboard(
        self,
        event: AstrMessageEvent | TelegramCallbackQueryEvent,
        sub_id: int,
        session_id: str | None = None,
        return_to_personal: bool = False,
        status_line: str | None = None,
    ) -> None:
        """Show global config toggle keyboard for a subscription (admin only)."""
        await self.initialize()

        if not self._is_admin(event, session_id):
            msg = "❌ This command requires admin privileges"
            if isinstance(event, TelegramCallbackQueryEvent):
                await event.answer_callback_query(text=msg)
            else:
                event.set_result(MessageEventResult().message(msg))
            return

        if session_id is None and not isinstance(event, TelegramCallbackQueryEvent):
            session_id = uuid.uuid4().hex[:8]
            KEYBOARD_SESSIONS[session_id] = {
                "type": "globalconfig",
                "umo": event.unified_msg_origin,
                "user_id": event.get_sender_id(),
                "is_admin": True,
            }

        subscription = await self.db.get_subscription(sub_id)
        if not subscription:
            msg = f"❌ Subscription ID {sub_id} not found"
            if isinstance(event, TelegramCallbackQueryEvent):
                await event.answer_callback_query(text=msg)
            else:
                event.set_result(MessageEventResult().message(msg))
            return

        buttons = self._build_global_config_buttons(
            sub_id,
            subscription,
            session_id=session_id,
            return_to_personal=return_to_personal,
        )

        result = MessageEventResult()
        status_text = f"{status_line}\n\n" if status_line else ""
        result.message(
            f"{status_text}"
            f"🔧 **{subscription.name}** - [{subscription.url}]({subscription.url}) 全局配置\n\n"
            f"开关项可直接切换，文本/数值项可点按钮后发送新值。"
        )
        result.inline_keyboard(buttons)

        await self._send_or_set_result(event, result)

    async def _show_config_prompt(
        self,
        event: TelegramCallbackQueryEvent,
        session_id: str,
        sub_id: int,
        scope: str,
        config_key: str,
        status_line: str | None = None,
    ) -> None:
        """Prompt for the next Telegram message to edit a config value."""
        subscription = await self.db.get_subscription(sub_id)
        if not subscription:
            await event.answer_callback_query(text="❌ 订阅未找到")
            return

        if scope == "global":
            if not self._is_admin(event, session_id):
                await event.answer_callback_query(text="❌ 全局配置需要管理员权限")
                return
            allowed_keys = {
                "interval",
                "max_image_number",
                "source_group_id",
                "black_keyword",
            }
            if config_key not in allowed_keys:
                await event.answer_callback_query(text="❌ 无效配置项")
                return
            current_value = getattr(subscription, config_key, None)
        elif scope == "personal":
            session = KEYBOARD_SESSIONS.get(session_id)
            if not session:
                await event.answer_callback_query(text="❌ Session expired")
                return
            if config_key not in ("black_keyword", "white_keyword"):
                await event.answer_callback_query(text="❌ 无效配置项")
                return
            subscriber = await self.db.get_subscriber(sub_id, session["umo"])
            if not subscriber:
                await event.answer_callback_query(text="❌ 您未订阅此源")
                return
            personal_config = subscriber.personal_config or {}
            current_value = personal_config.get(config_key, "")
        else:
            await event.answer_callback_query(text="❌ 无效配置项")
            return

        config_names = {
            "black_keyword": "关键词黑名单",
            "white_keyword": "关键词白名单",
            "interval": "抓取间隔（分钟）",
            "max_image_number": "最大图片数",
            "source_group_id": "所属分组ID",
        }
        current_display = self._format_config_value(current_value)
        numeric_keys = {"interval", "max_image_number", "source_group_id"}
        value_hint = (
            "请输入新的数值" if config_key in numeric_keys else "请输入新的文本"
        )

        session = KEYBOARD_SESSIONS.get(session_id)
        if not session:
            await event.answer_callback_query(text="❌ Session expired")
            return
        session["pending_edit"] = {
            "sub_id": sub_id,
            "scope": scope,
            "config_key": config_key,
            "message": event.message,
            "inline_message_id": event.inline_message_id,
        }

        await event.answer_callback_query(text="请直接发送新值")
        result = MessageEventResult()
        status_text = f"{status_line}\n\n" if status_line else ""
        result.message(
            f"{status_text}"
            f"📝 **编辑配置项**\n\n"
            f"订阅: {subscription.name}\n"
            f"配置: {config_names.get(config_key, config_key)}\n"
            f"当前值: {current_display}\n\n"
            f"{value_hint}，发送 `取消` 可返回。"
        )
        result.inline_keyboard(
            [
                [
                    {
                        "text": "取消/返回",
                        "callback_data": f"ruc:{session_id}",
                    }
                ]
            ]
        )
        await self._send_or_set_result(event, result)

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
            if action in ("rus", "rssupdate_select") and len(parts) >= 3:
                session_id = parts[1]
                sub_id = int(parts[2])
                user_id = parts[3] if len(parts) >= 4 else ""
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
                if not self._is_admin(event, session_id):
                    await event.answer_callback_query(text="❌ 全局配置需要管理员权限")
                    return
                sub_id = int(parts[2])
                await self._show_global_config_keyboard(
                    event, sub_id, session_id=session_id
                )
                return

            # Handle rssupdate_global_panel callback (switch from personal to global)
            if action in ("rug", "rssupdate_global_panel") and len(parts) >= 3:
                session_id = parts[1]
                if not self._is_admin(event, session_id):
                    await event.answer_callback_query(text="❌ 全局配置需要管理员权限")
                    return
                sub_id = int(parts[2])
                if not KEYBOARD_SESSIONS.get(session_id):
                    await event.answer_callback_query(text="❌ Session expired")
                    return
                await self._show_global_config_keyboard(
                    event,
                    sub_id,
                    session_id=session_id,
                    return_to_personal=True,
                )
                return

            # Handle rssupdate_personal_panel callback (switch back to personal)
            if action in ("rups", "rssupdate_personal_panel") and len(parts) >= 3:
                session_id = parts[1]
                sub_id = int(parts[2])
                await self._refresh_config_keyboard_mode_b(event, session_id, sub_id)
                return

            # Handle rssupdate_prompt callback (show edit command for text/numeric)
            if action in ("rup", "rssupdate_prompt") and len(parts) >= 5:
                session_id = parts[1]
                sub_id = int(parts[2])
                scope = parts[3]
                config_key = parts[4]
                await self._show_config_prompt(
                    event, session_id, sub_id, scope, config_key
                )
                return

            # Handle pending edit cancel/back callback
            if action == "ruc" and len(parts) >= 2:
                session_id = parts[1]
                session = KEYBOARD_SESSIONS.get(session_id)
                if not session:
                    await event.answer_callback_query(text="❌ Session expired")
                    return
                pending_edit = session.pop("pending_edit", None)
                if not pending_edit:
                    await event.answer_callback_query(text="已返回")
                    return
                await event.answer_callback_query(text="已取消编辑")
                if pending_edit["scope"] == "global":
                    await self._show_global_config_keyboard(
                        event,
                        pending_edit["sub_id"],
                        session_id=session_id if session_id != "0" else None,
                        return_to_personal=bool(
                            session and session.get("type") != "globalconfig"
                        ),
                        status_line="已取消编辑。",
                    )
                else:
                    await self._refresh_config_keyboard_mode_b(
                        event,
                        session_id,
                        pending_edit["sub_id"],
                        status_line="已取消编辑。",
                    )
                return

            # Handle rssupdate_global_toggle callback (Mode B global toggle)
            if action in ("rgt", "rssupdate_global_toggle") and len(parts) >= 4:
                session_id = parts[1]
                if not self._is_admin(event, session_id):
                    await event.answer_callback_query(text="❌ 全局配置需要管理员权限")
                    return
                sub_id = int(parts[2])
                config_key = parts[3]

                if config_key not in ("ai_summary_enabled", "enable_proxy", "stop"):
                    await event.answer_callback_query(text="❌ 无效配置项")
                    return

                subscription = await self.db.get_subscription(sub_id)
                if not subscription:
                    await event.answer_callback_query(text="❌ Subscription not found")
                    return

                current_value = getattr(subscription, config_key, False)
                setattr(subscription, config_key, not current_value)
                await self.db.update_subscription(subscription)
                await self.scheduler.schedule_subscription_fetch(subscription)

                new_value = getattr(subscription, config_key, False)
                status_text = "已开启" if new_value else "已关闭"
                await event.answer_callback_query(text=f"✅ {config_key} {status_text}")
                session = KEYBOARD_SESSIONS.get(session_id)
                await self._show_global_config_keyboard(
                    event,
                    sub_id,
                    session_id=session_id if session_id != "0" else None,
                    return_to_personal=bool(
                        session and session.get("type") != "globalconfig"
                    ),
                )
                return

            # Handle globaltoggle callback (toggle global config)
            if action == "globaltoggle" and len(parts) >= 3:
                if event.role not in ("admin", "superuser"):
                    await event.answer_callback_query(text="❌ 全局配置需要管理员权限")
                    return
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
                session_id = parts[1] if len(parts) >= 2 else None
                if session_id and session_id != "0":
                    if not self._is_admin(event, session_id):
                        await event.answer_callback_query(
                            text="❌ 全局配置需要管理员权限"
                        )
                        return
                    await self._refresh_global_list_keyboard(event, session_id)
                else:
                    await self._show_global_list_keyboard(event)
                return

            # Handle rssupdate_toggle callback (Mode B toggle)
            if action in ("rut", "rssupdate_toggle") and len(parts) >= 4:
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
                    "ai_filter_enabled",
                    "stop",
                ]
                global_config_keys = ["ai_summary_enabled", "enable_proxy", "stop"]

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
                elif config_key in ("black_keyword", "white_keyword"):
                    await self._show_config_prompt(
                        event, session_id, sub_id, "personal", config_key
                    )
                    return
                elif config_key in global_config_keys:
                    if not self._is_admin(event, session_id):
                        await event.answer_callback_query(
                            text="❌ 全局配置需要管理员权限"
                        )
                        return
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
            if action in ("rub", "rssupdate_back") and len(parts) >= 2:
                session_id = parts[1]
                await self._refresh_rssupdate_keyboard(event, session_id)
                return

            # Handle rssupdate_edit callback (edit numeric configs)
            if action == "rssupdate_edit" and len(parts) >= 4:
                session_id = parts[1]
                if not self._is_admin(event, session_id):
                    await event.answer_callback_query(text="❌ 全局配置需要管理员权限")
                    return
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
                await self._send_or_set_result(event, result)
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
            await self._send_or_set_result(event, result)
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
            await self._send_or_set_result(event, result)
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
        await self._send_or_set_result(event, result)
