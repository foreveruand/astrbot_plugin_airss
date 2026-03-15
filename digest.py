"""
AI Digest service for RSS plugin using AstrBot's LLM capabilities.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from .database import Database
from .models import RSSArticle, RSSGroup

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

logger = logging.getLogger("astrbot")


class DigestService:
    """Generates AI-powered digests from RSS articles."""

    DEFAULT_SYSTEM_PROMPT = """你是一个RSS文章摘要助手。你的任务是将用户订阅的RSS文章进行整理和总结。

要求：
1. 按主题对文章进行分类
2. 提取每篇文章的核心要点
3. 生成简洁的摘要
4. 保持原文的重要链接和信息

输出格式：
## 📰 今日要闻

### 📂 [主题分类]
1. **[文章标题]**
   - 要点：[核心内容]
   - 链接：[原文链接]

---

请用清晰、简洁的语言总结，保持信息的准确性和可读性。"""

    def __init__(self, context: "Context", db: Database):
        self.context = context
        self.db = db

    async def generate_digest(
        self,
        articles: list[RSSArticle],
        group_id: int,
        max_articles: int = 50,
    ) -> str:
        """
        Generate an AI digest from articles.

        Args:
            articles: List of articles to summarize
            group_id: Group ID for persona lookup
            max_articles: Maximum articles to include

        Returns:
            Generated digest content
        """
        if not articles:
            return "暂无新文章。"

        # Limit articles
        articles = articles[:max_articles]

        # Get persona for the group
        group = await self.db.get_group(group_id)
        system_prompt = self.DEFAULT_SYSTEM_PROMPT

        if group and group.persona_id:
            persona = self.context.persona_manager.get_persona(group.persona_id)
            if persona and persona.system_prompt:
                system_prompt = persona.system_prompt

        # Build prompt with articles
        prompt = self._build_prompt(articles)

        # Try AI generation
        try:
            ai_provider = await self._get_ai_provider()
            
            if ai_provider:
                response = await self.context.llm_generate(
                    chat_provider_id=ai_provider,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                return response.completion_text
            else:
                # Use session default provider
                response = await self._generate_with_default(prompt, system_prompt)
                if response:
                    return response
                
        except Exception as e:
            logger.error(f"AI digest generation failed: {e}", exc_info=True)

        # Fallback to simple list
        return self._generate_fallback(articles)

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

    async def _generate_with_default(self, prompt: str, system_prompt: str) -> Optional[str]:
        """Generate using session default provider."""
        try:
            # Get default provider for a test session
            # Since we're in a cron job, we need to use llm_generate with a specific provider
            # Try to get any available provider
            providers = self.context.provider_manager.get_providers()
            for provider in providers:
                if hasattr(provider, 'provider_id'):
                    try:
                        response = await self.context.llm_generate(
                            chat_provider_id=provider.provider_id,
                            prompt=prompt,
                            system_prompt=system_prompt,
                        )
                        return response.completion_text
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Failed to generate with default provider: {e}")
        return None

    def _build_prompt(self, articles: list[RSSArticle]) -> str:
        """Build prompt from articles."""
        lines = ["以下是今天订阅的RSS文章，请进行整理和总结：\n"]
        
        for i, article in enumerate(articles, 1):
            lines.append(f"### 文章 {i}")
            lines.append(f"标题：{article.title}")
            if article.content:
                # Truncate long content
                content = article.content[:500] + "..." if len(article.content) > 500 else article.content
                lines.append(f"内容：{content}")
            lines.append(f"链接：{article.link}")
            if article.published_at:
                lines.append(f"发布时间：{article.published_at.strftime('%Y-%m-%d %H:%M')}")
            lines.append("")
        
        return "\n".join(lines)

    def _generate_fallback(self, articles: list[RSSArticle]) -> str:
        """Generate fallback summary without AI."""
        lines = [
            "## 📰 RSS 文章摘要",
            f"\n共 {len(articles)} 篇新文章：\n",
        ]
        
        # Group by date
        date_groups: dict[str, list[RSSArticle]] = {}
        for article in articles:
            date_str = (article.published_at or article.fetched_at).strftime("%Y-%m-%d")
            if date_str not in date_groups:
                date_groups[date_str] = []
            date_groups[date_str].append(article)
        
        for date_str, date_articles in sorted(date_groups.items(), reverse=True):
            lines.append(f"### {date_str}")
            for article in date_articles:
                lines.append(f"- **{article.title}**")
                lines.append(f"  链接：{article.link}")
            lines.append("")
        
        return "\n".join(lines)
    
    async def generate_single_summary(self, article: RSSArticle, provider_id: Optional[str] = None) -> str:
        """Generate summary for a single article."""
        prompt = f"请用简洁的语言总结以下文章的核心内容：\n\n标题：{article.title}\n\n内容：{article.content[:1000] if article.content else '无内容'}"
        
        try:
            if provider_id:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt="你是一个文章摘要助手，请用简洁的语言总结文章核心内容。",
                )
                return response.completion_text
        except Exception as e:
            logger.error(f"Failed to generate article summary: {e}")
        
        return article.title