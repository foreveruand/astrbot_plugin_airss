from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from astrbot_plugin_airss.commands import GroupCommands
from astrbot_plugin_airss.database import Database
from astrbot_plugin_airss.main import KEYBOARD_SESSIONS, Main
from astrbot_plugin_airss.models import (
    RSSArticle,
    RSSGroup,
    RSSSubscription,
    Subscriber,
)
from astrbot_plugin_airss.persona_utils import ensure_group_persona_for_group
from astrbot_plugin_airss.scheduler import RSSScheduler


@pytest.mark.asyncio
async def test_ensure_group_persona_creates_missing_persona():
    context = MagicMock()
    context.persona_manager.get_persona = AsyncMock(side_effect=ValueError("missing"))
    context.persona_manager.create_persona = AsyncMock()
    db = MagicMock()
    db.update_group = AsyncMock()
    group = RSSGroup(id=7, name="news", persona_id=None)

    persona_id = await ensure_group_persona_for_group(context, db, group)

    assert persona_id == "rss_group_7"
    assert group.persona_id == "rss_group_7"
    context.persona_manager.create_persona.assert_awaited_once()
    create_call = context.persona_manager.create_persona.await_args.kwargs
    assert create_call["persona_id"] == "rss_group_7"
    assert create_call["tools"] is None
    assert create_call["skills"] is None


@pytest.mark.asyncio
async def test_ensure_group_persona_does_not_overwrite_existing():
    context = MagicMock()
    existing_persona = MagicMock()
    context.persona_manager.get_persona = AsyncMock(return_value=existing_persona)
    context.persona_manager.create_persona = AsyncMock()
    db = MagicMock()
    db.update_group = AsyncMock()
    group = RSSGroup(id=3, name="tech", persona_id="rss_group_3")

    persona_id = await ensure_group_persona_for_group(context, db, group)

    assert persona_id == "rss_group_3"
    context.persona_manager.create_persona.assert_not_called()


def _make_article(article_id: int, subscription_id: int) -> RSSArticle:
    return RSSArticle(
        id=article_id,
        subscription_id=subscription_id,
        title=f"title-{article_id}",
        content=f"content-{article_id}",
        link=f"https://example.com/{article_id}",
        guid=str(article_id),
    )


def test_command_parsing_preserves_argument_text():
    main = Main.__new__(Main)

    assert main._strip_command("/rssdel Tech rssdel Feed", "rssdel") == (
        "Tech rssdel Feed"
    )
    assert main._strip_command("/rssadd https://example.com/rss My Feed", "rssadd") == (
        "https://example.com/rss My Feed"
    )


def test_update_config_number_scope_distinguishes_global_black_keyword():
    assert Main._resolve_update_config_key("⑥") == ("black_keyword", "personal")
    assert Main._resolve_update_config_key("⑫") == ("black_keyword", "global")
    assert Main._resolve_update_config_key("⑬") == ("white_keyword", "personal")
    assert Main._resolve_update_config_key("⑭") == ("ai_filter_enabled", "personal")
    assert Main._resolve_update_config_key("black_keyword") == ("black_keyword", None)


def test_keyboard_callback_data_stays_within_telegram_limit():
    main = Main.__new__(Main)
    subscription = RSSSubscription(
        id=12345,
        interval=10,
        max_image_number=3,
        source_group_id=2,
    )

    personal_buttons = main._build_rssupdate_config_buttons(
        "abcdef12",
        12345,
        subscription,
        {"only_title": True},
        is_admin=True,
    )
    global_buttons = main._build_global_config_buttons(
        12345,
        subscription,
        session_id="abcdef12",
        return_to_personal=True,
    )
    callback_data = [
        button["callback_data"]
        for row in personal_buttons + global_buttons
        for button in row
    ]

    assert all(len(data.encode("utf-8")) <= 64 for data in callback_data)


def test_callback_admin_permission_can_fall_back_to_session_creator():
    main = Main.__new__(Main)
    event = MagicMock()
    event.role = "member"
    event.get_sender_id.return_value = "42"
    KEYBOARD_SESSIONS["adminsess"] = {
        "is_admin": True,
        "user_id": "42",
    }

    try:
        assert main._is_admin(event, "adminsess")
    finally:
        KEYBOARD_SESSIONS.pop("adminsess", None)


