from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot_plugin_airss.models import RSSArticle, RSSGroup, Subscriber
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


@pytest.mark.asyncio
async def test_collect_digest_targets_groups_by_visible_articles():
    context = MagicMock()
    db = MagicMock()
    scheduler = RSSScheduler(context, db, MagicMock(), {})

    sub1 = MagicMock(id=1)
    sub2 = MagicMock(id=2)

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

    async def get_unsent(subscription_id: int, subscriber_id: int, recent_days: int = 0):
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
    assert [recipient["umo"] for recipient in targets["1,2,3"]["recipients"]] == [
        "u2"
    ]
