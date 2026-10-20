'''Lead generation: mine company names from list pages, then resolve each to its own site.

Two stages, both inside DeepSeek Search / Fetch:
  1. topic  -> search -> fetch a LIST page (rankings, directories, funding news)
  2. mine names -> resolve each name to its own homepage -> fetch -> extract -> store
'''

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .client import DeepSeekClient, FetchPage, SearchHit, usage_summary
from .emailver import classify
from .pipeline import (
    ICP,
    Pipeline,
    extract_emails,
    host_of,
    is_directory,
    load_prompt,
    parse_json,
)
from .store import Store

logger = logging.getLogger('search_skills.leadgen')

DATA_DIR = Path(__file__).resolve().parents[1] / 'data'
SEEN_PATH = DATA_DIR / 'seen.json'
STOP_PATH = DATA_DIR / 'STOP'

# Words that make a page worth fetching: it probably names several companies.
LIST_HINTS = (
    'hiring', 'jobs', 'job board', 'careers', 'tender', 'rfp', 'procurement',
    'exhibitor', 'companies', 'company', 'startup', 'directory', 'list of',
    'funding', 'raised', 'ecosystem', '参展', '招聘', '招标', '采购', '名单', '名录',
)

STOP_TOKENS = {'the', 'and', 'inc', 'llc', 'ltd', 'group', 'labs', 'co', 'corp', 'io', 'ai', 'app'}

# Page furniture that models happily report as if it were a company name.
NAV_WORDS = {
    'about this website', 'about us', 'privacy policy', 'cookie policy', 'terms of service',
    'terms and conditions', 'contact us', 'login', 'sign in', 'sign up', 'register',
    'sitemap', 'imprint', 'impressum', 'careers', 'jobs', 'newsletter', 'blog', 'home',
    # same furniture in the languages the EU runs actually return
    'over deze website', 'over ons', 'privacybeleid', 'voorwaarden', 'contact',
    'uber diese website', 'über diese website', 'über uns', 'datenschutz', 'agb',
    'mentions légales', 'a propos', 'à propos', 'nous contacter', 'conditions générales',
}

NAV_PREFIXES = ('contact ', 'over ', 'about ', 'über ', 'uber ', 'à propos', 'a propos',
                'privacy', 'impressum', 'datenschutz', 'cookies', 'sitemap', 'login')

# Public bodies, associations, consultancies are not manufacturing buyers.
PUBLIC_RE = re.compile(
    r'\.(gov|gouv|overheid|bund|mil)(\.[a-z]{2})?$|gemeente|stadt|kommune|mairie|'
    r'ministry|ministerie|ministerium|municipality|city of|'
    r'\bverband\b|\bverein\b|\bassociation\b|\bchamber\b|\binstitut[e]?\b|'
    r'\bconsulting\b|\bconsultancy\b|\badvisory\b|\bberatung\b',
    re.I,
)

NON_BUYER_INDUSTRIES = {'Consulting', 'Advisory', 'Training', 'Media', 'Association', 'Government'}


def is_public_sector(name: str, domain: str) -> bool:
    return bool(PUBLIC_RE.search((name or '').lower() + ' ' + (domain or '').lower()))


def is_non_buyer(record: dict) -> bool:
    '''Consulting, associations, government are not manufacturing buyers.'''
    if is_public_sector(record.get('company_name', ''), record.get('domain', '')):
        return True
    industry = str(record.get('industry', '')).strip()
    return industry in NON_BUYER_INDUSTRIES

DOMAIN_RE = re.compile(r'^(https?://)?(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$', re.I)


def looks_like_junk_name(name: str) -> bool:
    '''Reject navigation text and bare domains pretending to be companies.'''
    lowered = (name or '').strip().lower()
    if not lowered:
        return True
    if lowered in NAV_WORDS:
        return True
    if lowered.startswith(NAV_PREFIXES):
        return True
    if DOMAIN_RE.match(lowered) and ' ' not in lowered:
        return True
    return False


