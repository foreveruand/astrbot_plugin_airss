"""
Database operations for the RSS plugin using async SQLite.
"""

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from .models import (
    PERSONAL_CONFIG_KEYS,
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
        self._conn: Optional[aiosqlite.Connection] = None

    async def init_db(self) -> None:
        """Initialize database tables."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    interval INTEGER DEFAULT 5,
                    group_id INTEGER,
                    ai_enabled INTEGER DEFAULT 1,
                    error_count INTEGER DEFAULT 0,
                    last_fetch TEXT,
                    etag TEXT,
                    last_modified TEXT,
                    cookies TEXT,
                    black_keyword TEXT
                );

                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    title TEXT,
                    content TEXT,
                    link TEXT,
                    guid TEXT,
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

                CREATE INDEX IF NOT EXISTS idx_articles_subscription ON articles(subscription_id);
                CREATE INDEX IF NOT EXISTS idx_articles_sent ON articles(is_sent);
                CREATE INDEX IF NOT EXISTS idx_subscribers_subscription ON subscribers(subscription_id);
            """)
            await conn.commit()
        logger.info(f"RSS database initialized at {self.db_path}")

    @staticmethod
    def _hash_content(guid: str, link: str) -> str:
        """Generate hash for article deduplication."""
        return hashlib.md5(f"{guid}:{link}".encode()).hexdigest()

    # ==================== Subscription Operations ====================

    async def add_subscription(self, sub: RSSSubscription) -> int:
        """Add a new subscription. Returns the subscription ID."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                """
                INSERT INTO subscriptions (name, url, interval, group_id, ai_enabled, cookies, black_keyword)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sub.name,
                    sub.url,
                    sub.interval,
                    sub.group_id,
                    1 if sub.ai_enabled else 0,
                    sub.cookies,
                    sub.black_keyword,
                ),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def get_subscription(self, sub_id: int) -> Optional[RSSSubscription]:
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
                    group_id=row["group_id"],
                    ai_enabled=bool(row["ai_enabled"]),
                    error_count=row["error_count"],
                    last_fetch=(
                        datetime.fromisoformat(row["last_fetch"])
                        if row["last_fetch"]
                        else None
                    ),
                    etag=row["etag"],
                    last_modified=row["last_modified"],
                    cookies=row["cookies"],
                    black_keyword=row["black_keyword"],
                )
            return None

    async def get_subscription_by_url(self, url: str) -> Optional[RSSSubscription]:
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
                    group_id=row["group_id"],
                    ai_enabled=bool(row["ai_enabled"]),
                    error_count=row["error_count"],
                    last_fetch=(
                        datetime.fromisoformat(row["last_fetch"])
                        if row["last_fetch"]
                        else None
                    ),
                    etag=row["etag"],
                    last_modified=row["last_modified"],
                    cookies=row["cookies"],
                    black_keyword=row["black_keyword"],
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
                    group_id=row["group_id"],
                    ai_enabled=bool(row["ai_enabled"]),
                    error_count=row["error_count"],
                    last_fetch=(
                        datetime.fromisoformat(row["last_fetch"])
                        if row["last_fetch"]
                        else None
                    ),
                    etag=row["etag"],
                    last_modified=row["last_modified"],
                    cookies=row["cookies"],
                    black_keyword=row["black_keyword"],
                )
                for row in rows
            ]

    async def update_subscription(self, sub: RSSSubscription) -> None:
        """Update subscription."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                UPDATE subscriptions SET
                    name = ?, url = ?, interval = ?, group_id = ?,
                    ai_enabled = ?, error_count = ?, last_fetch = ?,
                    etag = ?, last_modified = ?, cookies = ?, black_keyword = ?
                WHERE id = ?
                """,
                (
                    sub.name,
                    sub.url,
                    sub.interval,
                    sub.group_id,
                    1 if sub.ai_enabled else 0,
                    sub.error_count,
                    sub.last_fetch.isoformat() if sub.last_fetch else None,
                    sub.etag,
                    sub.last_modified,
                    sub.cookies,
                    sub.black_keyword,
                    sub.id,
                ),
            )
            await conn.commit()

    async def delete_subscription(self, sub_id: int) -> None:
        """Delete subscription and related data."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("DELETE FROM subscribers WHERE subscription_id = ?", (sub_id,))
            await conn.execute("DELETE FROM articles WHERE subscription_id = ?", (sub_id,))
            await conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            await conn.commit()

    # ==================== Article Operations ====================

    async def add_article(self, article: RSSArticle) -> Optional[int]:
        """Add article if not exists. Returns article ID or None if duplicate."""
        content_hash = self._hash_content(article.guid, article.link)
        async with aiosqlite.connect(self.db_path) as conn:
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO articles
                    (subscription_id, title, content, link, guid, published_at, fetched_at, is_sent, image_urls, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article.subscription_id,
                        article.title,
                        article.content,
                        article.link,
                        article.guid,
                        article.published_at.isoformat() if article.published_at else None,
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
                    published_at=(
                        datetime.fromisoformat(row["published_at"])
                        if row["published_at"]
                        else None
                    ),
                    fetched_at=datetime.fromisoformat(row["fetched_at"]),
                    is_sent=bool(row["is_sent"]),
                    image_urls=row["image_urls"].split("|||") if row["image_urls"] else [],
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

    async def get_group(self, group_id: int) -> Optional[RSSGroup]:
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

    async def add_subscriber(self, subscriber: Subscriber) -> Optional[int]:
        """Add subscriber to a subscription. Returns subscriber ID or None if exists."""
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
                return cursor.lastrowid
            except aiosqlite.IntegrityError:
                return None

    async def get_subscribers(self, subscription_id: int) -> list[Subscriber]:
        """Get all subscribers for a subscription."""
        async with aiosqlite.connect(self.db_path) as conn:
            import json

            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM subscribers WHERE subscription_id = ?", (subscription_id,)
            )
            rows = await cursor.fetchall()
            return [
                Subscriber(
                    id=row["id"],
                    subscription_id=row["subscription_id"],
                    umo=row["umo"],
                    personal_config=json.loads(row["personal_config"]),
                )
                for row in rows
            ]

    async def get_subscriber(self, subscription_id: int, umo: str) -> Optional[Subscriber]:
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
                    personal_config=json.loads(row["personal_config"]),
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
                "SELECT * FROM subscriptions WHERE group_id = ?", (group_id,)
            )
            rows = await cursor.fetchall()
            return [
                RSSSubscription(
                    id=row["id"],
                    name=row["name"],
                    url=row["url"],
                    interval=row["interval"],
                    group_id=row["group_id"],
                    ai_enabled=bool(row["ai_enabled"]),
                    error_count=row["error_count"],
                    last_fetch=(
                        datetime.fromisoformat(row["last_fetch"])
                        if row["last_fetch"]
                        else None
                    ),
                    etag=row["etag"],
                    last_modified=row["last_modified"],
                    cookies=row["cookies"],
                    black_keyword=row["black_keyword"],
                )
                for row in rows
            ]