'''Page fetching with two backends.

Why two: DeepSeek Fetch is a cloud GET behind a gateway -- it returns 504 on
roughly a quarter of calls and cannot render single-page apps. crawl4ai renders
JS locally for free, but it uses this machine's IP, so bot-protected sites still
fail. Order: local first (free, renders JS), DeepSeek as the fallback (different
IP pool, costs 0.005 per page).
'''

from __future__ import annotations

import logging

from .client import DeepSeekClient, FetchPage

logger = logging.getLogger('search_skills.fetcher')

DEFAULT_MIN_CHARS = 500


def _markdown_of(result) -> str:
    '''CrawlResult.markdown is either a string or a generation object.'''
    markdown = getattr(result, 'markdown', '') or ''
    for attribute in ('raw_markdown', 'fit_markdown'):
        value = getattr(markdown, attribute, '')
        if value:
            return str(value)
    return markdown if isinstance(markdown, str) else str(markdown)


def _title_of(result) -> str:
    title = getattr(result, 'title', '') or ''
    if title:
        return str(title)
    metadata = getattr(result, 'metadata', None) or {}
    if isinstance(metadata, dict):
        return str(metadata.get('title') or '')
    return ''


class LocalFetcher:
    '''crawl4ai + Playwright, started lazily and kept warm for the whole run.'''

    name = 'local'

    def __init__(self, timeout_ms: int = 30000) -> None:
        self.timeout_ms = timeout_ms
        self._crawler = None

    async def start(self) -> 'LocalFetcher':
        if self._crawler is not None:
            return self
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        self._crawler = AsyncWebCrawler(
            config=BrowserConfig(headless=True, verbose=False)
        )
        await self._crawler.start()
        return self

    async def close(self) -> None:
        if self._crawler is not None:
            try:
                await self._crawler.close()
            except Exception as e:
                logger.debug('crawler close failed: %s', e)
            self._crawler = None

    async def fetch(self, url: str, *, wait_for: str | None = None) -> FetchPage:
        if self._crawler is None:
            await self.start()
        from crawl4ai import CrawlerRunConfig

        kwargs = dict(page_timeout=self.timeout_ms, wait_until='domcontentloaded',
                      verbose=False)
        if wait_for:
            kwargs['wait_for'] = wait_for
        config = CrawlerRunConfig(**kwargs)
        result = await self._crawler.arun(url=url, config=config)
        if not getattr(result, 'success', False):
            raise RuntimeError(str(getattr(result, 'error_message', 'crawl failed'))[:200])
        return FetchPage(
            url=url,
            title=_title_of(result),
            content=_markdown_of(result),
            backend='local',
        )


class Fetcher:
    '''Try the local renderer, then DeepSeek Fetch; keep the longer page.'''

    def __init__(
        self,
        client: DeepSeekClient | None = None,
        *,
        local: bool = True,
        min_chars: int = DEFAULT_MIN_CHARS,
        timeout_ms: int = 30000,
    ) -> None:
        self.client = client or DeepSeekClient()
        self.min_chars = min_chars
        self.local = LocalFetcher(timeout_ms) if local else None
        self.stats = {
            'local_ok': 0, 'local_short': 0, 'local_fail': 0,
            'cloud_ok': 0, 'cloud_fail': 0,
        }

    async def __aenter__(self) -> 'Fetcher':
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self.local is not None:
            await self.local.close()

    async def fetch(self, url: str, *, wait_for: str | None = None) -> FetchPage:
        best: FetchPage | None = None

        if self.local is not None:
            try:
                page = await self.local.fetch(url, wait_for=wait_for)
            except Exception as e:
                self.stats['local_fail'] += 1
                logger.info('local fetch failed for %s: %s', url, str(e)[:120])
            else:
                if len(page.content or '') >= self.min_chars:
                    self.stats['local_ok'] += 1
                    return page
                self.stats['local_short'] += 1
                best = page

        try:
            page = await self.client.fetch(url)
        except Exception:
            self.stats['cloud_fail'] += 1
            if best is not None:
                return best
            raise
        self.stats['cloud_ok'] += 1
        if best is not None and len(best.content or '') >= len(page.content or ''):
            return best
        return page


async def with_fetcher(factory, client: DeepSeekClient | None = None, **kwargs):
    '''Run an async callable with a ready Fetcher, then shut the browser down.'''
    fetcher = Fetcher(client=client, **kwargs)
    try:
        return await factory(fetcher)
    finally:
        await fetcher.close()
