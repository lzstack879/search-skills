'''Seed pages: exhibitor rosters and association member lists -> seeds.csv.

Most exhibitor pages name hundreds of companies but only some carry a website
link, so this module keeps names and attaches domains only when the page (or a
follow-up search, with --resolve) actually shows one.
'''

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import re

from .client import DeepSeekClient
from .pipeline import ICP, DIRECTORY_HOSTS, host_of, is_directory, load_prompt, parse_json
from .store import Store

logger = logging.getLogger('search_skills.seedpages')

NAV_LABELS = {
    'login', 'register', 'contact', 'imprint', 'impressum', 'privacy', 'privacy policy',
    'terms', 'press', 'news', 'home', 'search', 'hall plan', 'tickets', 'programme',
    'exhibitor list', 'exhibitors', 'visit', 'about', 'faq', 'newsletter',
}


def clean_company(name: str) -> str:
    text = ' '.join(str(name or '').replace('*', '').split())
    text = re.sub(r'\s+[|–—]\s+.*$', '', text)
    return text.strip(' .,-:|')[:80]


def links_from_markdown(markdown: str, page_host: str) -> dict[str, str]:
    '''Company-ish links found inside the page: label -> absolute URL.'''
    found: dict[str, str] = {}
    for label, url in re.findall(r'\[([^\]]{2,60})\]\((https?://[^)\s]+)\)', markdown or ''):
        label = clean_company(label)
        if not label or label.lower() in NAV_LABELS:
            continue
        host = host_of(url)
        if not host or is_directory(url) or host == page_host:
            continue
        found.setdefault(label.lower(), url)
    return found


def build_seed_rows(companies: list[dict], links: dict[str, str],
                    source_url: str) -> list[dict]:
    '''Merge model output with page links; keep name-only rows for later resolve.'''
    rows: list[dict] = []
    seen: set[str] = set()
    for company in companies:
        if not isinstance(company, dict):
            continue
        name = clean_company(company.get('name'))
        if not name or len(name) < 2:
            continue
        website = str(company.get('website') or '').strip()
        if not website:
            website = links.get(name.lower(), '')
        domain = host_of(website)
        if domain and ('.' not in domain or domain in DIRECTORY_HOSTS):
            domain = ''
        key = domain or ('name:' + name.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            'company': name,
            'domain': domain,
            'country': str(company.get('country') or '').strip(),
            'industry': str(company.get('industry') or '').strip(),
            'source_url': source_url,
        })
    return rows


def diagnose_page(text: str, url: str) -> str:
    '''Explain why a roster page produced nothing, with the next step to take.'''
    if len(text or '') < 500:
        return (
            'looks like a JS-rendered app (only ' + str(len(text or '')) + ' chars). '
            'Try: (1) --wait-for ".exhibitor-list, table, main" to let it render, '
            '(2) the fair JSON endpoint behind the search box, '
            '(3) the exhibitor PDF catalogue, or (4) paste a static exhibitor list URL.'
        )
    if not re.search(r'\[[^\]]{2,60}\]\(https?://', text or ''):
        return (
            'page has text but no company links; exhibitors are probably name-only. '
            'Use --resolve to look each name up, or take the official catalogue.'
        )
    return ''


class SeedPageReader:
    '''Reads a roster page and turns it into seed rows.'''

    def __init__(self, client: DeepSeekClient | None = None,
                 fetcher=None, store: Store | None = None) -> None:
        self.client = client or DeepSeekClient()
        self.fetcher = fetcher or self.client
        self.store = store or Store()
        self.last_diagnosis = ''

    async def read_page(self, url: str, *, limit: int = 400,
                        icp: ICP | None = None, wait_for: str = '') -> list[dict]:
        page = await self.fetcher.fetch(url, wait_for=wait_for or None) if wait_for \
            else await self.fetcher.fetch(url)
        text = page.content or ''
        self.last_diagnosis = diagnose_page(text, url)
        if len(text.strip()) < 200:
            logger.warning('page too thin: %s', url)
            return []
        prompt = (
            load_prompt('mine_exhibitors')
            + '\n\n---\n## ICP\n' + (icp or ICP()).to_text()
            + '\nLIMIT=' + str(limit)
            + '\n\n## 页面 URL\n' + url
            + '\n\n## 页面 Markdown\n' + text[:20000]
        )
        data = parse_json(await self.client.complete(prompt, max_tokens=2400))
        companies = data.get('companies') or []
        links = links_from_markdown(text, host_of(url))
        return build_seed_rows(companies, links, url)

    async def find_pages(self, query: str, pages: int = 3) -> list[str]:
        hits = await self.client.search(query, 8)
        out: list[str] = []
        for hit in hits:
            if not hit.url or is_directory(hit.url):
                continue
            out.append(hit.url)
            if len(out) >= pages:
                break
        return out

    async def resolve_domains(self, rows: list[dict], limit: int = 20) -> list[dict]:
        '''One search per name-only row; returns only rows that ended up with a domain.'''
        '''One search per row for the ones the page did not link.'''
        from .leadgen import host_score

        done = 0
        for row in rows:
            if row.get('domain') or done >= limit:
                continue
            try:
                hits = await self.client.search('"' + row['company'] + '" official website', 5)
            except Exception:
                continue
            done += 1
            for hit in hits:
                if not hit.url or is_directory(hit.url):
                    continue
                if host_score(host_of(hit.url), row['company']) >= 0.6:
                    row['domain'] = host_of(hit.url)
                    break
        return [r for r in rows if r.get('domain')]


def json_rows(data, *, path: str = '', name_key: str = 'name',
              domain_key: str = 'website') -> list[dict]:
    '''Pull company rows out of a fair's JSON payload.'''
    items = data
    for part in [p for p in (path or '').split('.') if p]:
        if isinstance(items, dict):
            items = items.get(part)
        else:
            return []
    if not isinstance(items, list):
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = clean_company(item.get(name_key))
        website = str(item.get(domain_key) or '').strip()
        domain = host_of(website)
        if not name or not domain or '.' not in domain or domain in seen:
            continue
        seen.add(domain)
        rows.append({'company': name, 'domain': domain,
                     'country': str(item.get('country') or '').strip(),
                     'industry': str(item.get('industry') or '').strip(),
                     'source_url': ''})
    return rows


def fetch_json(url: str, *, timeout: float = 20.0):
    '''Cheap local GET for exhibitor APIs (no DeepSeek credits).'''
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={'User-Agent': 'Mozilla/5.0 (compatible; seedpages/1.0)',
                                  'Accept': 'application/json'})
    response.raise_for_status()
    return response.json()


def write_csv(rows: list[dict], path: str) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['company', 'domain', 'country', 'industry'])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, '') for k in
                             ('company', 'domain', 'country', 'industry')})
    return len(rows)