def norm_name(raw: str) -> str:
    '''Trim a mined name: no pipes, no legal suffixes, no leading bullets.'''
    name = ' '.join(str(raw or '').replace('*', '').split())
    name = re.sub(r'\s+[|–—]\s+.*$', '', name)
    name = re.sub(r'[（(].*?[)）]\s*$', '', name)
    name = re.sub(r'\b(Inc|LLC|Ltd|Limited|GmbH|Co|Corp|Corporation|Pvt)\b\.?$', '', name, flags=re.I)
    name = name.strip(' .,-:|')
    return name if len(name) <= 60 else ''


def has_words(name: str) -> bool:
    '''True when a name holds letters/digits in any script (Chinese included).'''
    return any(ch.isalnum() for ch in (name or ''))


def name_tokens(name: str) -> list[str]:
    tokens = re.split(r'[^a-z0-9]+', (name or '').lower())
    out = []
    for token in tokens:
        if len(token) >= 3 and token not in STOP_TOKENS and token not in out:
            out.append(token)
    return out


def host_score(host: str, name: str) -> float:
    '''How much a host looks like it belongs to a company name (0 unrelated, 1 exact).'''
    label = (host or '').lower().split('.')[0].replace('-', '')
    tokens = name_tokens(name)
    if not label or not tokens:
        return 0.0
    first = tokens[0]
    joined = ''.join(tokens)
    if label == joined or label == first:
        return 1.0
    if len(first) >= 4 and (label.startswith(first) or first.startswith(label)):
        return 0.85
    if len(label) >= 4 and (joined.startswith(label) or label in joined):
        return 0.7
    if len(first) >= 5 and first in label:
        return 0.6
    return 0.0


@dataclass
class Candidate:
    '''One company name pulled out of a list page.'''

    name: str
    hint: str = ''
    source: str = ''
    role: str = 'unknown'


class Seen:
    '''Names and hosts already paid for, so a long run never repeats itself.'''

    def __init__(self, path: Path = SEEN_PATH) -> None:
        self.path = path
        self.names: set[str] = set()
        self.hosts: set[str] = set()
        self.queries: list[str] = []
        self.round: int = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        self.names = {str(x) for x in (data.get('names') or [])}
        self.hosts = {str(x) for x in (data.get('hosts') or [])}
        self.queries = [str(x) for x in (data.get('queries') or [])]
        try:
            self.round = int(data.get('round') or 0)
        except (TypeError, ValueError):
            self.round = 0

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'names': sorted(self.names)[-8000:],
                'hosts': sorted(self.hosts)[-8000:],
                'queries': self.queries[-500:],
                'round': self.round,
                'updated': date.today().isoformat(),
            }
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
        except OSError as e:
            logger.warning('could not save seen list: %s', e)


# One signal family per round, so consecutive rounds never ask the same thing.
SIGNAL_BUCKETS = (
    ('招聘信号（在招人 = 现在没人干）', 'hiring'),
    ('招标/采购公告（预算已批）', 'tender'),
    ('展会/协会参展商名单（一页几十家）', 'expo'),
    ('融资/扩张新闻（刚有钱，最缺系统）', 'funding'),
    ('政府/行业名录（专精特新、500 强）', 'directory'),
)


