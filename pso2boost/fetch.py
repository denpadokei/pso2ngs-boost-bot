"""HTTP 取得（リトライ・礼儀正しい間隔つき）。"""

from __future__ import annotations

import logging
import time
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

SITE = "https://pso2.jp"
SCHEDULE_URL = SITE + "/players/schedule/"
NEWS_URL = SITE + "/players/news/"
EVENT_NEWS_URL = SITE + "/players/news/event/"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 pso2ngs-boost-bot/0.1"
)


class FetchError(RuntimeError):
    """取得に失敗した（呼び出し側は通知をスキップして次回に任せる）。"""


class Fetcher:
    def __init__(
        self,
        delay: float = 1.0,
        retries: int = 3,
        timeout: float = 20.0,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._cache: dict[str, str] = {}
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ja,en;q=0.8",
            }
        )

    def get(self, url: str, *, cache: bool = False) -> str:
        url = urljoin(SITE + "/", url)
        if cache and url in self._cache:
            return self._cache[url]

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
                if response.status_code >= 500:
                    raise FetchError(f"HTTP {response.status_code}: {url}")
                if response.status_code >= 400:
                    # 4xx はリトライしても無駄なので即座に諦める
                    raise FetchError(f"HTTP {response.status_code}: {url}")
                response.encoding = response.encoding or "utf-8"
                text = response.text
                self._cache[url] = text
                time.sleep(self.delay)
                return text
            except FetchError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                log.warning("取得失敗 (%d/%d) %s: %s", attempt, self.retries, url, exc)
                time.sleep(min(2**attempt, 8))

        raise FetchError(f"取得に失敗しました: {url} ({last_error})")
