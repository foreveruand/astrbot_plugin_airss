from __future__ import annotations

import hashlib

from astrbot_plugin_airss.fetcher import RSSFetcher


def _rsshub_code(request_path: str, key: str) -> str:
    return hashlib.md5(f"{request_path}{key}".encode()).hexdigest()


def test_build_rsshub_url_preserves_existing_percent_escapes_for_code():
    key = "secret"
    fetcher = RSSFetcher(rsshub_url="https://rsshub.example", rsshub_key=key)

    url = fetcher.build_rsshub_url("/github/file/user/repo/master/a%2Fb.js")

    request_path = "/github/file/user/repo/master/a%2Fb.js"
    assert url == (
        "https://rsshub.example/github/file/user/repo/master/a%2Fb.js"
        f"?code={_rsshub_code(request_path, key)}"
    )


def test_build_rsshub_url_quotes_raw_path_chars_before_code():
    key = "secret"
    fetcher = RSSFetcher(rsshub_url="https://rsshub.example", rsshub_key=key)

    url = fetcher.build_rsshub_url("/search/caf\u00e9 news")

    request_path = "/search/caf%C3%A9%20news"
    assert url == (
        "https://rsshub.example/search/caf%C3%A9%20news"
        f"?code={_rsshub_code(request_path, key)}"
    )


def test_build_rsshub_url_appends_code_to_existing_query():
    key = "secret"
    fetcher = RSSFetcher(rsshub_url="https://rsshub.example", rsshub_key=key)

    url = fetcher.build_rsshub_url("/telegram/channel/rsshub?limit=20")

    request_path = "/telegram/channel/rsshub"
    assert url == (
        "https://rsshub.example/telegram/channel/rsshub"
        f"?limit=20&code={_rsshub_code(request_path, key)}"
    )
