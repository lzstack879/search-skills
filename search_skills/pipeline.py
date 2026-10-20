'''Company resolution pipeline: ICP -> queries -> search -> judge -> fetch -> extract -> store.'''

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .client import DeepSeekClient, FetchPage, SearchHit
from .store import Store

logger = logging.getLogger('search_skills.pipeline')

PROMPTS_DIR = Path(__file__).resolve().parents[1] / 'prompts'

# Hosts that are never a company own website.
DIRECTORY_HOSTS = (
    'linkedin.com', 'crunchbase.com', 'glassdoor.com', 'indeed.com', 'wikipedia.org',
    'bloomberg.com', 'facebook.com', 'twitter.com', 'x.com', 'yelp.com', 'g2.com',
    'capterra.com', 'trustradius.com', 'getapp.com', 'zoominfo.com', 'apollo.io',
    'pitchbook.com', 'owler.com', 'dnb.com', 'zoominfo.com', 'medium.com',
    'reddit.com', 'quora.com', 'youtube.com', 'forbes.com', 'techcrunch.com',
    'gartner.com', 'cbinsights.com', 'wellfound.com', 'builtin.com', 'ycombinator.com',
    'dealroom.co', 'crustdata.com', 'signalhire.com', 'clutch.co', 'himalayas.app',
    'career.io', 'jobgether.com', 'leadiq.com', 'parsers.vc', 'producthunt.com',
    'upwork.com', 'f6s.com', 'tracxn.com', 'g2crowd.com', 'sourceforge.net',
    'salesforce.com', 'microsoft.com', 'google.com', 'amazon.com',
)

RANK_LABELS = ((75, '高'), (50, '中'), (0, '低'))


@dataclass
class ICP:
    '''Who we are looking for. Everything the prompts need lives here.'''

    industries: list[str] = field(default_factory=lambda: ['SaaS'])
    titles: list[str] = field(default_factory=lambda: ['VP Sales'])
    geography: list[str] = field(default_factory=lambda: ['United States'])
    company_size: str = '10-200 employees'
    product: str = ''
    signals: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        return '\n'.join([
            '行业: ' + ', '.join(self.industries),
            '目标职位: ' + ', '.join(self.titles),
            '地区: ' + ', '.join(self.geography),
            '公司规模: ' + self.company_size,
            '产品: ' + (self.product or '(未提供)'),
            '需求信号: ' + ('; '.join(self.signals) if self.signals else '(未提供，请从产品反推)'),
        ])

    def to_dict(self) -> dict:
        return {
            'industries': list(self.industries),
            'titles': list(self.titles),
            'geography': list(self.geography),
            'company_size': self.company_size,
            'product': self.product,
            'signals': list(self.signals),
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'ICP':
        return cls(
            industries=list(data.get('industries') or ['SaaS']),
            titles=list(data.get('titles') or ['VP Sales']),
            geography=list(data.get('geography') or ['United States']),
            company_size=str(data.get('company_size') or '10-200 employees'),
            product=str(data.get('product') or ''),
            signals=list(data.get('signals') or []),
        )


def load_prompt(name: str) -> str:
    '''Read one prompt file; missing prompts degrade to empty guidance.'''
    try:
        return (PROMPTS_DIR / (name + '.md')).read_text(encoding='utf-8')
    except OSError as e:
        logger.warning('prompt %s not readable: %s', name, e)
        return ''


def parse_json(text: str) -> dict:
    '''Pull the first JSON object out of a model reply, fences and prose included.'''
    if not text:
        return {}
    cleaned = re.sub(r'^\s*```(?:json)?|```\s*$', '', text.strip(), flags=re.MULTILINE)
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def host_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or '').lower()
    except ValueError:
        return ''
    return host[4:] if host.startswith('www.') else host


def is_directory(url: str) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith('.' + d) for d in DIRECTORY_HOSTS)


def rank_label(score: int) -> str:
    for threshold, label in RANK_LABELS:
        if score >= threshold:
            return label
    return '低'


EXCLUSIONS = '-site:indeed.com -site:glassdoor.com -site:crunchbase.com -jobs'


