from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot_plugin_airss.commands import GroupCommands
from astrbot_plugin_airss.main import Main
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
    assert Main._resolve_update_config_key("black_keyword") == ("black_keyword", None)


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