def heuristic_topics(icp: ICP, count: int = 4) -> list[str]:
    '''Deterministic topic rotation when the model is unavailable: buyer signals only.'''
    year = str(date.today().year)
    industry = icp.industries[0] if icp.industries else 'SaaS'
    geo = icp.geography[0] if icp.geography else 'United States'
    chinese = any('中国' in g or 'china' in g.lower() for g in icp.geography)
    if chinese:
        shapes = [
            '"' + industry + '" 招聘 ' + year + ' ' + geo,
            '招标 ' + industry + ' 项目 ' + year + ' 公告',
            year + ' 工业博览会 参展商名单',
            '"' + industry + '" 企业 融资 ' + year + ' ' + geo,
            year + ' 专精特新 名单 ' + geo,
            '"' + industry + '" 采购 公告 ' + year,
        ]
    else:
        shapes = [
            '"' + industry + '" hiring ' + year + ' "' + geo + '"',
            '"' + industry + '" tender ' + year + ' "' + geo + '"',
            year + ' "' + geo + '" exhibitor list ' + industry,
            '"' + industry + '" funding round ' + year + ' "' + geo + '"',
            'directory "' + industry + '" companies ' + year + ' "' + geo + '"',
        ]
    out: list[str] = []
    for index in range(max(1, count)):
        query = shapes[index % len(shapes)][:128]
        if query not in out:
            out.append(query)
    return out