@pytest.mark.asyncio
async def test_filter_articles_for_subscriber_applies_personal_white_keyword():
    scheduler = RSSScheduler(MagicMock(), MagicMock(), MagicMock(), {})
    subscription = RSSSubscription(id=1)
    subscriber = Subscriber(
        id=11,
        subscription_id=1,
        umo="u1",
        personal_config={"white_keyword": "keep"},
    )
    hidden = _make_article(1, 1)
    shown = _make_article(2, 1)
    shown.content = "please keep this"

    articles, skipped_article_ids = await scheduler._filter_articles_for_subscriber(
        [hidden, shown], subscriber, subscription
    )

    assert [article.id for article in articles] == [2]
    assert skipped_article_ids == [1]


@pytest.mark.asyncio
async def test_filter_articles_for_subscriber_prefers_black_keyword_over_white_keyword():
    scheduler = RSSScheduler(MagicMock(), MagicMock(), MagicMock(), {})
    subscription = RSSSubscription(id=1)
    subscriber = Subscriber(
        id=11,
        subscription_id=1,
        umo="u1",
        personal_config={"black_keyword": "block", "white_keyword": "keep"},
    )
    conflicted = _make_article(1, 1)
    conflicted.title = "keep but block"

    articles, skipped_article_ids = await scheduler._filter_articles_for_subscriber(
        [conflicted], subscriber, subscription
    )

    assert articles == []
    assert skipped_article_ids == [1]


@pytest.mark.asyncio
async def test_filter_articles_for_subscriber_marks_ai_duplicate_skipped():
    context = MagicMock()
    response = MagicMock()
    response.completion_text = '[{"id": 1, "duplicate": true}]'
    context.llm_generate = AsyncMock(return_value=response)
    db = MagicMock()
    db.get_article_ai_filter_results = AsyncMock(return_value={})
    db.get_recent_ai_filter_candidates = AsyncMock(
        return_value=[(99, "same event elsewhere")]
    )
    db.set_article_ai_filter_results = AsyncMock()
    scheduler = RSSScheduler(
        context,
        db,
        MagicMock(),
        {"ai_config": {"ai_filter_provider": "filter-provider"}},
    )
    subscription = RSSSubscription(id=1)
    subscriber = Subscriber(
        id=11,
        subscription_id=1,
        umo="u1",
        personal_config={"ai_filter_enabled": True},
    )
    article = _make_article(1, 1)

    articles, skipped_article_ids = await scheduler._filter_articles_for_subscriber(
        [article], subscriber, subscription
    )

    assert articles == []
    assert skipped_article_ids == [1]
    db.get_recent_ai_filter_candidates.assert_awaited_once_with(
        30, exclude_article_ids=[1]
    )
    db.set_article_ai_filter_results.assert_awaited_once_with({1: True})
    context.llm_generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_filter_articles_for_subscriber_keeps_article_when_ai_filter_fails():
    context = MagicMock()
    context.llm_generate = AsyncMock(side_effect=RuntimeError("provider failed"))
    db = MagicMock()
    db.get_article_ai_filter_results = AsyncMock(return_value={})
    db.get_recent_ai_filter_candidates = AsyncMock(
        return_value=[(99, "similar candidate")]
    )
    db.set_article_ai_filter_results = AsyncMock()
    scheduler = RSSScheduler(
        context,
        db,
        MagicMock(),
        {"ai_config": {"ai_filter_provider": "filter-provider"}},
    )
    subscription = RSSSubscription(id=1)
    subscriber = Subscriber(
        id=11,
        subscription_id=1,
        umo="u1",
        personal_config={"ai_filter_enabled": True},
    )
    article = _make_article(1, 1)

    articles, skipped_article_ids = await scheduler._filter_articles_for_subscriber(
        [article], subscriber, subscription
    )

    assert articles == [article]
    assert skipped_article_ids == []
    db.set_article_ai_filter_results.assert_awaited_once_with({1: False})


