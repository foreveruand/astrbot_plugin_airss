"""
AI Digest service for RSS plugin using AstrBot's LLM capabilities.

The digest prompt is retrieved from AstrBot's Persona system.
Each RSS group can have its own Persona with a custom system prompt.
Persona ID format: rss_group_{group_id}
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .database import Database
from .models import RSSArticle

if TYPE_CHECKING:
    from astrbot.api import AstrBotConfig
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")


class DigestService:
    """Generates AI-powered digests from RSS articles using AstrBot's Persona system."""

    FALLBACK_PROMPT = (
        "Summarize the following RSS articles in a clear, organized format."
    )

    def __init__(self, context: "Context", db: Database, config: "AstrBotConfig"):
        self.context = context
        self.db = db
        self.config = config

    async def _get_persona_system_prompt(self, group_id: int) -> str:
        persona_id = f"rss_group_{group_id}"

        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
            if persona and persona.system_prompt:
                logger.debug(f"Using Persona {persona_id} for digest")
                return persona.system_prompt
        except Exception as e:
            logger.warning(f"Failed to get Persona {persona_id}: {e}")

        return self.FALLBACK_PROMPT

    def _get_ai_provider(self) -> str | None:
        ai_config = self.config.get("ai_config", {})
        provider_id = ai_config.get("ai_provider", "")
        return provider_id if provider_id else None

    def _get_astrbot_config(self) -> tuple[dict | None, str]:
        """Resolve the selected AstrBot config used as the fallback source."""
        ai_config = self.config.get("ai_config", {})
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
                if config_file_name in {display_name, path_name, conf_id} or target_name in {
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

    def _get_fallback_providers(self) -> list[str]:
        """Get fallback provider IDs from AstrBot config, excluding primary provider."""
        primary_provider = self._get_ai_provider()
        astrbot_config, source_name = self._get_astrbot_config()
        if not astrbot_config:
            logger.debug("AI fallback providers disabled because astrbot_config_file is empty")
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

    def _get_all_providers(self) -> list[str]:
        """Get all provider IDs (primary + fallbacks) for sequential trying."""
        providers: list[str] = []
        primary = self._get_ai_provider()
        if primary:
            providers.append(primary)
        providers.extend(self._get_fallback_providers())
        return providers

    async def generate_digest(
        self,
        articles: list[RSSArticle],
        group_id: int,
    ) -> tuple[str, int]:
        """
        Generate an AI digest from articles.

        Args:
            articles: List of articles to summarize
            group_id: Group ID for persona lookup

        Returns:
            Tuple of (generated digest content, number of articles processed)
        """
        if not articles:
            return "暂无新文章。", 0

        ai_config = self.config.get("ai_config", {})
        max_articles = ai_config.get("ai_digest_max_articles", 50)
        recent_days = ai_config.get("ai_digest_recent_days", 0)
        max_input_tokens = ai_config.get("ai_digest_max_input_tokens", 131072)
        max_output_tokens = ai_config.get("ai_digest_max_output_tokens", 8192)
        title_max_len = ai_config.get("ai_digest_title_max_len", 120)
        content_max_len = ai_config.get("ai_digest_content_max_len", 2048)

        filtered_articles = self._filter_recent_articles(articles, recent_days)

        trimmed = self._trim_candidates(
            filtered_articles[:max_articles], title_max_len, content_max_len
        )
        if not trimmed:
            return "暂无新文章。", 0

        system_prompt = await self._get_persona_system_prompt(group_id)
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
                return response.completion_text, len(trimmed)
            except Exception as e:
                last_exception = e
                logger.warning(
                    "Provider %s failed for digest generation: %s",
                    provider_id,
                    e,
                )
                if is_last:
                    logger.error(
                        "All %d provider(s) failed for digest generation",
                        total_providers,
                    )
                    raise last_exception
                continue

        raise last_exception or RuntimeError("Unexpected error in digest generation")

    def _filter_recent_articles(
        self, articles: list[RSSArticle], recent_days: int
    ) -> list[RSSArticle]:
        """Filter articles to those updated/published within recent_days."""
        if recent_days <= 0:
            return articles

        cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
        filtered: list[RSSArticle] = []

        for article in articles:
            article_time = article.published_at or article.fetched_at
            if not article_time:
                continue

            normalized_time = self._normalize_datetime(article_time)
            if normalized_time >= cutoff:
                filtered.append(article)

        logger.debug(
            "Digest recent-days filter applied: kept %d/%d articles within %d day(s)",
            len(filtered),
            len(articles),
            recent_days,
        )
        return filtered

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        """Normalize datetime values to timezone-aware UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _trim_candidates(
        self,
        articles: list[RSSArticle],
        title_max_len: int,
        content_max_len: int,
    ) -> list[RSSArticle]:
        """Trim articles by truncating title and content fields."""
        trimmed: list[RSSArticle] = []
        for article in articles:
            # Create a copy with truncated title and content
            truncated_title = self._truncate(article.title or "", title_max_len)
            truncated_content = self._truncate(article.content or "", content_max_len)

            # Only include if we have at least title or content
            if not truncated_title and not truncated_content:
                continue

            # Create new article object with truncated values
            trimmed_article = RSSArticle(
                id=article.id,
                subscription_id=article.subscription_id,
                title=truncated_title,
                link=article.link,
                content=truncated_content,
                guid=article.guid,
                published_at=article.published_at,
                fetched_at=article.fetched_at,
                is_sent=article.is_sent,
                image_urls=article.image_urls,
            )
            trimmed.append(trimmed_article)
        return trimmed

    def _count_tokens(self, text: str) -> int:
        """
        Count tokens in text using simple approximation.
        Uses char/3 approximation (rough estimate for multilingual text).
        """
        if not text:
            return 0
        # Simple approximation: ~3 chars per token on average
        return max(1, len(text) // 3)

    def _build_prompt(self, articles: list[RSSArticle]) -> str:
        """
        Build prompt from articles.

        Following nonebot_plugin_rss pattern: simple article list,
        let AI handle topic organization based on Persona instructions.
        """
        lines = ["ARTICLES:\n"]

        for i, article in enumerate(articles, 1):
            # Title and content already truncated in _trim_candidates
            title = article.title or ""
            content = article.content or ""
            link = article.link or ""

            lines.append(f"{i}. TITLE: {title}")
            if content:
                lines.append(f"CONTENT: {content}")
            lines.append(f"LINK: {link}")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Truncate text to limit characters (from nonebot_plugin_rss)."""
        text = re.sub(r"\s+", " ", text).strip()
        if limit <= 0:
            return ""
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    def _generate_fallback(
        self, articles: list[RSSArticle], fallback_message: str = ""
    ) -> str:
        """Generate fallback summary without AI (similar to nonebot_plugin_rss)."""
        # Use configured fallback message if provided
        if fallback_message:
            return fallback_message

        # Otherwise, generate article list
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
        """
        Generate summary for a single article using group's Persona.
        """
        system_prompt = await self._get_persona_system_prompt(group_id)
        prompt = f"Summarize this article:\n\nTITLE: {article.title}\n\nCONTENT: {self._truncate(article.content or '', 1000)}"

        providers: list[str] = []
        if provider_id:
            providers.append(provider_id)
        providers.extend(self._get_fallback_providers())

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
            except Exception as e:
                logger.warning(
                    "Provider %s failed for single summary: %s",
                    prov_id,
                    e,
                )
                if idx == len(providers) - 1:
                    logger.error("All providers failed for single summary")
                continue

        return article.title or "Untitled"