def heuristic_mine(page: FetchPage, source: str, limit: int = 12) -> list[Candidate]:
    '''Fallback mining: markdown links and bold headings that look like brands.'''
    text = page.content or ''
    found: list[Candidate] = []
    seen: set[str] = set()
    own_host = host_of(page.url or source)

    for label, url in re.findall(r'\[([^\]]{2,60})\]\((https?://[^)\s]+)\)', text):
        host = host_of(url)
        if not host or host == own_host or is_directory(url):
            continue
        name = norm_name(label)
        if not name or not re.match(r'^[A-Z0-9]', name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(Candidate(name, '', source))
        if len(found) >= limit:
            return found

    for line in text.splitlines():
        line = line.strip()
        match = re.match(r'^#{2,4}\s+([A-Z][\w&.\- ]{1,40})$', line)
        if not match:
            match = re.match(r'^[\-\*]\s+\*\*([A-Z][\w&.\- ]{1,40})\*\*', line)
        if not match:
            continue
        name = norm_name(match.group(1))
        key = name.lower()
        if not name or key in seen or not has_words(name) or looks_like_junk_name(name):
            continue
        seen.add(key)
        found.append(Candidate(name, '', source))
        if len(found) >= limit:
            break
    return found


def parse_seeds_csv(path: str) -> list[dict]:
    '''Read a directory-style seed list: company/domain plus optional columns.'''
    import csv

    rows: list[dict] = []
    seen: set[str] = set()
    with open(path, encoding='utf-8-sig', newline='') as handle:
        for raw in csv.DictReader(handle):
            domain = str(raw.get('domain') or raw.get('website') or '').strip().lower()
            domain = re.sub(r'^https?://', '', domain).split('/')[0]
            domain = domain[4:] if domain.startswith('www.') else domain
            if not domain or '.' not in domain or domain in seen:
                continue
            seen.add(domain)
            rows.append({
                'name': str(raw.get('company') or raw.get('name') or domain).strip(),
                'domain': domain,
                'country': str(raw.get('country') or '').strip(),
                'industry': str(raw.get('industry') or '').strip(),
            })
    return rows


class LeadGen:
    '''Runs one mining round: topics -> list pages -> names -> homepages -> store.'''

    def __init__(
        self,
        client: DeepSeekClient,
        store: Store,
        icp: ICP | None = None,
        *,
        use_llm: bool = True,
        per_topic: int = 8,
        pages_per_topic: int = 2,
        fetch_limit: int = 4,
        daily_budget: int = 400,
        seen: Seen | None = None,
        fetcher=None,
    ) -> None:
        self.client = client
        self.store = store
        self.fetcher = fetcher or client
        self.icp = icp or ICP()
        self.use_llm = use_llm
        self.per_topic = per_topic
        self.pages_per_topic = pages_per_topic
        self.fetch_limit = fetch_limit
        self.daily_budget = daily_budget
        self.seen = seen or Seen()
        self.known: set[str] = set()
        self.pipeline = Pipeline(client, store, self.icp, use_llm=use_llm, fetcher=self.fetcher)

    # -- budget ---------------------------------------------------------

    def calls_today(self) -> int:
        usage = usage_summary()
        return usage['today_search'] + usage['today_fetch']

    def budget_left(self) -> int:
        return self.daily_budget - self.calls_today()

    # -- stage 1: topics and list pages ---------------------------------

    def focus(self) -> tuple[str, str, str]:
        '''This round's signal family and country, rotated by round number.'''
        label, _key = SIGNAL_BUCKETS[self.seen.round % len(SIGNAL_BUCKETS)]
        geos = self.icp.geography or ['United States']
        geo = geos[self.seen.round % len(geos)]
        return label, geo, _key

    async def plan_topics(self, count: int = 4) -> list[str]:
        label, geo, _key = self.focus()
        recent = self.seen.queries[-40:]
        if self.use_llm and load_prompt('search_topics'):
            prompt = (
                load_prompt('search_topics')
                + '\n\n---\n## 本次输入\n' + self.icp.to_text()
                + '\n\n## 本轮聚焦（必须遵守）'
                + '\n信号类型：' + label
                + '\n地区：' + geo
                + '\nCOUNT=' + str(count)
            )
            if recent:
                prompt += '\n\n## 已用过的查询（禁止重复）\n' + '\n'.join(recent)
            try:
                reply = await self.client.complete(prompt, max_tokens=800)
                data = parse_json(reply)
                queries = [str(q).strip() for q in (data.get('queries') or []) if str(q).strip()]
                queries = [q[:128] for q in queries if q]
                queries = [q for q in queries if q not in self.seen.queries]
                if queries:
                    return queries[:count]
            except Exception as e:
                logger.warning('topic planning via LLM failed: %s', e)
        fallback = [q for q in heuristic_topics(self.icp, count + 4)
                    if q not in self.seen.queries]
        return fallback[:count]

    def _page_budget(self, hits: list[SearchHit]) -> int:
        '''List pages (exhibitor rosters, directories) get more pages per topic.'''
        budget = self.pages_per_topic
        title = (hits[0].title or '').lower() if hits else ''
        if any(word in title for word in
               ('exhibitor', '参展', 'aussteller', 'list', '名单', 'directory', '名录')):
            budget = min(budget + 2, 4)
        return budget

    def _minable(self, hits: list[SearchHit]) -> list[SearchHit]:
        '''Rank hits by how likely the page names many companies.'''
        scored = []
        used: set[str] = set()
        for hit in hits:
            if not hit.url or is_directory(hit.url):
                continue
            host = host_of(hit.url)
            if not host or host in used:
                continue
            used.add(host)
            title = (hit.title or '').lower()
            score = sum(1 for word in LIST_HINTS if word in title)
            scored.append((score, hit))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        strong = [hit for score, hit in scored if score > 0]
        return strong or [hit for _score, hit in scored][:2]

    # -- stage 2: names out of a page -----------------------------------

    async def mine(self, page: FetchPage, source: str, limit: int = 12) -> list[Candidate]:
        text = (page.content or '')[:12000]
        if len(text.strip()) < 200:
            return []
        if self.use_llm and load_prompt('mine_names'):
            prompt = (
                load_prompt('mine_names')
                + '\n\n---\n## ICP\n' + self.icp.to_text()
                + '\nLIMIT=' + str(limit)
                + '\n\n## 页面 URL\n' + source
                + '\n\n## 页面 Markdown\n' + text
            )
            try:
                reply = await self.client.complete(prompt, max_tokens=1200)
                rows = parse_json(reply).get('companies') or []
                out: list[Candidate] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = norm_name(row.get('name'))
                    if not name or not has_words(name) or looks_like_junk_name(name):
                        continue
                    role = str(row.get('role') or 'unknown').strip().lower()
                    if role not in ('buyer', 'vendor', 'unknown'):
                        role = 'unknown'
                    out.append(Candidate(name, str(row.get('hint') or '')[:80], source, role))
                if out:
                    return out[:limit]
            except Exception as e:
                logger.warning('mining via LLM failed, falling back: %s', e)
        return heuristic_mine(page, source, limit)

    # -- stage 3: name -> own homepage ----------------------------------

    async def resolve(self, name: str) -> SearchHit | None:
        try:
            hits = await self.client.search('"' + name + '" official website', 6)
        except Exception as e:
            logger.warning('resolve search failed for %s: %s', name, e)
            return None
        best: SearchHit | None = None
        best_score = 0.0
        for hit in hits:
            if not hit.url or is_directory(hit.url):
                continue
            score = host_score(host_of(hit.url), name)
            if score > best_score:
                best_score = score
                best = hit
        if best is not None and best_score >= 0.6:
            return best
        # Chinese (and other non-latin) names cannot match a host; trust the title.
        lowered = (name or '').strip().lower()
        if lowered and not name_tokens(name):
            for hit in hits:
                if not hit.url or is_directory(hit.url):
                    continue
                if lowered in (hit.title or '').lower():
                    return hit
        return None

    # -- the round ------------------------------------------------------

    def _fits_icp(self, record: dict) -> bool:
        '''Drop what the ICP clearly rules out: low score, or foreign industry.'''
        score = int(record.get('match_score') or 0)
        if score < 30:
            return False
        industry = str(record.get('industry') or '').strip().lower()
        allowed = [item.strip().lower() for item in self.icp.industries]
        if industry and industry not in allowed and score < 40:
            return False
        return True

    async def seed_round(self, seeds: list[dict], limit: int = 50) -> dict:
        '''Same harvest, but the starting list comes from a file, not from search.'''
        self.store.init()
        self.client.check_credentials()
        self.known = self.store.dedupe_keys()
        stats = {'topics': 0, 'pages': 0, 'candidates': len(seeds), 'vendor': 0,
                 'resolved': len(seeds), 'fetched': 0, 'inserted': 0, 'updated': 0,
                 'dropped': 0, 'skipped': 0, 'records': [], 'queries': ['seeds.csv']}
        for seed in seeds[:limit]:
            domain = seed['domain']
            root = 'https://' + domain
            if root in self.seen.hosts or ('web:' + root).lower() in self.known:
                stats['skipped'] += 1
                continue
            try:
                page = await self.fetcher.fetch(root)
            except Exception as e:
                logger.warning('seed fetch failed for %s: %s', root, str(e)[:80])
                stats['skipped'] += 1
                continue
            stats['fetched'] += 1
            record = await self.pipeline.extract(page, seed.get('name') or domain, 'seed')
            record['company_name'] = norm_name(record.get('company_name') or '') or seed['name']
            record['company_website'] = root
            record['domain'] = domain
            record['industry'] = record.get('industry') or seed.get('industry', '')
            record['query'] = 'seed:' + (seed.get('country') or '')
            record['source_url'] = 'seeds.csv'
            email = str(record.get('company_email') or '').strip()
            if email:
                kind, _reason = classify(domain, email)
                if kind == 'blocked':
                    record['company_email'] = ''
                    email = ''
                else:
                    record['email_kind'] = kind
            record['email_status'] = 'found' if email else 'pending'
            record['email_source'] = 'extract' if email else ''
            if not self._fits_icp(record) or is_non_buyer(record):
                stats['dropped'] += 1
                continue
            self.seen.hosts.add(root)
            outcome = self.store.upsert(record)
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome != 'skipped':
                self.known.add(('web:' + root).lower())
                stats['records'].append(record)
        self.seen.save()
        if hasattr(self.fetcher, 'stats'):
            stats['fetch'] = dict(self.fetcher.stats)
        self.store.log_run(query='seeds.csv ' + str(len(seeds)), searched=0,
                           fetched=stats['fetched'],
                           stored=stats['inserted'] + stats['updated'])
        return stats

    async def round(self, count: int = 4) -> dict:
        self.store.init()
        self.client.check_credentials()
        self.known = self.store.dedupe_keys()

        stats = {
            'topics': 0, 'pages': 0, 'candidates': 0, 'resolved': 0,
            'fetched': 0, 'inserted': 0, 'updated': 0, 'skipped': 0, 'dropped': 0,
            'vendor': 0,
            'records': [], 'queries': [],
        }
        topics = await self.plan_topics(count)
        stats['topics'] = len(topics)
        stats['queries'] = topics
        stats['focus'] = self.focus()[0] + ' @ ' + self.focus()[1]
        self.seen.queries.extend(topics)
        self.seen.round += 1

        pool: list[Candidate] = []
        seen_names: set[str] = set()
        for topic in topics:
            if self.budget_left() < 6:
                break
            try:
                hits = await self.client.search(topic, self.per_topic)
            except Exception as e:
                logger.error('topic search failed for %r: %s', topic[:60], e)
                continue
            minable = self._minable(hits)
            for hit in minable[: self._page_budget(minable)]:
                if self.budget_left() < 5:
                    break
                try:
                    page = await self.fetcher.fetch(hit.url)
                except Exception as e:
                    logger.warning('list page fetch failed for %s: %s', hit.url, e)
                    continue
                stats['pages'] += 1
                for cand in await self.mine(page, hit.url):
                    if cand.role == 'vendor':
                        stats['vendor'] = stats.get('vendor', 0) + 1
                        continue
                    key = cand.name.lower()
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    pool.append(cand)

        stats['candidates'] = len(pool)
        logger.info(
            'round: %d topics, %d list pages, %d candidate names',
            stats['topics'], stats['pages'], stats['candidates'],
        )

        for cand in pool:
            if stats['fetched'] >= self.fetch_limit or self.budget_left() < 4:
                break
            key = cand.name.lower()
            if key in self.seen.names:
                stats['skipped'] += 1
                continue
            hit = await self.resolve(cand.name)
            self.seen.names.add(key)
            if hit is None:
                stats['skipped'] += 1
                continue
            stats['resolved'] += 1
            root = 'https://' + host_of(hit.url)
            if root in self.seen.hosts or ('web:' + root).lower() in self.known:
                stats['skipped'] += 1
                continue
            try:
                page = await self.fetcher.fetch(root)
            except Exception as e:
                logger.warning('homepage fetch failed for %s: %s', root, e)
                continue
            self.seen.hosts.add(root)
            stats['fetched'] += 1
            record = await self.pipeline.extract(page, str(hit.title or cand.name), cand.name)
            record['company_name'] = norm_name(record.get('company_name') or '') or cand.name
            record['company_website'] = root
            record['domain'] = host_of(root)
            email = str(record.get('company_email') or '').strip()
            if email:
                kind, _reason = classify(record['domain'], email)
                if kind == 'blocked':
                    record['company_email'] = ''
                    email = ''
                else:
                    record['email_kind'] = kind
            record['email_status'] = 'found' if email else 'pending'
            record['email_source'] = 'extract' if email else ''
            if not self._fits_icp(record) or is_non_buyer(record):
                stats['dropped'] = stats.get('dropped', 0) + 1
                continue
            record['query'] = cand.name
            record['source_url'] = cand.source or hit.url
            if not record.get('summary') and cand.hint:
                record['summary'] = cand.hint
            outcome = self.store.upsert(record)
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome != 'skipped':
                self.known.add(('web:' + root).lower())
                stats['records'].append(record)

        self.seen.save()
        if hasattr(self.fetcher, 'stats'):
            stats['fetch'] = dict(self.fetcher.stats)
        self.store.log_run(
            query='; '.join(topics)[:500],
            searched=stats['pages'] + stats['resolved'],
            fetched=stats['fetched'],
            stored=stats['inserted'] + stats['updated'],
        )
        return stats