@pytest.mark.asyncio
async def test_filter_articles_for_subscriber_skips_ai_after_keyword_filtering():
    context = MagicMock()
    context.llm_generate = AsyncMock()
    db = MagicMock()
    scheduler = RSSScheduler(
        context,
        db,
        MagicMock(),
        {"ai_config": {"ai_filter_provider": "filter-provider"}},
    )
    subscription = RSSSubscription(id=1)
    subscriber = Subscriber(
        id=11,
        subscription_id=1,
        umo="u1",
        personal_config={"black_keyword": "blocked", "ai_filter_enabled": True},
    )
    article = _make_article(1, 1)
    article.title = "blocked article"

    articles, skipped_article_ids = await scheduler._filter_articles_for_subscriber(
        [article], subscriber, subscription
    )

    assert articles == []
    assert skipped_article_ids == [1]
    context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_filter_articles_for_subscriber_reuses_article_ai_result():
    context = MagicMock()
    response = MagicMock()
    response.completion_text = '[{"id": 1, "duplicate": true}]'
    context.llm_generate = AsyncMock(return_value=response)
    db = MagicMock()
    db.get_article_ai_filter_results = AsyncMock(side_effect=[{}, {1: True}])
    db.get_recent_ai_filter_candidates = AsyncMock(return_value=[(99, "same event")])
    db.set_article_ai_filter_results = AsyncMock()
    scheduler = RSSScheduler(
        context,
        db,
        MagicMock(),
        {"ai_config": {"ai_filter_provider": "filter-provider"}},
    )
    subscription = RSSSubscription(id=1)
    subscribers = [
        Subscriber(
            id=11,
            subscription_id=1,
            umo="u1",
            personal_config={"ai_filter_enabled": True},
        ),
        Subscriber(
            id=12,
            subscription_id=1,
            umo="u2",
            personal_config={"ai_filter_enabled": True},
        ),
    ]
    article = _make_article(1, 1)

    for subscriber in subscribers:
        articles, skipped_article_ids = await scheduler._filter_articles_for_subscriber(
            [article], subscriber, subscription
        )
        assert articles == []
        assert skipped_article_ids == [1]

    context.llm_generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_database_migrates_existing_articles_for_ai_filter(tmp_path):
    db_path = tmp_path / "rss.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE articles (
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
        )
        """
    )
    connection.commit()
    connection.close()

    db = Database(db_path)
    await db.init_db()
    try:
        async with db._acquire() as connection:
            cursor = await connection.execute("PRAGMA table_info(articles)")
            columns = {row[1] for row in await cursor.fetchall()}
        assert {"ai_filter_result", "ai_filter_checked_at"} <= columns
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recent_ai_filter_candidates_exclude_marked_duplicates(tmp_path):
    db = Database(tmp_path / "rss.db")
    await db.init_db()
    try:
        duplicate = _make_article(0, 1)
        duplicate.id = None
        duplicate.title = "duplicate topic"
        duplicate_id = await db.add_article(duplicate)
        keep = _make_article(0, 1)
        keep.id = None
        keep.title = "independent topic"
        keep.guid = "keep"
        keep.link = "https://example.com/keep"
        keep_id = await db.add_article(keep)
        assert duplicate_id is not None
        assert keep_id is not None

        await db.set_article_ai_filter_results({duplicate_id: True})

        candidates = await db.get_recent_ai_filter_candidates(30, [])

        assert candidates == [(keep_id, "independent topic")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_collect_digest_targets_groups_by_visible_articles():
    context = MagicMock()
    db = MagicMock()
    scheduler = RSSScheduler(context, db, MagicMock(), {})

    sub1 = RSSSubscription(id=1)
    sub2 = RSSSubscription(id=2)

    u1s1 = Subscriber(id=11, subscription_id=1, umo="u1")
    u1s2 = Subscriber(id=12, subscription_id=2, umo="u1")
    u2s1 = Subscriber(id=21, subscription_id=1, umo="u2")
    u2s2 = Subscriber(id=22, subscription_id=2, umo="u2")
    stopped = Subscriber(
        id=31,
        subscription_id=1,
        umo="u3",
        personal_config={"stop": True},
    )

    async def get_subscribers(subscription_id: int):
        return {
            1: [u1s1, u2s1, stopped],
            2: [u1s2, u2s2],
        }[subscription_id]

    async def get_unsent(
        subscription_id: int, subscriber_id: int, recent_days: int = 0
    ):
        mapping = {
            (1, 11): [_make_article(1, 1)],
            (2, 12): [_make_article(2, 2)],
            (1, 21): [_make_article(1, 1)],
            (2, 22): [_make_article(2, 2), _make_article(3, 2)],
        }
        return mapping.get((subscription_id, subscriber_id), [])

    db.get_subscribers = AsyncMock(side_effect=get_subscribers)
    db.get_unsent_articles_for_subscriber = AsyncMock(side_effect=get_unsent)

    targets = await scheduler._collect_digest_targets([sub1, sub2], recent_days=0)

    assert set(targets) == {"1,2", "1,2,3"}
    assert [recipient["umo"] for recipient in targets["1,2"]["recipients"]] == ["u1"]
    assert [recipient["umo"] for recipient in targets["1,2,3"]["recipients"]] == ["u2"]


@pytest.mark.asyncio
async def test_collect_digest_targets_applies_subscriber_filters():
    context = MagicMock()
    db = MagicMock()
    scheduler = RSSScheduler(context, db, MagicMock(), {})

    subscription = RSSSubscription(id=1, black_keyword="blocked")
    subscriber = Subscriber(
        id=11,
        subscription_id=1,
        umo="u1",
        personal_config={"only_has_pic": True},
    )
    blocked = _make_article(1, 1)
    blocked.title = "blocked title"
    no_image = _make_article(2, 1)
    visible = _make_article(3, 1)
    visible.image_urls = ["https://example.com/image.jpg"]

    db.get_subscribers = AsyncMock(return_value=[subscriber])
    db.get_unsent_articles_for_subscriber = AsyncMock(
        return_value=[blocked, no_image, visible]
    )
    db.mark_articles_sent_to_subscriber = AsyncMock()

    targets = await scheduler._collect_digest_targets([subscription], recent_days=0)

    assert set(targets) == {"3"}
    assert [article.id for article in targets["3"]["articles"]] == [3]
    db.mark_articles_sent_to_subscriber.assert_awaited_once_with(11, [1, 2])


@pytest.mark.asyncio
async def test_collect_digest_targets_applies_white_keyword_filter():
    context = MagicMock()
    db = MagicMock()
    scheduler = RSSScheduler(context, db, MagicMock(), {})

    subscription = RSSSubscription(id=1)
    subscriber = Subscriber(
        id=11,
        subscription_id=1,
        umo="u1",
        personal_config={"white_keyword": "allowed"},
    )
    skipped = _make_article(1, 1)
    visible = _make_article(2, 1)
    visible.title = "allowed update"

    db.get_subscribers = AsyncMock(return_value=[subscriber])
    db.get_unsent_articles_for_subscriber = AsyncMock(return_value=[skipped, visible])
    db.mark_articles_sent_to_subscriber = AsyncMock()

    targets = await scheduler._collect_digest_targets([subscription], recent_days=0)

    assert set(targets) == {"2"}
    assert [article.id for article in targets["2"]["articles"]] == [2]
    db.mark_articles_sent_to_subscriber.assert_awaited_once_with(11, [1])


@pytest.mark.asyncio
async def test_collect_digest_targets_marks_ai_duplicate_sent_for_subscriber():
    context = MagicMock()
    response = MagicMock()
    response.completion_text = '[{"id": 1, "duplicate": true}]'
    context.llm_generate = AsyncMock(return_value=response)
    db = MagicMock()
    scheduler = RSSScheduler(
        context,
        db,
        MagicMock(),
        {"ai_config": {"ai_filter_provider": "filter-provider"}},
    )

    subscription = RSSSubscription(id=1)
    subscriber = Subscriber(
        id=11,
        subscription_id=1,
        umo="u1",
        personal_config={"ai_filter_enabled": True},
    )
    article = _make_article(1, 1)

    db.get_subscribers = AsyncMock(return_value=[subscriber])
    db.get_unsent_articles_for_subscriber = AsyncMock(return_value=[article])
    db.get_article_ai_filter_results = AsyncMock(return_value={})
    db.get_recent_ai_filter_candidates = AsyncMock(
        return_value=[(99, "same event elsewhere")]
    )
    db.set_article_ai_filter_results = AsyncMock()
    db.mark_articles_sent_to_subscriber = AsyncMock()

    targets = await scheduler._collect_digest_targets([subscription], recent_days=0)

    assert targets == {}
    db.mark_articles_sent_to_subscriber.assert_awaited_once_with(11, [1])


@pytest.mark.asyncio
async def test_resuming_personal_stop_marks_backlog_sent_for_that_subscriber(
    tmp_path,
):
    db = Database(tmp_path / "rss.db")
    await db.init_db()
    try:
        subscription_id = await db.add_subscription(
            RSSSubscription(name="feed", url="https://example.com/feed.xml")
        )
        paused = Subscriber(subscription_id=subscription_id, umo="u1")
        active = Subscriber(subscription_id=subscription_id, umo="u2")
        paused.id = await db.add_subscriber(paused)
        active.id = await db.add_subscriber(active)

        assert paused.id is not None
        assert active.id is not None

        paused.personal_config = {"stop": True}
        await db.update_subscriber(paused)

        await db.add_article(_make_article(1, subscription_id))
        await db.add_article(_make_article(2, subscription_id))

        paused.personal_config = {"stop": False}
        await db.update_subscriber(paused)

        paused_unsent = await db.get_unsent_articles_for_subscriber(
            subscription_id,
            paused.id,
        )
        active_unsent = await db.get_unsent_articles_for_subscriber(
            subscription_id,
            active.id,
        )

        assert paused_unsent == []
        assert {article.title for article in active_unsent} == {"title-1", "title-2"}
    finally:
        await db.close()


def test_normalize_digest_schedule_supports_legacy_time_and_cron():
    assert RSSScheduler.normalize_digest_schedule("9:05") == "09:05"
    assert RSSScheduler.normalize_digest_schedule(" 0 9 * * 1-5 ") == "0 9 * * 1-5"


def test_normalize_digest_schedule_rejects_invalid_values():
    with pytest.raises(ValueError):
        RSSScheduler.normalize_digest_schedule("25:00")

    with pytest.raises(ValueError):
        RSSScheduler.normalize_digest_schedule("0 9 * *")


def test_schedule_to_cron_keeps_cron_and_expands_daily_time():
    scheduler = RSSScheduler(MagicMock(), MagicMock(), MagicMock(), {})

    assert scheduler._schedule_to_cron("09:05") == "5 9 * * *"
    assert scheduler._schedule_to_cron("0 9 * * 1-5") == "0 9 * * 1-5"


def test_make_digest_job_name_is_backward_compatible_for_daily_time():
    scheduler = RSSScheduler(MagicMock(), MagicMock(), MagicMock(), {})

    assert scheduler._make_digest_job_name(3, "09:05") == "rss_digest_3_09_05"
    assert scheduler._make_digest_job_name(3, "0 9 * * 1-5").startswith(
        "rss_digest_3_cron_"
    )


@pytest.mark.asyncio
async def test_schedule_subscription_fetch_removes_job_when_stopped():
    context = MagicMock()
    context.cron_manager.list_jobs = AsyncMock(return_value=[])
    context.cron_manager.add_basic_job = AsyncMock()
    scheduler = RSSScheduler(context, MagicMock(), MagicMock(), {})

    await scheduler.schedule_subscription_fetch(
        RSSSubscription(id=5, name="paused", stop=True)
    )

    context.cron_manager.add_basic_job.assert_not_called()


@pytest.mark.asyncio
async def test_group_time_adds_cron_schedule_with_normalized_storage():
    context = MagicMock()
    db = MagicMock()
    scheduler = MagicMock()
    scheduler.normalize_digest_schedule = RSSScheduler.normalize_digest_schedule
    scheduler.schedule_digest = AsyncMock()
    group_commands = GroupCommands(context, db, scheduler)
    event = MagicMock()
    event.set_result = MagicMock()
    db.get_group = AsyncMock(return_value=RSSGroup(id=1, name="news", schedules=[]))
    db.update_group = AsyncMock()

    await group_commands.group_time(event, 1, "add", "0 9 * * 1-5")

    stored_group = db.get_group.await_args_list[0]
    assert stored_group is not None
    updated_group = db.update_group.await_args.args[0]
    assert updated_group.schedules == ["0 9 * * 1-5"]
    scheduler.schedule_digest.assert_awaited_once_with(updated_group, "0 9 * * 1-5")