def heuristic_queries(icp: ICP, count: int = 8) -> list[str]:
    '''Deterministic fallback: rotate industries and titles through three shapes.'''
    queries: list[str] = []
    exclusions = '-site:indeed.com -site:stepstone.de -site:heyjobs. -site:glassdoor.com'
    for index in range(max(1, count)):
        industry = icp.industries[index % len(icp.industries)]
        title = icp.titles[index % len(icp.titles)]
        geo = icp.geography[index % len(icp.geography)]
        shape = index % 3
        if shape == 0:
            queries.append('"' + title + '" "' + industry + '" "' + geo + '" job ' + exclusions)
        elif shape == 1:
            queries.append(industry + ' company "' + geo + '" ' + exclusions)
        else:
            queries.append(industry + ' "' + geo + '" contact email ' + exclusions)
    # Dedupe while keeping order.
    seen = set()
    unique = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


TITLE_WORDS = ('head', 'vp', 'director', 'founder', 'chief', 'manager', 'owner', 'lead')


def heuristic_judge(hits: list[SearchHit]) -> dict:
    '''Pick the best non-directory hit; no model needed.'''
    candidates = [h for h in hits if h.url and not is_directory(h.url)]
    if not candidates:
        return {'is_company': False, 'reason': 'no non-directory result'}
    candidates.sort(key=lambda h: (h.authority, h.rerank_score), reverse=True)
    best = candidates[0]
    host = host_of(best.url)
    brand = host.split('.')[0] if host else ''
    name = brand[:1].upper() + brand[1:] if brand else ''
    return {
        'is_company': True,
        'company_name': name,
        'company_website': best.url,
        'confidence': 60,
        'reason': 'heuristic: highest authority non-directory result',
    }


EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
SKIP_EMAIL_SUFFIX = ('example.com', 'sentry.io', 'wixpress.com', '.png', '.jpg')


def extract_emails(markdown: str) -> list[str]:
    found: list[str] = []
    for raw in EMAIL_RE.findall(markdown or ''):
        email = raw.strip().strip('.,;:').lower()
        if any(email.endswith(s) for s in SKIP_EMAIL_SUFFIX):
            continue
        if email not in found:
            found.append(email)
    return found


def heuristic_extract(page: FetchPage, icp: ICP, company: str, query: str) -> dict:
    '''Deterministic extraction: everything comes straight from the Markdown.'''
    text = page.content or ''
    lowered = text.lower()

    # Drop summary when the page is just a cookie/privacy wall.
    junk_phrases = ('cookie policy', 'privacy policy', 'we use cookies', 'accept cookies',
                    'datenschutz', 'impressum', 'legal notice', 'terms of service')
    mostly_junk = sum(1 for phrase in junk_phrases if phrase in lowered) >= 2 and len(text) < 800

    emails = extract_emails(text)
    industry = ''
    for candidate in icp.industries:
        if candidate and candidate.lower() in lowered:
            industry = candidate
            break
    # First prose paragraph of reasonable length becomes the summary.
    summary = ''
    if not mostly_junk:
        for block in re.split(r'\n\s*\n', text):
            line = block.strip().lstrip('#').strip()
            if len(line) < 40 or line.startswith(('http', '![', '|')):
                continue
            # Skip blocks that are just cookie/privacy notices.
            if any(phrase in line.lower() for phrase in junk_phrases):
                continue
            summary = re.sub(r'\s+', ' ', line)[:120]
            break
    score = 0
    if industry:
        score += 40
    if emails:
        score += 15
    if any(word in lowered for word in TITLE_WORDS):
        score += 20
    if len(text) > 1500:
        score += 10
    if all(part in lowered for part in str(icp.company_size).replace('employees', '').split('-') if part.strip().isdigit()):
        score += 15
    label = rank_label(score)
    reason = '行业匹配' if industry else '未确认行业'
    if emails:
        reason += '，抓到邮箱'
    return {
        'company_name': company or page.title or '',
        'company_website': page.url,
        'company_perple': '',
        'company_email': emails[0] if emails else '',
        'industry': industry,
        'summary': summary,
        'match_result': label + '（' + str(score) + '/100）· ' + reason,
        'match_score': score,
        'confidence': 50,
    }


