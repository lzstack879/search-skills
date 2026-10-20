'''DeepSeek Search + Fetch — the only network backends this skill uses.

POST /v1/tools/search   {query, count, need_content, ori_question}
POST /v1/tools/fetch    {url}  ->  {title, content (Markdown)}
POST /chat/completions  used only to run this skill's prompts

Billed per successful request (5 CNY / 1000), so every call lands in a local
ledger. Nothing else here talks to a search provider.
'''

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

logger = logging.getLogger('search_skills.client')

SEARCH_URL = 'https://api.deepseek.com/v1/tools/search'
FETCH_URL = 'https://api.deepseek.com/v1/tools/fetch'
CHAT_URL = 'https://api.deepseek.com/chat/completions'
DEFAULT_MODEL = 'deepseek-chat'
PRICE_PER_1000 = 5.0

USAGE_PATH = Path(__file__).resolve().parents[1] / 'data' / 'usage.json'
RETRY_STATUS = {429, 500, 502, 503, 504}


class DeepSeekError(RuntimeError):
    '''A DeepSeek endpoint failed in a way the caller cannot ignore.'''


@dataclass
class SearchHit:
    '''One search result, mirroring DeepSeek response fields.'''

    title: str = ''
    url: str = ''
    snippet: str = ''
    summary: str = ''
    content: str = ''
    site_name: str = ''
    published: str = ''
    rerank_score: float = 0.0
    authority: int = -1

    @classmethod
    def from_json(cls, raw: dict) -> 'SearchHit':
        score = raw.get('rerank_score')
        authority = raw.get('authority')
        return cls(
            title=str(raw.get('title') or ''),
            url=str(raw.get('url') or ''),
            snippet=str(raw.get('snippet') or ''),
            summary=str(raw.get('summary') or ''),
            content=str(raw.get('content') or ''),
            site_name=str(raw.get('site_name') or ''),
            published=str(raw.get('date') or ''),
            rerank_score=float(score) if score is not None else 0.0,
            authority=int(authority) if authority is not None else -1,
        )

    def card(self, limit: int = 700) -> str:
        '''The block handed to a judging prompt.'''
        parts = [
            'URL: ' + self.url,
            'TITLE: ' + self.title,
            'SCORE: ' + str(self.rerank_score) + ' | AUTHORITY: ' + str(self.authority)
            + ' | SITE: ' + (self.site_name or '-'),
        ]
        body = self.summary or self.snippet
        if body:
            parts.append('TEXT: ' + body[:limit])
        return '\n'.join(parts)


@dataclass
class FetchPage:
    '''A fetched page rendered as Markdown.'''

    url: str = ''
    title: str = ''
    content: str = ''
    backend: str = ''

    @property
    def length(self) -> int:
        return len(self.content)


