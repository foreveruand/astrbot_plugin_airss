"""
Database operations for the RSS plugin using async SQLite.
"""

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    RSSArticle,
    RSSGroup,
    RSSSubscription,
    Subscriber,
)

logger = logging.getLogger("astrbot")


class Database:
    """Async SQLite database for RSS plugin."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    async def init_db(self) -> None:
        """Initialize database tables."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    interval INTEGER DEFAULT 5,
                    source_group_id INTEGER DEFAULT NULL,
                    cookies TEXT DEFAULT NULL,
                    black_keyword TEXT DEFAULT NULL,
                    content_to_remove TEXT DEFAULT NULL,
                    max_image_number INTEGER DEFAULT 0,
                    ai_summary_enabled INTEGER DEFAULT 1,
                    enable_proxy INTEGER DEFAULT 0,
                    stop INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_fetch TEXT DEFAULT NULL,
                    etag TEXT DEFAULT NULL,
                    last_modified TEXT DEFAULT NULL
                );

                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    title TEXT,
                    content TEXT,
                    link TEXT,
                    guid TEXT,
                    author TEXT DEFAULT '',
                    published_at TEXT,
                    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    is_sent INTEGER DEFAULT 0,
                    image_urls TEXT,
                    content_hash TEXT,
                    UNIQUE(subscription_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    schedules TEXT DEFAULT '[]',
                    persona_id TEXT
                );

                CREATE TABLE IF NOT EXISTS subscribers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    umo TEXT NOT NULL,
                    personal_config TEXT DEFAULT '{}',
                    UNIQUE(subscription_id, umo)
                );

                CREATE TABLE IF NOT EXISTS article_sent (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    subscriber_id INTEGER NOT NULL,
                    sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(article_id, subscriber_id)
                );

                CREATE INDEX IF NOT EXISTS idx_articles_subscription ON articles(subscription_id);
                CREATE INDEX IF NOT EXISTS idx_articles_sent ON articles(is_sent);
                CREATE INDEX IF NOT EXISTS idx_subscribers_subscription ON subscribers(subscription_id);
                CREATE INDEX IF NOT EXISTS idx_article_sent_article ON article_sent(article_id);
                CREATE INDEX IF NOT EXISTS idx_article_sent_subscriber ON article_sent(subscriber_id);
            """)
            await conn.commit()

        # Run migrations for existing databases
        await self._migrate_db()

        logger.info(f"RSS database initialized at {self.db_path}")

    @staticmethod
    def _hash_content(guid: str, link: str) -> str:
        """Generate hash for article deduplication."""
        return hashlib.md5(f"{guid}:{link}".encode()).hexdigest()

    async def _migrate_db(self) -> None:
        """Migrate database schema for compatibility with old versions."""
        async with aiosqlite.connect(self.db_path) as conn:
            # Check if articles table has author column
            cursor = await conn.execute(
                "SELECT name FROM pragma_table_info('articles') WHERE name = 'author'"
            )
            row = await cursor.fetchone()
            if not row:
                logger.info(
                    "Migrating database: adding author column to articles table"
                )
                await conn.execute(
                    "ALTER TABLE articles ADD COLUMN author TEXT DEFAULT ''"
                )
                await conn.commit()
                logger.info("Database migration completed: author column added")

    # ==================== Subscription Operations ====================

    async def add_subscription(self, sub: RSSSubscription) -> int:
        """Add a new subscription. Returns the subscription ID."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                """
                INSERT INTO subscriptions (
                    name, url, interval, source_group_id, cookies, black_keyword,
                    content_to_remove, max_image_number, ai_summary_enabled, enable_proxy, stop
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sub.name,
                    sub.url,
                    sub.interval,
                    sub.source_group_id,
                    sub.cookies,
                    sub.black_keyword,
                    sub.content_to_remove,
                    sub.max_image_number,
                    1 if sub.ai_summary_enabled else 0,
                    1 if sub.enable_proxy else 0,
                    1 if sub.stop else 0,
                ),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def get_subscription(self, sub_id: int) -> RSSSubscription | None:
        """Get subscription by ID."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
            )
            row = await cursor.fetchone()
            if row:
                return RSSSubscription(
                    id=row["id"],
                    name=row["name"],
                    url=row["url"],
                    interval=row["interval"],
                    source_group_id=row["source_group_id"],
                    cookies=row["cookies"],
                    black_keyword=row["black_keyword"],
                    content_to_remove=row["content_to_remove"],
                    max_image_number=row["max_image_number"] or 0,
                    ai_summary_enabled=bool(
                        row["ai_summary_enabled"]
                        if row["ai_summary_enabled"] is not None
                        else True
                    ),
                    enable_proxy=bool(row["enable_proxy"]),
                    stop=bool(row["stop"]) if row["stop"] is not None else False,
                    error_count=row["error_count"]
                    if row["error_count"] is not None
                    else 0,
                    last_fetch=(
                        datetime.fromisoformat(row["last_fetch"])
                        if row["last_fetch"]
                        else None
                    ),
                    etag=row["etag"],
                    last_modified=row["last_modified"],
                )
            return None

    async def get_subscription_by_url(self, url: str) -> RSSSubscription | None:
        """Get subscription by URL."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM subscriptions WHERE url = ?", (url,)
            )
            row = await cursor.fetchone()
            if row:
                return RSSSubscription(
                    id=row["id"],
                    name=row["name"],
                    url=row["url"],
                    interval=row["interval"],
                    source_group_id=row["source_group_id"],
                    cookies=row["cookies"],
                    black_keyword=row["black_keyword"],
                    content_to_remove=row["content_to_remove"],
                    max_image_number=row["max_image_number"] or 0,
                    ai_summary_enabled=bool(
                        row["ai_summary_enabled"]
                        if row["ai_summary_enabled"] is not None
                        else True
                    ),
                    enable_proxy=bool(row["enable_proxy"]),
                    stop=bool(row["stop"]) if row["stop"] is not None else False,
                    error_count=row["error_count"]
                    if row["error_count"] is not None
                    else 0,
                    last_fetch=(
                        datetime.fromisoformat(row["last_fetch"])
                        if row["last_fetch"]
                        else None
                    ),
                    etag=row["etag"],
                    last_modified=row["last_modified"],
                )
            return None

    async def get_all_subscriptions(self) -> list[RSSSubscription]:
        """Get all subscriptions."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM subscriptions")
            rows = await cursor.fetchall()
            return [
                RSSSubscription(
                    id=row["id"],
                    name=row["name"],
                    url=row["url"],
                    interval=row["interval"],
                    source_group_id=row["source_group_id"],
                    cookies=row["cookies"],
                    black_keyword=row["black_keyword"],
                    content_to_remove=row["content_to_remove"],
                    max_image_number=row["max_image_number"] or 0,
                    ai_summary_enabled=bool(
                        row["ai_summary_enabled"]
                        if row["ai_summary_enabled"] is not None
                        else True
                    ),
                    enable_proxy=bool(row["enable_proxy"]),
                    stop=bool(row["stop"]) if row["stop"] is not None else False,
                    error_count=row["error_count"]
                    if row["error_count"] is not None
                    else 0,
                    last_fetch=(
                        datetime.fromisoformat(row["last_fetch"])
                        if row["last_fetch"]
                        else None
                    ),
                    etag=row["etag"],
                    last_modified=row["last_modified"],
                )
                for row in rows
            ]

    async def update_subscription(self, sub: RSSSubscription) -> None:
        """Update subscription."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                UPDATE subscriptions SET
                    name = ?, url = ?, interval = ?, source_group_id = ?,
                    cookies = ?, black_keyword = ?, content_to_remove = ?,
                    max_image_number = ?, ai_summary_enabled = ?, enable_proxy = ?, stop = ?,
                    error_count = ?, last_fetch = ?, etag = ?, last_modified = ?
                WHERE id = ?
                """,
                (
                    sub.name,
                    sub.url,
                    sub.interval,
                    sub.source_group_id,
                    sub.cookies,
                    sub.black_keyword,
                    sub.content_to_remove,
                    sub.max_image_number,
                    1 if sub.ai_summary_enabled else 0,
                    1 if sub.enable_proxy else 0,
                    1 if sub.stop else 0,
                    sub.error_count,
                    sub.last_fetch.isoformat() if sub.last_fetch else None,
                    sub.etag,
                    sub.last_modified,
                    sub.id,
                ),
            )
            await conn.commit()

    async def delete_subscription(self, sub_id: int) -> None:
        """Delete subscription and related data."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "DELETE FROM subscribers WHERE subscription_id = ?", (sub_id,)
            )
            await conn.execute(
                "DELETE FROM articles WHERE subscription_id = ?", (sub_id,)
            )
            await conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            await conn.commit()

    # ==================== Article Operations ====================

    async def add_article(self, article: RSSArticle) -> int | None:
        """Add article if not exists. Returns article ID or None if duplicate."""
        content_hash = self._hash_content(article.guid, article.link)
        async with aiosqlite.connect(self.db_path) as conn:
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO articles
                    (subscription_id, title, content, link, guid, author, published_at, fetched_at, is_sent, image_urls, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article.subscription_id,
                        article.title,
                        article.content,
                        article.link,
                        article.guid,
                        article.author,
                        article.published_at.isoformat()
                        if article.published_at
                        else None,
                        article.fetched_at.isoformat(),
                        1 if article.is_sent else 0,
                        "|||".join(article.image_urls),
                        content_hash,
                    ),
                )
                await conn.commit()
                return cursor.lastrowid
            except aiosqlite.IntegrityError:
                # Duplicate article
                return None

    async def article_exists(self, subscription_id: int, guid: str, link: str) -> bool:
        """Check if article already exists."""
        content_hash = self._hash_content(guid, link)
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM articles WHERE subscription_id = ? AND content_hash = ?",
                (subscription_id, content_hash),
            )
            return await cursor.fetchone() is not None

    async def get_unsent_articles(self, subscription_id: int) -> list[RSSArticle]:
        """Get unsent articles for a subscription."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT * FROM articles
                WHERE subscription_id = ? AND is_sent = 0
                ORDER BY published_at DESC, fetched_at DESC
                """,
                (subscription_id,),
            )
            rows = await cursor.fetchall()
            return [
                RSSArticle(
                    id=row["id"],
                    subscription_id=row["subscription_id"],
                    title=row["title"],
                    content=row["content"],
                    link=row["link"],
                    guid=row["guid"],
                    author=row["author"] or "",
                    published_at=(
                        datetime.fromisoformat(row["published_at"])
                        if row["published_at"]
                        else None
                    ),
                    fetched_at=datetime.fromisoformat(row["fetched_at"]),
                    is_sent=bool(row["is_sent"]),
                    image_urls=row["image_urls"].split("|||")
                    if row["image_urls"]
                    else [],
                )
                for row in rows
            ]

    async def mark_articles_sent(self, article_ids: list[int]) -> None:
        """Mark articles as sent."""
        if not article_ids:
            return
        async with aiosqlite.connect(self.db_path) as conn:
            placeholders = ",".join("?" * len(article_ids))
            await conn.execute(
                f"UPDATE articles SET is_sent = 1 WHERE id IN ({placeholders})",
                article_ids,
            )
            await conn.commit()

    # ==================== Article Sent Tracking (Per-Subscriber) ====================

    async def mark_articles_sent_to_subscriber(
        self, subscriber_id: int, article_ids: list[int]
    ) -> None:
        """Mark articles as sent to a specific subscriber."""
        if not article_ids:
            return
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executemany(
                "INSERT OR IGNORE INTO article_sent (article_id, subscriber_id) VALUES (?, ?)",
                [(article_id, subscriber_id) for article_id in article_ids],
            )
            await conn.commit()

    async def get_unsent_articles_for_subscriber(
        self, subscription_id: int, subscriber_id: int
    ) -> list[RSSArticle]:
        """Get articles not yet sent to a specific subscriber."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT a.* FROM articles a
                WHERE a.subscription_id = ?
                AND a.id NOT IN (
                    SELECT article_id FROM article_sent WHERE subscriber_id = ?
                )
                ORDER BY a.published_at DESC, a.fetched_at DESC
                """,
                (subscription_id, subscriber_id),
            )
            rows = await cursor.fetchall()
            return [
                RSSArticle(
                    id=row["id"],
                    subscription_id=row["subscription_id"],
                    title=row["title"],
                    content=row["content"],
                    link=row["link"],
                    guid=row["guid"],
                    author=row["author"] or "",
                    published_at=(
                        datetime.fromisoformat(row["published_at"])
                        if row["published_at"]
                        else None
                    ),
                    fetched_at=datetime.fromisoformat(row["fetched_at"]),
                    is_sent=bool(row["is_sent"]),
                    image_urls=row["image_urls"].split("|||")
                    if row["image_urls"]
                    else [],
                )
                for row in rows
            ]

    async def mark_all_articles_sent_to_subscriber(
        self, subscription_id: int, subscriber_id: int
    ) -> None:
        """Mark all existing articles for a subscription as sent to a subscriber.

        Used when adding a new subscriber to prevent sending old articles.
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO article_sent (article_id, subscriber_id)
                SELECT id, ? FROM articles WHERE subscription_id = ?
                """,
                (subscriber_id, subscription_id),
            )
            await conn.commit()

    # ==================== Group Operations ====================

    async def add_group(self, group: RSSGroup) -> int:
        """Add a new group. Returns the group ID."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "INSERT INTO groups (name, schedules, persona_id) VALUES (?, ?, ?)",
                (group.name, "[]", group.persona_id),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def get_group(self, group_id: int) -> RSSGroup | None:
        """Get group by ID."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM groups WHERE id = ?", (group_id,)
            )
            row = await cursor.fetchone()
            if row:
                import json

                return RSSGroup(
                    id=row["id"],
                    name=row["name"],
                    schedules=json.loads(row["schedules"]),
                    persona_id=row["persona_id"],
                )
            return None

    async def get_all_groups(self) -> list[RSSGroup]:
        """Get all groups."""
        async with aiosqlite.connect(self.db_path) as conn:
            import json

            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM groups")
            rows = await cursor.fetchall()
            return [
                RSSGroup(
                    id=row["id"],
                    name=row["name"],
                    schedules=json.loads(row["schedules"]),
                    persona_id=row["persona_id"],
                )
                for row in rows
            ]

    async def update_group(self, group: RSSGroup) -> None:
        """Update group."""
        async with aiosqlite.connect(self.db_path) as conn:
            import json

            await conn.execute(
                "UPDATE groups SET name = ?, schedules = ?, persona_id = ? WHERE id = ?",
                (
                    group.name,
                    json.dumps(group.schedules),
                    group.persona_id,
                    group.id,
                ),
            )
            await conn.commit()

    async def delete_group(self, group_id: int) -> None:
        """Delete group."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            await conn.commit()

    # ==================== Subscriber Operations ====================

    async def add_subscriber(self, subscriber: Subscriber) -> int | None:
        """Add subscriber to a subscription. Returns subscriber ID or None if exists.

        Also marks all existing articles as sent to prevent sending old articles.
        """
        async with aiosqlite.connect(self.db_path) as conn:
            import json

            try:
                cursor = await conn.execute(
                    "INSERT INTO subscribers (subscription_id, umo, personal_config) VALUES (?, ?, ?)",
                    (
                        subscriber.subscription_id,
                        subscriber.umo,
                        json.dumps(subscriber.personal_config),
                    ),
                )
                await conn.commit()
                subscriber_id = cursor.lastrowid
                if subscriber_id:
                    await self.mark_all_articles_sent_to_subscriber(
                        subscriber.subscription_id, subscriber_id
                    )
                return subscriber_id
            except aiosqlite.IntegrityError:
                return None

    async def get_subscribers(self, subscription_id: int) -> list[Subscriber]:
        """Get all subscribers for a subscription."""
        async with aiosqlite.connect(self.db_path) as conn:
            import json

            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM subscribers WHERE subscription_id = ?",
                (subscription_id,),
            )
            rows = await cursor.fetchall()
            return [
                Subscriber(
                    id=row["id"],
                    subscription_id=row["subscription_id"],
                    umo=row["umo"],
                    personal_config=json.loads(row["personal_config"])
                    if row["personal_config"] is not None
                    else {},
                )
                for row in rows
            ]

    async def get_subscriber(self, subscription_id: int, umo: str) -> Subscriber | None:
        """Get specific subscriber."""
        async with aiosqlite.connect(self.db_path) as conn:
            import json

            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM subscribers WHERE subscription_id = ? AND umo = ?",
                (subscription_id, umo),
            )
            row = await cursor.fetchone()
            if row:
                return Subscriber(
                    id=row["id"],
                    subscription_id=row["subscription_id"],
                    umo=row["umo"],
                    personal_config=json.loads(row["personal_config"])
                    if row["personal_config"] is not None
                    else {},
                )
            return None

    async def update_subscriber(self, subscriber: Subscriber) -> None:
        """Update subscriber config."""
        async with aiosqlite.connect(self.db_path) as conn:
            import json

            await conn.execute(
                "UPDATE subscribers SET personal_config = ? WHERE id = ?",
                (json.dumps(subscriber.personal_config), subscriber.id),
            )
            await conn.commit()

    async def delete_subscriber(self, subscription_id: int, umo: str) -> None:
        """Delete subscriber."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "DELETE FROM subscribers WHERE subscription_id = ? AND umo = ?",
                (subscription_id, umo),
            )
            await conn.commit()

    async def get_subscriptions_by_group(self, group_id: int) -> list[RSSSubscription]:
        """Get all subscriptions in a group."""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM subscriptions WHERE source_group_id = ?", (group_id,)
            )
            rows = await cursor.fetchall()
            return [
                RSSSubscription(
                    id=row["id"],
                    name=row["name"],
                    url=row["url"],
                    interval=row["interval"],
                    source_group_id=row["source_group_id"],
                    cookies=row["cookies"],
                    black_keyword=row["black_keyword"],
                    content_to_remove=row["content_to_remove"],
                    max_image_number=row["max_image_number"] or 0,
                    ai_summary_enabled=bool(
                        row["ai_summary_enabled"]
                        if row["ai_summary_enabled"] is not None
                        else True
                    ),
                    enable_proxy=bool(row["enable_proxy"]),
                    stop=bool(row["stop"]) if row["stop"] is not None else False,
                    error_count=row["error_count"]
                    if row["error_count"] is not None
                    else 0,
                    last_fetch=(
                        datetime.fromisoformat(row["last_fetch"])
                        if row["last_fetch"]
                        else None
                    ),
                    etag=row["etag"],
                    last_modified=row["last_modified"],
                )
                for row in rows
            ]

    async def update_subscription_global_config(
        self, subscription_id: int, key: str, value: Any
    ) -> None:
        """Update a single global config field for a subscription.

        Args:
            subscription_id: The subscription ID
            key: The field name to update
            value: The new value
        """
        from .models import GLOBAL_CONFIGURABLE_FIELDS

        if key not in GLOBAL_CONFIGURABLE_FIELDS:
            raise ValueError(f"Field '{key}' is not configurable")

        async with aiosqlite.connect(self.db_path) as conn:
            # Convert bool to int for SQLite
            if isinstance(value, bool):
                value = 1 if value else 0
            await conn.execute(
                f"UPDATE subscriptions SET {key} = ? WHERE id = ?",
                (value, subscription_id),
            )
            await conn.commit()

    async def get_subscribers_by_subscription(
        self, subscription_id: int
    ) -> list[Subscriber]:
        """Get all subscribers for a subscription. Alias for get_subscribers."""
        return await self.get_subscribers(subscription_id)

    # ==================== Cleanup Operations ====================

    async def cleanup_old_articles(self, retention_days: int = 30) -> int:
        """Delete articles older than retention_days.

        Args:
            retention_days: Number of days to retain articles.

        Returns:
            Count of deleted articles.
        """
        async with aiosqlite.connect(self.db_path) as conn:
            # First clean up orphaned article_sent rows for articles that will be deleted
            await conn.execute(
                """
                DELETE FROM article_sent
                WHERE article_id IN (
                    SELECT id FROM articles
                    WHERE datetime(fetched_at) < datetime('now', ? || ' days')
                )
                """,
                (f"-{retention_days}",),
            )
            cursor = await conn.execute(
                """
                DELETE FROM articles
                WHERE datetime(fetched_at) < datetime('now', ? || ' days')
                """,
                (f"-{retention_days}",),
            )
            await conn.commit()
            return cursor.rowcount