class Pipeline:
    '''Ties the prompts, the two DeepSeek endpoints, and the store together.'''

    def __init__(
        self,
        client: DeepSeekClient,
        store: Store,
        icp: ICP | None = None,
        *,
        use_llm: bool = True,
        per_query: int = 6,
        fetch_limit: int = 4,
        fetcher=None,
    ) -> None:
        self.client = client
        self.store = store
        self.fetcher = fetcher or client
        self.icp = icp or ICP()
        self.use_llm = use_llm
        self.per_query = per_query
        self.fetch_limit = fetch_limit

    async def plan(self, count: int = 8) -> list[str]:
        '''ICP -> queries, via the search_query prompt or the fallback.'''
        if self.use_llm:
            prompt = load_prompt('search_query')
            filled = (
                prompt
                + '\n\n---\n## 本次输入\n'
                + self.icp.to_text()
                + '\nCOUNT=' + str(count)
            )
            try:
                reply = await self.client.complete(filled, max_tokens=900)
                data = parse_json(reply)
                queries = [str(q).strip() for q in (data.get('queries') or []) if str(q).strip()]
                queries = [q for q in queries if 1 <= len(q) <= 128]
                if queries:
                    return queries[:count]
            except Exception as e:
                logger.warning('query planning via LLM failed, falling back: %s', e)
        return heuristic_queries(self.icp, count)

    async def judge(self, query: str, hits: list[SearchHit]) -> dict:
        '''Decide which hit is the company own site.'''
        usable = [h for h in hits if h.url and not is_directory(h.url)]
        if not usable:
            return {'is_company': False, 'reason': 'all results were directories'}
        if self.use_llm:
            prompt = load_prompt('search_judge')
            filled = (
                prompt
                + '\n\n---\n## 查询\n' + query
                + '\n\n## ICP\n' + self.icp.to_text()
                + '\n\n## 候选结果\n'
                + '\n\n'.join('[' + str(i + 1) + ']\n' + h.card() for i, h in enumerate(usable))
            )
            try:
                reply = await self.client.complete(filled, max_tokens=600)
                data = parse_json(reply)
                if data:
                    return data
            except Exception as e:
                logger.warning('judging via LLM failed, falling back: %s', e)
        return heuristic_judge(usable)

    async def extract(self, page: FetchPage, company: str, query: str) -> dict:
        '''Turn the fetched Markdown into the seven report fields.'''
        if self.use_llm:
            prompt = load_prompt('fetch_extract')
            filled = (
                prompt
                + '\n\n---\n## ICP\n' + self.icp.to_text()
                + '\n\n## 公司（判定结果）\n' + (company or '(未判定)')
                + '\n\n## 页面 Markdown\n' + (page.content or '')[:12000]
            )
            try:
                reply = await self.client.complete(filled, max_tokens=1200)
                data = parse_json(reply)
                if data.get('company_name') or data.get('summary'):
                    data.setdefault('company_website', page.url)
                    return data
            except Exception as e:
                logger.warning('extraction via LLM failed, falling back: %s', e)
        return heuristic_extract(page, self.icp, company, query)

    async def run(self, count: int = 8) -> dict:
        '''One full pass. Returns counters for logging/reporting.'''
        self.store.init()
        # Fail fast on a missing key rather than storing nothing silently.
        self.client.check_credentials()
        queries = await self.plan(count)
        stats = {
            'queries': len(queries),
            'hits': 0,
            'judged': 0,
            'fetched': 0,
            'inserted': 0,
            'updated': 0,
            'skipped': 0,
            'records': [],
        }
        fetched = 0
        for query in queries:
            try:
                hits = await self.client.search(query, self.per_query)
            except Exception as e:
                logger.error('search failed for %r: %s', query[:60], e)
                continue
            stats['hits'] += len(hits)
            if not hits:
                continue
            verdict = await self.judge(query, hits)
            stats['judged'] += 1
            if not verdict.get('is_company'):
                stats['skipped'] += 1
                continue
            if int(verdict.get('confidence') or 0) < 60:
                stats['skipped'] += 1
                continue
            url = str(verdict.get('company_website') or '').strip()
            if not url or is_directory(url):
                stats['skipped'] += 1
                continue
            if fetched >= self.fetch_limit:
                logger.info('fetch budget reached for this run')
                break
            try:
                page = await self.fetcher.fetch(url)
            except Exception as e:
                logger.error('fetch failed for %s: %s', url, e)
                continue
            fetched += 1
            stats['fetched'] += 1
            record = await self.extract(page, str(verdict.get('company_name') or ''), query)
            record['query'] = query
            record['source_url'] = url
            outcome = self.store.upsert(record)
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome != 'skipped':
                stats['records'].append(record)
        self.store.log_run(
            query='; '.join(queries)[:500],
            searched=stats['hits'],
            fetched=stats['fetched'],
            stored=stats['inserted'] + stats['updated'],
        )
        return stats
