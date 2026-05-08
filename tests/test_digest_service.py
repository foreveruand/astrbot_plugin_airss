from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot_plugin_airss.digest import DigestService
from astrbot_plugin_airss.models import RSSArticle


def _make_article(article_id: int) -> RSSArticle:
    return RSSArticle(
        id=article_id,
        subscription_id=1,
        title=f"title-{article_id}",
        content=f"content-{article_id}",
        link=f"https://example.com/{article_id}",
        guid=str(article_id),
    )


@pytest.mark.asyncio
async def test_generate_digest_with_agent_retries_fallbacks(monkeypatch):
    context = MagicMock()
    context.get_using_provider.return_value = MagicMock(
        provider_config={"id": "session-default"}
    )
    context.astrbot_config_mgr = MagicMock()
    context.astrbot_config_mgr.confs = {
        "conf-1": {
            "provider_settings": {
                "fallback_chat_models": ["fallback-a", "fallback-b"],
            }
        }
    }
    context.astrbot_config_mgr.get_conf_list.return_value = [
        {
            "id": "conf-1",
            "name": "default",
            "path": "/tmp/default.json",
        }
    ]

    config = {
        "ai_config": {
            "ai_provider": "primary",
            "astrbot_config_file": "default",
            "ai_digest_use_agent": True,
        }
    }
    service = DigestService(context, MagicMock(), config)
    run_mock = AsyncMock(side_effect=[RuntimeError("boom"), "digest ok"])
    monkeypatch.setattr(service, "_run_agent_digest", run_mock)
    monkeypatch.setattr(
        "astrbot_plugin_airss.digest.ensure_group_persona",
        AsyncMock(return_value="rss_group_1"),
    )

    result = await service._generate_digest_with_agent(
        [_make_article(1)],
        group_id=1,
        article_signature="1",
    )

    assert result == "digest ok"
    assert [call.kwargs["provider_id"] for call in run_mock.await_args_list] == [
        "primary",
        "fallback-a",
    ]


@pytest.mark.asyncio
async def test_generate_digest_respects_agent_switch(monkeypatch):
    service = DigestService(MagicMock(), MagicMock(), {"ai_config": {}})
    agent_mock = AsyncMock(return_value="agent digest")
    llm_mock = AsyncMock(return_value="llm digest")
    monkeypatch.setattr(service, "_generate_digest_with_agent", agent_mock)
    monkeypatch.setattr(service, "_generate_digest_with_llm", llm_mock)

    result, count = await service.generate_digest([_make_article(1)], group_id=1)

    assert result == "agent digest"
    assert count == 1
    agent_mock.assert_awaited_once()
    llm_mock.assert_not_called()
