"""
RSS Fetcher - Async RSS feed fetching and parsing.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

import aiohttp
import feedparser

from .models import RSSArticle

logger = logging.getLogger("astrbot")


@dataclass
class FetchResult:
    """Result of fetching an RSS feed."""

    success: bool
    articles: list[RSSArticle] = field(default_factory=list)
    feed_title: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


class RSSFetcher:
    """Async RSS feed fetcher."""

    def __init__(
        self,
        proxy: str | None = None,
        timeout: int = 30,
        rsshub_url: str | None = None,
        rsshub_key: str | None = None,
    ):
        self.proxy = proxy
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.rsshub_url = rsshub_url
        self.rsshub_key = rsshub_key

    def build_rsshub_url(self, path: str) -> str:
        """Build full RSSHub URL from path."""
        if not self.rsshub_url:
            return f"https://rsshub.app/{path.lstrip('/')}"
        elif self.rsshub_key:
            from urllib.parse import quote
            import hashlib

            encoded_router = quote(f"/{path.lstrip('/')}")
            code = hashlib.md5(f"{encoded_router}{self.rsshub_key}".encode()).hexdigest()
            return f"{self.rsshub_url}/{path.lstrip('/')}?code={code}"
        return urljoin(self.rsshub_url, path)

    async def fetch_feed(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
        cookies: str | None = None,
    ) -> FetchResult:
        """
        Fetch and parse an RSS feed.

        Args:
            url: Feed URL
            etag: Previous ETag for conditional request
            last_modified: Previous Last-Modified for conditional request
            cookies: Cookie string

        Returns:
            FetchResult with articles and metadata
        """
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if cookies:
            headers["Cookie"] = cookies

        proxy = self.proxy if self.proxy else None

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=headers, proxy=proxy) as response:
                    if response.status == 304:
                        # Not modified
                        return FetchResult(
                            success=True,
                            articles=[],
                            etag=etag,
                            last_modified=last_modified,
                        )

                    if response.status != 200:
                        return FetchResult(
                            success=False,
                            error=f"HTTP {response.status}",
                        )

                    content = await response.text()
                    new_etag = response.headers.get("ETag")
                    new_last_modified = response.headers.get("Last-Modified")

            # Parse feed in thread pool to avoid blocking
            feed = await asyncio.to_thread(feedparser.parse, content)

            if feed.bozo and not feed.entries:
                return FetchResult(
                    success=False,
                    error=str(feed.bozo_exception)
                    if feed.bozo_exception
                    else "Parse error",
                )

            articles = self._parse_entries(feed.entries)

            # Get feed title
            feed_title = None
            if hasattr(feed, "feed") and hasattr(feed.feed, "title"):
                feed_title = feed.feed.title
                if isinstance(feed_title, dict):
                    feed_title = feed_title.get("value")

            return FetchResult(
                success=True,
                articles=articles,
                feed_title=feed_title,
                etag=new_etag,
                last_modified=new_last_modified,
            )

        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch RSS feed {url}: {e}")
            return FetchResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {e}")
            return FetchResult(success=False, error=str(e))

    def _parse_entries(self, entries: list) -> list[RSSArticle]:
        """Parse feed entries into RSSArticle objects."""
        articles = []

        for entry in entries:
            try:
                # Get GUID
                guid = entry.get("id") or entry.get("guid") or entry.get("link", "")

                # Get title
                title = entry.get("title", "Untitled")
                if isinstance(title, dict):
                    title = title.get("value", "Untitled")

                # Get content
                content = ""
                if entry.get("content"):
                    content = entry.content[0].get("value", "")
                elif entry.get("summary"):
                    content = entry.summary
                elif entry.get("description"):
                    content = entry.description

                # Get link
                link = entry.get("link", "")
                if isinstance(link, dict):
                    link = link.get("href", "")
                elif isinstance(link, list) and link:
                    link = (
                        link[0].get("href", "")
                        if isinstance(link[0], dict)
                        else str(link[0])
                    )

                # Get published date
                published_at = None
                if entry.get("published_parsed"):
                    try:
                        published_at = datetime(
                            *entry.published_parsed[:6], tzinfo=timezone.utc
                        )
                    except (TypeError, ValueError):
                        pass
                elif entry.get("updated_parsed"):
                    try:
                        published_at = datetime(
                            *entry.updated_parsed[:6], tzinfo=timezone.utc
                        )
                    except (TypeError, ValueError):
                        pass

                # Get author
                author = ""
                if entry.get("author"):
                    author = entry.author
                elif entry.get("author_detail") and hasattr(
                    entry.author_detail, "name"
                ):
                    author = entry.author_detail.name

                # Get images from content
                image_urls = self._extract_images(content)

                article = RSSArticle(
                    title=title,
                    content=content,
                    link=link,
                    guid=guid,
                    author=author,
                    published_at=published_at,
                    fetched_at=datetime.now(timezone.utc),
                    image_urls=image_urls,
                )
                articles.append(article)

            except Exception as e:
                logger.warning(f"Failed to parse entry: {e}")
                continue

        return articles

    def _extract_images(self, content: str) -> list[str]:
        """Extract image URLs from HTML content."""
        import re

        # Match img src attributes
        pattern = r'<img[^>]+src=["\']([^"\']+)["\']'
        matches = re.findall(pattern, content, re.IGNORECASE)
        return matches[:5]  # Limit to 5 images


def detect_new_articles(
    articles: list[RSSArticle],
    existing_guids: set[str],
) -> list[RSSArticle]:
    """
    Filter out articles that already exist.

    Args:
        articles: List of articles to filter
        existing_guids: Set of GUIDs that already exist

    Returns:
        List of new articles
    """
    return [a for a in articles if a.guid not in existing_guids]
