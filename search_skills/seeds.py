'''Seed generation: let the model propose real companies, then verify before use.

The model is good at "who exists in industry X", bad at "who needs us right now".
So seeds are only a starting list: every domain is MX-checked, and the why_fit
text is treated as a hypothesis, never as evidence.
'''

from __future__ import annotations

import asyncio
import json
import logging
import re

from .client import DeepSeekClient
from .pipeline import ICP, load_prompt, parse_json
from .store import Store

logger = logging.getLogger('search_skills.seeds')

DOMAIN_RE = re.compile(r'^(?!-)[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$')


def clean_seed(row: dict) -> dict | None:
    '''Normalise one model-proposed company; drop anything unusable.'''
    if not isinstance(row, dict):
        return None
    name = ' '.join(str(row.get('name') or '').split())[:80]
    domain = str(row.get('domain') or '').strip().lower()
    domain = re.sub(r'^https?://', '', domain).split('/')[0]
    domain = domain[4:] if domain.startswith('www.') else domain
    if not name or not DOMAIN_RE.match(domain):
        return None
    try:
        employees = int(float(row.get('employees') or 0))
    except (TypeError, ValueError):
        employees = 0
    listed = bool(row.get('listed')) if isinstance(row.get('listed'), bool) else False
    return {
        'name': name,
        'domain': domain,
        'country': str(row.get('country') or '').strip()[:40],
        'industry': str(row.get('industry') or '').strip()[:60],
        'employees': employees,
        'listed': listed,
        'why_fit': str(row.get('why_fit') or '').strip()[:120],
    }


async def mx_ok(domain: str) -> bool | None:
    try:
        import dns.resolver
    except ImportError:
        return None
    try:
        answers = await asyncio.to_thread(
            lambda: dns.resolver.resolve(domain, 'MX', lifetime=5)
        )
    except Exception as e:
        if type(e).__name__ in ('NXDOMAIN', 'NoAnswer'):
            return False
        return None
    return any(True for _ in answers)


def max_employees_of(size: str) -> int:
    '''Upper bound from a size band like "50-500 employees".'''
    numbers = [int(n) for n in re.findall(r'\d+', size or '')]
    return max(50, min(max(numbers), 5000)) if numbers else 2000


def build_prompt(icp: ICP, count: int, *, country: str = '', industry: str = '',
                 exclude: list[str] | None = None) -> str:
    '''Fill the template with this ICP: product, industries, geo, size, signals.'''
    signals = [s for s in (icp.signals or []) if s]
    values = {
        'PRODUCT': icp.product or '(未提供，请从行业与规模推断这家公司卖什么)',
        'INDUSTRIES': industry or ', '.join(icp.industries),
        'GEOGRAPHY': country or ', '.join(icp.geography),
        'SIZE': icp.company_size,
        'TITLES': ', '.join(icp.titles),
        'SIGNALS': '\n'.join('- ' + item for item in signals),
        'MAX_EMPLOYEES': str(max_employees_of(icp.company_size)),
        'COUNT': str(count),
    }
    text = load_prompt('gen_seeds')
    for key, value in values.items():
        text = text.replace('{{' + key + '}}', value)
    if exclude:
        text += '\n\n## 已收录（不要重复）\n' + '\n'.join(exclude[:120])
    return text


class SeedBuilder:
    '''Ask the model for companies, then keep only the ones that really exist.'''

    def __init__(self, client: DeepSeekClient | None = None, store: Store | None = None) -> None:
        self.client = client or DeepSeekClient()
        self.store = store or Store()

    async def generate(self, count: int = 50, *, country: str = '', industry: str = '',
                       icp: ICP | None = None, exclude: list[str] | None = None) -> dict:
        icp = icp or ICP()
        prompt = build_prompt(icp, count, country=country, industry=industry, exclude=exclude)

        reply = await self.client.complete(prompt, max_tokens=2400)
        data = parse_json(reply)
        rows = data.get('companies') or []
        signals = [str(x) for x in (data.get('signals') or [])][:5]
        seeds = [s for s in (clean_seed(r) for r in rows) if s]

        known = {k.replace('web:https://', '') for k in self.store.dedupe_keys()}
        seen: set[str] = set()
        kept: list[dict] = []
        stats = {'proposed': len(rows), 'parsed': len(seeds), 'duplicate': 0, 'dead': 0,
                 'too_big': 0}
        for seed in seeds:
            domain = seed['domain']
            if seed.get('employees', 0) > 2000 or seed.get('listed'):
                stats['too_big'] += 1
                continue
            if domain in seen or domain in known:
                stats['duplicate'] += 1
                continue
            seen.add(domain)
            alive = await mx_ok(domain)
            if alive is False:
                stats['dead'] += 1
                continue
            seed['mx'] = 'ok' if alive else 'unknown'
            kept.append(seed)
        return {'seeds': kept, 'signals': signals, 'stats': stats}

    def save(self, seeds: list[dict]) -> int:
        stored = 0
        for seed in seeds:
            self.store.upsert({
                'company_name': seed['name'],
                'company_website': 'https://' + seed['domain'],
                'domain': seed['domain'],
                'industry': seed['industry'],
                'summary': seed.get('why_fit', ''),
                'match_score': 50,
                'match_result': '中（50/100）· LLM 种子，待验证',
                'query': 'seed:' + (seed.get('country') or ''),
                'source_url': 'llm-seed',
                'email_status': 'pending',
            })
            stored += 1
        return stored