def _load_usage() -> dict:
    try:
        data = json.loads(USAGE_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_usage(usage: dict) -> None:
    try:
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        USAGE_PATH.write_text(
            json.dumps(usage, ensure_ascii=False, indent=1), encoding='utf-8'
        )
    except OSError as e:
        logger.debug('Could not persist usage ledger: %s', e)


def record_usage(kind: str) -> None:
    '''Add one successful call to the ledger and to today bucket.'''
    usage = _load_usage()
    today = date.today().isoformat()
    daily = usage.setdefault('daily', {})
    bucket = daily.setdefault(today, {'search': 0, 'fetch': 0})
    bucket[kind] = int(bucket.get(kind, 0)) + 1
    totals = usage.setdefault('total', {'search': 0, 'fetch': 0})
    totals[kind] = int(totals.get(kind, 0)) + 1
    calls = totals['search'] + totals['fetch']
    usage['cny'] = round(calls * PRICE_PER_1000 / 1000, 4)
    if len(daily) > 60:
        for stale in sorted(daily)[:-60]:
            daily.pop(stale, None)
    _save_usage(usage)


def usage_summary(day: str | None = None) -> dict:
    '''Calls made today (or on `day`) plus the lifetime total.'''
    usage = _load_usage()
    day = day or date.today().isoformat()
    today = (usage.get('daily') or {}).get(day, {'search': 0, 'fetch': 0})
    total = usage.get('total') or {'search': 0, 'fetch': 0}
    return {
        'day': day,
        'today_search': int(today.get('search', 0)),
        'today_fetch': int(today.get('fetch', 0)),
        'total_search': int(total.get('search', 0)),
        'total_fetch': int(total.get('fetch', 0)),
        'total_cny': float(usage.get('cny') or 0.0),
    }


class DeepSeekClient:
    '''Async client for the two DeepSeek tool endpoints, plus chat.'''

    def __init__(
        self,
        api_key: str = '',
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.api_key = (api_key or os.getenv('DEEPSEEK_API_KEY') or '').strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.model = model

    def _headers(self) -> dict:
        if not self.api_key:
            raise DeepSeekError(
                'DEEPSEEK_API_KEY is not set. DeepSeek Search/Fetch are the only '
                'backends this skill uses, so nothing can run without it.'
            )
        return {
            'Authorization': 'Bearer ' + self.api_key,
            'Content-Type': 'application/json',
        }

    def check_credentials(self) -> None:
        '''Raise early when the key is missing, before any query is spent.'''
        self._headers()

    async def _post(self, url: str, payload: dict) -> dict:
        headers = self._headers()
        last_error = ''
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except httpx.HTTPError as e:
                    last_error = 'transport error: ' + str(e)
                else:
                    if response.status_code == 200:
                        try:
                            return response.json()
                        except ValueError:
                            last_error = 'response was not JSON'
                    else:
                        last_error = 'HTTP ' + str(response.status_code) + ': ' + response.text[:300]
                        if response.status_code not in RETRY_STATUS:
                            raise DeepSeekError(last_error)
                if attempt < self.max_retries:
                    delay = 0.8 * (2 ** (attempt - 1))
                    logger.warning(
                        'DeepSeek retry %s/%s: %s', attempt, self.max_retries, last_error
                    )
                    await asyncio.sleep(delay)
        raise DeepSeekError(last_error or 'DeepSeek request failed')

    async def search(
        self,
        query: str,
        count: int = 10,
        *,
        need_content: bool = False,
        ori_question: str = '',
    ) -> list[SearchHit]:
        '''Find pages for one query. Supports site:/lang:/after: operators.'''
        query = (query or '').strip()
        if not query:
            return []
        if not 1 <= len(query) <= 128:
            raise DeepSeekError('query must be 1-128 chars, got ' + str(len(query)))
        payload = {
            'query': query,
            'count': max(1, min(int(count), 50)),
            'need_content': bool(need_content),
        }
        if ori_question:
            payload['ori_question'] = ori_question[:128]
        data = await self._post(SEARCH_URL, payload)
        rows = data.get('results') or []
        hits = [SearchHit.from_json(r) for r in rows if isinstance(r, dict)]
        record_usage('search')
        return hits

    async def fetch(self, url: str) -> FetchPage:
        '''Fetch one page as Markdown (the replacement for HTML scraping).'''
        url = (url or '').strip()
        if not url.lower().startswith(('http://', 'https://')):
            raise DeepSeekError('not an http(s) URL: ' + url[:80])
        data = await self._post(FETCH_URL, {'url': url[:8192]})
        record_usage('fetch')
        return FetchPage(
            url=url,
            title=str(data.get('title') or ''),
            content=str(data.get('content') or ''),
        )

    async def complete(
        self,
        prompt: str,
        *,
        system: str = '',
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> str:
        '''One-shot completion used to run this skill prompts.'''
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        data = await self._post(
            CHAT_URL,
            {
                'model': self.model,
                'messages': messages,
                'temperature': temperature,
                'max_tokens': max_tokens,
                'stream': False,
            },
        )
        choices = data.get('choices') or []
        if not choices:
            return ''
        message = choices[0].get('message') or {}
        return str(message.get('content') or '').strip()
