from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from astrbot_plugin_airss.digest import DigestService
from astrbot_plugin_airss.models import RSSArticle

from astrbot.core.agent.tool import ToolSet


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
    run_mock = AsyncMock(side_effect=[Exception("model not found"), "digest ok"])
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
async def test_generate_digest_with_agent_does_not_fallback_on_non_provider_error(
    monkeypatch,
):
    service = DigestService(
        MagicMock(),
        MagicMock(),
        {
            "ai_config": {
                "ai_provider": "primary",
                "astrbot_config_file": "default",
                "ai_digest_use_agent": True,
            }
        },
    )
    monkeypatch.setattr(
        service,
        "_get_all_providers",
        lambda session_umo=None: ["primary", "fallback-a"],
    )
    monkeypatch.setattr(
        "astrbot_plugin_airss.digest.ensure_group_persona",
        AsyncMock(return_value="rss_group_1"),
    )
    run_mock = AsyncMock(side_effect=AttributeError("conversation_id missing"))
    monkeypatch.setattr(service, "_run_agent_digest", run_mock)

    with pytest.raises(AttributeError, match="conversation_id missing"):
        await service._generate_digest_with_agent(
            [_make_article(1)],
            group_id=1,
            article_signature="1",
        )

    assert [call.kwargs["provider_id"] for call in run_mock.await_args_list] == [
        "primary"
    ]


@pytest.mark.asyncio
async def test_generate_digest_with_llm_retries_on_model_link_error(monkeypatch):
    context = MagicMock()
    service = DigestService(
        context,
        MagicMock(),
        {"ai_config": {"ai_provider": "primary", "astrbot_config_file": "default"}},
    )
    monkeypatch.setattr(
        service,
        "_get_all_providers",
        lambda session_umo=None: ["primary", "fallback-a"],
    )
    monkeypatch.setattr(
        service,
        "_get_persona_system_prompt",
        AsyncMock(return_value="system"),
    )

    not_found = Exception("Model not found for endpoint")
    context.llm_generate = AsyncMock(
        side_effect=[
            not_found,
            MagicMock(completion_text="digest ok"),
        ]
    )

    result = await service._generate_digest_with_llm([_make_article(1)], group_id=1)

    assert result == "digest ok"
    assert context.llm_generate.await_count == 2


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


def test_build_main_agent_config_uses_current_compress_ratio_key(monkeypatch):
    service = DigestService(MagicMock(), MagicMock(), {"ai_config": {}})
    monkeypatch.setattr(
        service,
        "_get_effective_astrbot_config",
        lambda session_umo: {
            "provider_settings": {
                "llm_compress_keep_recent": 6,
                "llm_compress_keep_recent_ratio": 0.2,
            }
        },
    )

    config = service._build_main_agent_config("cron:FriendMessage:test")

    assert config.llm_compress_keep_recent_ratio == 0.2


@pytest.mark.asyncio
async def test_prepare_digest_agent_tools_includes_persona_and_web_search_tools(
    monkeypatch,
):
    context = MagicMock()
    tool_mgr = MagicMock()
    fetch_tool = MagicMock(name="fetch_tool")
    fetch_tool.name = "fetch_url"
    tavily_tool = MagicMock(name="tavily_tool")
    tavily_tool.name = "web_search_tavily"
    extract_tool = MagicMock(name="extract_tool")
    extract_tool.name = "tavily_extract_web_page"

    tool_mgr.get_func.side_effect = {
        "fetch_url": fetch_tool,
        "web_search_tavily": tavily_tool,
        "tavily_extract_web_page": extract_tool,
    }.get
    context.get_llm_tool_manager.return_value = tool_mgr
    context.persona_manager.get_persona = AsyncMock(
        return_value=MagicMock(tools=["fetch_url"])
    )

    service = DigestService(
        context,
        MagicMock(),
        {"ai_config": {}, "provider_settings": {}},
    )
    monkeypatch.setattr(
        "astrbot_plugin_airss.digest.ensure_group_persona",
        AsyncMock(return_value="rss_group_1"),
    )
    monkeypatch.setattr(
        service,
        "_get_effective_astrbot_config",
        lambda session_umo: {
            "provider_settings": {
                "web_search": True,
                "websearch_provider": "tavily",
            }
        },
    )

    req = MagicMock()
    req.func_tool = ToolSet()

    await service._prepare_digest_agent_tools(req, "cron:FriendMessage:test", 1)

    assert req.func_tool.get_tool("fetch_url") is fetch_tool
    assert req.func_tool.get_tool("web_search_tavily") is tavily_tool
    assert req.func_tool.get_tool("tavily_extract_web_page") is extract_tool


@pytest.mark.asyncio
async def test_run_agent_digest_deletes_temp_conversation(monkeypatch):
    context = MagicMock()
    context.get_provider_by_id.return_value = MagicMock()
    context.conversation_manager.delete_conversation = AsyncMock()

    service = DigestService(context, MagicMock(), {"ai_config": {}})
    monkeypatch.setattr("astrbot_plugin_airss.digest.Provider", object)
    monkeypatch.setattr(service, "_prepare_digest_agent_tools", AsyncMock())
    monkeypatch.setattr(
        service,
        "_prepare_digest_conversation",
        AsyncMock(return_value=MagicMock(conversation_id="conv-1")),
    )
    monkeypatch.setattr(
        "astrbot_plugin_airss.digest.build_main_agent",
        AsyncMock(return_value=None),
    )

    with pytest.raises(RuntimeError, match="Failed to build main agent"):
        await service._run_agent_digest(
            articles=[_make_article(1)],
            group_id=1,
            article_signature="1",
            provider_id="primary",
            session_umo="cron:FriendMessage:test",
        )

    context.conversation_manager.delete_conversation.assert_awaited_once_with(
        "cron:FriendMessage:test",
        "conv-1",
    )


@pytest.mark.asyncio
async def test_run_agent_digest_deletes_temp_conversation_with_legacy_cid(
    monkeypatch,
):
    context = MagicMock()
    context.get_provider_by_id.return_value = MagicMock()
    context.conversation_manager.delete_conversation = AsyncMock()

    service = DigestService(context, MagicMock(), {"ai_config": {}})
    monkeypatch.setattr("astrbot_plugin_airss.digest.Provider", object)
    monkeypatch.setattr(service, "_prepare_digest_agent_tools", AsyncMock())
    monkeypatch.setattr(
        service,
        "_prepare_digest_conversation",
        AsyncMock(return_value=MagicMock(cid="legacy-conv-1")),
    )
    monkeypatch.setattr(
        "astrbot_plugin_airss.digest.build_main_agent",
        AsyncMock(return_value=None),
    )

    with pytest.raises(RuntimeError, match="Failed to build main agent"):
        await service._run_agent_digest(
            articles=[_make_article(1)],
            group_id=1,
            article_signature="1",
            provider_id="primary",
            session_umo="cron:FriendMessage:test",
        )

    context.conversation_manager.delete_conversation.assert_awaited_once_with(
        "cron:FriendMessage:test",
        "legacy-conv-1",
    )
