"""AI digest generation for AIRSS."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.provider import Provider
from astrbot.core.provider.entities import ProviderRequest

from .database import Database
from .models import RSSArticle
from .persona_utils import ensure_group_persona

if TYPE_CHECKING:
    from astrbot.api import AstrBotConfig
    from astrbot.core.db.po import ConversationV2
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")


class DigestService:
    """Generate AI digests for RSS articles."""

    FALLBACK_PROMPT = (
        "Summarize the following RSS articles in a clear, organized format."
    )

    def __init__(self, context: Context, db: Database, config: AstrBotConfig):
        self.context = context
        self.db = db
        self.config = config

    def _get_ai_config(self) -> dict:
        return self.config.get("ai_config", {})

    def _get_ai_provider(self, session_umo: str | None = None) -> str | None:
        ai_config = self._get_ai_config()
        provider_id = ai_config.get("ai_provider", "")
        if provider_id:
            return provider_id

        try:
            provider = self.context.get_using_provider(umo=session_umo)
        except ValueError as exc:
            logger.warning("Failed to resolve default digest provider: %s", exc)
            return None

        if not provider:
            return None
        return str(provider.provider_config.get("id", "") or "") or None

    def _get_astrbot_config(self) -> tuple[dict | None, str]:
        """Resolve the selected AstrBot config used as the fallback source."""
        ai_config = self._get_ai_config()
        config_file_name = ai_config.get("astrbot_config_file", "")
        source_name = "disabled"

        if config_file_name:
            config_mgr = getattr(self.context, "astrbot_config_mgr", None)
            confs = getattr(config_mgr, "confs", {}) if config_mgr else {}
            conf_list = config_mgr.get_conf_list() if config_mgr else []
            target_name = Path(config_file_name).name

            for conf_info in conf_list:
                conf_id = conf_info.get("id")
                if conf_id not in confs:
                    continue
                display_name = conf_info.get("name", "")
                path_name = Path(conf_info.get("path", "")).name
                if config_file_name in {
                    display_name,
                    path_name,
                    conf_id,
                } or target_name in {
                    display_name,
                    path_name,
                    conf_id,
                }:
                    source_name = display_name or path_name or conf_id or target_name
                    return confs[conf_id], source_name
            logger.warning(
                "AstrBot config file %s not found, falling back to current session config",
                config_file_name,
            )
            return None, source_name

        return None, source_name

    def _get_fallback_providers(self, session_umo: str | None = None) -> list[str]:
        """Get fallback provider IDs from AstrBot config, excluding primary provider."""
        primary_provider = self._get_ai_provider(session_umo=session_umo)
        astrbot_config, source_name = self._get_astrbot_config()
        if not astrbot_config:
            logger.debug(
                "AI fallback providers disabled because astrbot_config_file is empty"
            )
            return []

        provider_settings = astrbot_config.get("provider_settings", {})
        fallback_ids = provider_settings.get("fallback_chat_models", [])
        source_label = "provider_settings.fallback_chat_models"

        if not isinstance(fallback_ids, list):
            logger.warning("%s is not a list, skipping fallbacks", source_label)
            return []

        seen: set[str] = {primary_provider} if primary_provider else set()
        valid_fallbacks: list[str] = []

        for provider_id in fallback_ids:
            if not isinstance(provider_id, str) or not provider_id:
                continue
            if provider_id in seen:
                continue
            valid_fallbacks.append(provider_id)
            seen.add(provider_id)

        logger.debug(
            "AI fallback providers resolved from %s: %s",
            source_name,
            valid_fallbacks,
        )

        return valid_fallbacks

    def _get_all_providers(self, session_umo: str | None = None) -> list[str]:
        """Get all provider IDs (primary + fallbacks) for sequential trying."""
        providers: list[str] = []
        primary = self._get_ai_provider(session_umo=session_umo)
        if primary:
            providers.append(primary)
        providers.extend(self._get_fallback_providers(session_umo=session_umo))
        return providers

    def _get_effective_astrbot_config(self, session_umo: str) -> dict:
        selected_config, _ = self._get_astrbot_config()
        if selected_config:
            return selected_config
        return self.context.get_config(umo=session_umo)

    async def generate_digest(
        self,
        articles: list[RSSArticle],
        group_id: int,
        article_signature: str | None = None,
    ) -> tuple[str, int]:
        """Generate an AI digest from articles."""
        if not articles:
            return "暂无新文章。", 0

        ai_config = self._get_ai_config()
        max_articles = ai_config.get("ai_digest_max_articles", 50)
        max_input_tokens = ai_config.get("ai_digest_max_input_tokens", 131072)
        title_max_len = ai_config.get("ai_digest_title_max_len", 120)
        content_max_len = ai_config.get("ai_digest_content_max_len", 2048)

        trimmed = self._trim_candidates(
            articles[:max_articles], title_max_len, content_max_len
        )
        if not trimmed:
            return "暂无新文章。", 0

        prompt = self._build_prompt(trimmed)

        while self._count_tokens(prompt) > max_input_tokens and len(trimmed) > 1:
            trimmed.pop()
            prompt = self._build_prompt(trimmed)

        if self._count_tokens(prompt) > max_input_tokens:
            logger.warning(
                "Digest input still exceeds token budget=%s, using fallback",
                max_input_tokens,
            )
            raise ValueError("Input exceeds token budget even after trimming")

        if ai_config.get("ai_digest_use_agent", True):
            digest = await self._generate_digest_with_agent(
                trimmed,
                group_id,
                article_signature=article_signature,
            )
        else:
            digest = await self._generate_digest_with_llm(trimmed, group_id)

        return digest, len(trimmed)

    async def _generate_digest_with_llm(
        self,
        articles: list[RSSArticle],
        group_id: int,
    ) -> str:
        system_prompt = await self._get_persona_system_prompt(group_id)
        prompt = self._build_prompt(articles)
        max_output_tokens = self._get_ai_config().get(
            "ai_digest_max_output_tokens", 8192
        )

        providers = self._get_all_providers()
        if not providers:
            raise ValueError("No AI provider configured for digest generation")

        last_exception: Exception | None = None
        total_providers = len(providers)

        for idx, provider_id in enumerate(providers):
            is_last = idx == total_providers - 1

            if idx > 0:
                logger.warning(
                    "Switching to fallback provider: %s (attempt %d/%d)",
                    provider_id,
                    idx + 1,
                    total_providers,
                )

            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_output_tokens,
                )
                if idx > 0:
                    logger.info(
                        "Digest generation succeeded with fallback provider: %s",
                        provider_id,
                    )
                return response.completion_text
            except Exception as exc:
                last_exception = exc
                logger.warning(
                    "Provider %s failed for digest generation: %s",
                    provider_id,
                    exc,
                )
                if is_last:
                    logger.error(
                        "All %d provider(s) failed for digest generation",
                        total_providers,
                    )
                    raise last_exception

        raise last_exception or RuntimeError("Unexpected error in digest generation")

    async def _generate_digest_with_agent(
        self,
        articles: list[RSSArticle],
        group_id: int,
        article_signature: str | None = None,
    ) -> str:
        signature = article_signature or self._build_article_signature(articles)
        session_umo = self._build_digest_session_umo(group_id, signature)
        providers = self._get_all_providers(session_umo=session_umo)
        if not providers:
            raise ValueError("No AI provider configured for digest generation")

        await ensure_group_persona(self.context, self.db, group_id)

        last_exception: Exception | None = None
        total_providers = len(providers)

        for idx, provider_id in enumerate(providers):
            is_last = idx == total_providers - 1
            if idx > 0:
                logger.warning(
                    "Switching to fallback provider: %s (attempt %d/%d)",
                    provider_id,
                    idx + 1,
                    total_providers,
                )

            try:
                return await self._run_agent_digest(
                    articles=articles,
                    group_id=group_id,
                    article_signature=signature,
                    provider_id=provider_id,
                    session_umo=session_umo,
                )
            except Exception as exc:
                last_exception = exc
                logger.warning(
                    "Provider %s failed for agent digest generation: %s",
                    provider_id,
                    exc,
                )
                if is_last:
                    logger.error(
                        "All %d provider(s) failed for agent digest generation",
                        total_providers,
                    )
                    raise last_exception

        raise last_exception or RuntimeError(
            "Unexpected error in agent digest generation"
        )

    async def _run_agent_digest(
        self,
        *,
        articles: list[RSSArticle],
        group_id: int,
        article_signature: str,
        provider_id: str,
        session_umo: str,
    ) -> str:
        provider = self.context.get_provider_by_id(provider_id)
        if not provider or not isinstance(provider, Provider):
            raise ValueError(f"Provider {provider_id} not found for digest generation")

        session = MessageSession.from_str(session_umo)
        cron_event = CronMessageEvent(
            context=self.context,
            session=session,
            message="AIRSS digest generation task",
            extras={
                "rss_digest": {
                    "group_id": group_id,
                    "signature": article_signature,
                    "article_count": len(articles),
                }
            },
            message_type=MessageType.FRIEND_MESSAGE,
        )
        cron_event.role = "admin"

        conversation = await self._prepare_digest_conversation(
            session_umo=session_umo,
            group_id=group_id,
            article_signature=article_signature,
        )
        req = ProviderRequest(
            prompt=self._build_agent_prompt(articles),
            contexts=[],
            system_prompt="",
            conversation=conversation,
        )

        build_result = await build_main_agent(
            event=cron_event,
            plugin_context=self.context,
            config=self._build_main_agent_config(session_umo),
            provider=provider,
            req=req,
        )
        if not build_result:
            raise RuntimeError("Failed to build main agent for digest generation")

        max_steps = self._get_ai_config().get("ai_digest_agent_max_steps", 8)
        async for _ in build_result.agent_runner.step_until_done(max_steps):
            pass

        llm_resp = build_result.agent_runner.get_final_llm_resp()
        if not llm_resp or not (llm_resp.completion_text or "").strip():
            raise RuntimeError("Digest agent returned an empty response")
        return llm_resp.completion_text.strip()

    async def _prepare_digest_conversation(
        self,
        *,
        session_umo: str,
        group_id: int,
        article_signature: str,
    ) -> ConversationV2:
        persona_id = await ensure_group_persona(self.context, self.db, group_id)
        conv_mgr = self.context.conversation_manager
        title = f"RSS Digest {group_id}:{article_signature[:12]}"
        conversation_id = await conv_mgr.new_conversation(
            session_umo,
            "cron",
            content=[],
            title=title,
            persona_id=persona_id,
        )
        await conv_mgr.switch_conversation(session_umo, conversation_id)
        conversation = await conv_mgr.get_conversation(session_umo, conversation_id)
        if not conversation:
            raise RuntimeError("Failed to create digest conversation")
        conversation.persona_id = persona_id
        return conversation

    def _build_main_agent_config(self, session_umo: str) -> MainAgentBuildConfig:
        ai_config = self._get_ai_config()
        astrbot_config = self._get_effective_astrbot_config(session_umo)
        provider_settings = astrbot_config.get("provider_settings", {})
        proactive_cfg = provider_settings.get("proactive_capability", {})

        return MainAgentBuildConfig(
            tool_call_timeout=ai_config.get("ai_digest_tool_call_timeout", 60),
            tool_schema_mode=provider_settings.get("tool_schema_mode", "full"),
            provider_wake_prefix="",
            streaming_response=False,
            sanitize_context_by_modalities=False,
            kb_agentic_mode=False,
            file_extract_enabled=False,
            context_limit_reached_strategy=provider_settings.get(
                "context_limit_reached_strategy",
                "truncate_by_turns",
            ),
            llm_compress_instruction=provider_settings.get(
                "llm_compress_instruction", ""
            ),
            llm_compress_keep_recent=provider_settings.get(
                "llm_compress_keep_recent", 6
            ),
            llm_compress_provider_id=provider_settings.get(
                "llm_compress_provider_id", ""
            ),
            max_context_length=provider_settings.get("max_context_length", -1),
            dequeue_context_length=provider_settings.get("dequeue_context_length", 1),
            fallback_max_context_tokens=provider_settings.get(
                "fallback_max_context_tokens", 128000
            ),
            llm_safety_mode=provider_settings.get("llm_safety_mode", True),
            safety_mode_strategy=provider_settings.get(
                "safety_mode_strategy",
                "system_prompt",
            ),
            computer_use_runtime=provider_settings.get(
                "computer_use_runtime",
                "none",
            ),
            sandbox_cfg=provider_settings.get("sandbox", {}),
            add_cron_tools=bool(proactive_cfg.get("add_cron_tools", True)),
            provider_settings=provider_settings,
            subagent_orchestrator=astrbot_config.get("subagent_orchestrator", {}),
            timezone=astrbot_config.get("timezone"),
        )

    async def _get_persona_system_prompt(self, group_id: int) -> str:
        persona_id = await ensure_group_persona(self.context, self.db, group_id)

        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
            if persona and persona.system_prompt:
                logger.debug("Using Persona %s for digest", persona_id)
                return persona.system_prompt
        except Exception as exc:
            logger.warning("Failed to get Persona %s: %s", persona_id, exc)

        return self.FALLBACK_PROMPT

    def _trim_candidates(
        self,
        articles: list[RSSArticle],
        title_max_len: int,
        content_max_len: int,
    ) -> list[RSSArticle]:
        """Trim articles by truncating title and content fields."""
        trimmed: list[RSSArticle] = []
        for article in articles:
            truncated_title = self._truncate(article.title or "", title_max_len)
            truncated_content = self._truncate(article.content or "", content_max_len)

            if not truncated_title and not truncated_content:
                continue

            trimmed_article = RSSArticle(
                id=article.id,
                subscription_id=article.subscription_id,
                title=truncated_title,
                link=article.link,
                content=truncated_content,
                guid=article.guid,
                author=article.author,
                published_at=article.published_at,
                fetched_at=article.fetched_at,
                is_sent=article.is_sent,
                image_urls=article.image_urls,
            )
            trimmed.append(trimmed_article)
        return trimmed

    def _count_tokens(self, text: str) -> int:
        """Count tokens using a simple approximation."""
        if not text:
            return 0
        return max(1, len(text) // 3)

    def _build_prompt(self, articles: list[RSSArticle]) -> str:
        """Build the digest prompt payload from articles."""
        lines = [
            "Please generate an RSS digest based on the provided articles.",
            "Prioritize summarizing the provided content before using any tools.",
            "",
            "ARTICLES:",
            "",
        ]

        for i, article in enumerate(articles, 1):
            title = article.title or ""
            content = article.content or ""
            link = article.link or ""

            lines.append(f"{i}. TITLE: {title}")
            if content:
                lines.append(f"CONTENT: {content}")
            lines.append(f"LINK: {link}")
            lines.append("")

        return "\n".join(lines)

    def _build_agent_prompt(self, articles: list[RSSArticle]) -> str:
        """Build the user prompt for the full AstrBot agent path."""
        return self._build_prompt(articles)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Truncate text to limit characters."""
        text = re.sub(r"\s+", " ", text).strip()
        if limit <= 0:
            return ""
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    def _generate_fallback(
        self, articles: list[RSSArticle], fallback_message: str = ""
    ) -> str:
        """Generate fallback summary without AI."""
        if fallback_message:
            return fallback_message

        lines = ["**News**"]

        for article in articles:
            title = self._truncate(article.title or "", 50)
            link = article.link or ""
            if title and link:
                lines.append(f"- {title} [{link}]")

        return "\n".join(lines)

    async def generate_single_summary(
        self, article: RSSArticle, group_id: int, provider_id: str | None = None
    ) -> str:
        """Generate summary for a single article using group's Persona."""
        system_prompt = await self._get_persona_system_prompt(group_id)
        prompt = (
            "Summarize this article:\n\n"
            f"TITLE: {article.title}\n\n"
            f"CONTENT: {self._truncate(article.content or '', 1000)}"
        )

        session_umo = self._build_digest_session_umo(
            group_id, str(article.id or article.guid or article.link or "single")
        )
        providers: list[str] = []
        if provider_id:
            providers.append(provider_id)
        else:
            primary = self._get_ai_provider(session_umo=session_umo)
            if primary:
                providers.append(primary)
        providers.extend(self._get_fallback_providers(session_umo=session_umo))

        if not providers:
            return article.title or "Untitled"

        for idx, prov_id in enumerate(providers):
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=prov_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                return response.completion_text
            except Exception as exc:
                logger.warning(
                    "Provider %s failed for single summary: %s",
                    prov_id,
                    exc,
                )
                if idx == len(providers) - 1:
                    logger.error("All providers failed for single summary")
                continue

        return article.title or "Untitled"

    def _build_digest_session_umo(self, group_id: int, article_signature: str) -> str:
        session_hash = hashlib.sha1(article_signature.encode("utf-8")).hexdigest()[:16]
        session = MessageSession(
            platform_name="cron",
            message_type=MessageType.FRIEND_MESSAGE,
            session_id=f"airss_digest_g{group_id}_{session_hash}",
        )
        return str(session)

    @staticmethod
    def _build_article_signature(articles: list[RSSArticle]) -> str:
        article_ids = sorted(
            str(article.id) for article in articles if article.id is not None
        )
        if article_ids:
            return ",".join(article_ids)
        return hashlib.sha1(
            "|".join(
                f"{article.subscription_id}:{article.guid}:{article.link}"
                for article in articles
            ).encode("utf-8")
        ).hexdigest()
