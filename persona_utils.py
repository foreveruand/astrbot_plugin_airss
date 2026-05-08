"""Helpers for maintaining per-group digest personas."""

from __future__ import annotations

import logging

from .database import Database
from .models import RSSGroup

logger = logging.getLogger("astrbot")

GROUP_PERSONA_PROMPT = """你是 RSS 分组摘要 Agent。

你的首要任务是基于用户提供的 RSS 文章内容，整理出清晰、可靠、便于阅读的摘要。

要求：
1. 优先总结已提供的 RSS 内容，不要忽略文章本身。
2. 只有在文章内容明显不足以完成摘要，或需要补充关键背景时，才调用工具或 Skills 进一步检索。
3. 输出应结构清晰，突出重要信息、主题归类和潜在影响。
4. 如果不同文章观点冲突或信息不完整，要明确说明不确定性。
"""


def build_group_persona_id(group_id: int) -> str:
    """Build the canonical persona id for an RSS group."""
    return f"rss_group_{group_id}"


async def ensure_group_persona_for_group(context, db: Database, group: RSSGroup) -> str:
    """Ensure the group's persona metadata and backing AstrBot persona exist."""
    if group.id is None:
        raise ValueError("Group ID is required to ensure group persona")

    persona_id = build_group_persona_id(group.id)
    if group.persona_id != persona_id:
        group.persona_id = persona_id
        await db.update_group(group)

    try:
        await context.persona_manager.get_persona(persona_id)
    except ValueError:
        logger.info("Creating missing RSS digest persona: %s", persona_id)
        await context.persona_manager.create_persona(
            persona_id=persona_id,
            system_prompt=GROUP_PERSONA_PROMPT,
            tools=None,
            skills=None,
        )

    return persona_id


async def ensure_group_persona(context, db: Database, group_id: int) -> str:
    """Ensure persona exists for the given group id."""
    group = await db.get_group(group_id)
    if not group:
        raise ValueError(f"Group {group_id} not found")
    return await ensure_group_persona_for_group(context, db, group)
