"""
AI Digest service for RSS plugin using AstrBot's LLM capabilities.

The digest prompt is retrieved from AstrBot's Persona system.
Each RSS group can have its own Persona with a custom system prompt.
Persona ID format: rss_group_{group_id}
"""

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

from .database import Database
from .models import RSSArticle, RSSGroup

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")


class DigestService:
    """Generates AI-powered digests from RSS articles using AstrBot's Persona system."""

    # Minimal fallback prompt used only when no Persona is configured
    FALLBACK_PROMPT = "Summarize the following RSS articles in a clear, organized format."

    def __init__(self, context: "Context", db: Database):
        self.context = context
        self.db = db
        self._config_cache: Optional[dict] = None

    def _get_config(self) -> dict:
        """Get plugin config with caching."""
        if self._config_cache is not None:
            return self._config_cache
        try:
            config = self.context.get_config()
            self._config_cache = config if isinstance(config, dict) else {}
        except Exception:
            self._config_cache = {}
        return self._config_cache

    def _get_config_value(self, key: str, default: Any) -> Any:
        """Get a config value with fallback to default."""
        config = self._get_config()
        return config.get(key, default)

    def _get_persona_system_prompt(self, group_id: int) -> str:
        """
        Get the system prompt from the Persona associated with this RSS group.

        Each RSS group has a Persona with ID: rss_group_{group_id}
        The Persona's system_prompt contains the digest instructions.

        Returns:
            System prompt from Persona, or fallback if not found
        """
        persona_id = f"rss_group_{group_id}"
        
        try:
            persona = self.context.persona_manager.get_persona(persona_id)
            if persona and persona.system_prompt:
                logger.debug(f"Using Persona {persona_id} for digest")
                return persona.system_prompt
        except Exception as e:
            logger.warning(f"Failed to get Persona {persona_id}: {e}")

        # Try to get group's configured persona_id
        # (in case it was set differently)
        return self.FALLBACK_PROMPT

    async def generate_digest(
        self,
        articles: list[RSSArticle],
        group_id: int,
    ) -> str:
        """
        Generate an AI digest from articles.

        Args:
            articles: List of articles to summarize
            group_id: Group ID for persona lookup

        Returns:
            Generated digest content
        """
        if not articles:
            return "暂无新文章。"

        # Get config values
        config = self._get_config()
        max_articles = config.get("ai_digest_max_articles", 50)
        max_input_tokens = config.get("ai_digest_max_input_tokens", 131072)
        max_output_tokens = config.get("ai_digest_max_output_tokens", 8192)
        title_max_len = config.get("ai_digest_title_max_len", 120)
        content_max_len = config.get("ai_digest_content_max_len", 2048)
        fallback_message = config.get("ai_fallback_message", "")

        # Trim candidates to max articles and truncate fields
        trimmed = self._trim_candidates(articles[:max_articles], title_max_len, content_max_len)
        if not trimmed:
            return "暂无新文章。"

        # Get system prompt from Persona
        system_prompt = self._get_persona_system_prompt(group_id)

        # Build prompt with articles
        prompt = self._build_prompt(trimmed)

        # Token budget management - trim if exceeds budget
        while self._count_tokens(prompt) > max_input_tokens and len(trimmed) > 1:
            trimmed.pop()
            prompt = self._build_prompt(trimmed)

        if self._count_tokens(prompt) > max_input_tokens:
            logger.warning(
                "Digest input still exceeds token budget=%s, using fallback",
                max_input_tokens,
            )
            return self._generate_fallback(trimmed, fallback_message)

        # Try AI generation
        try:
            ai_provider = await self._get_ai_provider()

            if ai_provider:
                response = await self.context.llm_generate(
                    chat_provider_id=ai_provider,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_output_tokens,
                )
                return response.completion_text
            else:
                # Use session default provider
                response = await self._generate_with_default(
                    prompt, system_prompt, max_output_tokens
                )
                if response:
                    return response

        except Exception as e:
            logger.error(f"AI digest generation failed: {e}", exc_info=True)

        # Fallback to simple list
        return self._generate_fallback(trimmed, fallback_message)

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

    async def _get_ai_provider(self) -> Optional[str]:
        """Get configured AI provider ID from plugin config."""
        try:
            config = self.context.get_config()
            if config and isinstance(config, dict):
                provider_id = config.get("ai_provider", "")
                if provider_id:
                    return provider_id
        except Exception:
            pass
        return None

    async def _generate_with_default(
        self, prompt: str, system_prompt: str, max_output_tokens: int
    ) -> Optional[str]:
        """Generate using any available provider."""
        try:
            providers = self.context.provider_manager.get_providers()
            for provider in providers:
                if hasattr(provider, "provider_id"):
                    try:
                        response = await self.context.llm_generate(
                            chat_provider_id=provider.provider_id,
                            prompt=prompt,
                            system_prompt=system_prompt,
                            max_tokens=max_output_tokens,
                        )
                        return response.completion_text
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Failed to generate with default provider: {e}")
        return None

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
        return text[:max(0, limit - 1)] + "…"

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
        self, 
        article: RSSArticle, 
        group_id: int,
        provider_id: Optional[str] = None
    ) -> str:
        """
        Generate summary for a single article using group's Persona.
        """
        system_prompt = self._get_persona_system_prompt(group_id)
        prompt = f"Summarize this article:\n\nTITLE: {article.title}\n\nCONTENT: {self._truncate(article.content or '', 1000)}"
        
        try:
            if provider_id:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                return response.completion_text
        except Exception as e:
            logger.error(f"Failed to generate article summary: {e}")
        
        return article.title or "Untitled"