'''ICP builder: a company website, or a short interview, becomes data/icp.json.

Two entry points, one schema:
  from_site(url)        -- fetch the homepage plus key pages, ask the model who they sell to
  from_answers(answers) -- no website: normalise the user's own answers

Both return {'icp': {...}, 'meta': {...}}; write_icp() drops them on disk.
'''

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .client import DeepSeekClient, DeepSeekError
from .pipeline import load_prompt, parse_json

BASE_DIR = Path(__file__).resolve().parents[1]
ICP_PATH = BASE_DIR / 'data' / 'icp.json'
TEMPLATE_PATH = BASE_DIR / 'templates' / 'icp.template.json'

# Pages worth one fetch each when profiling a seller site.
KEY_PAGES = ('/about', '/about-us', '/pricing', '/product', '/solutions', '/customers')

QUESTIONS = (
    ('product', '你们卖什么？（一句话：给谁、解决什么问题）', 'AI 销售助手：自动找线索、写冷邮件、约会议'),
    ('industries', '目标客户的行业（英文，逗号分隔）', 'SaaS, Marketing Agency'),
    ('titles', '要触达的决策人职位（英文，逗号分隔）', 'VP Marketing, Head of Growth, VP Sales'),
    ('geography', '目标地区（英文，逗号分隔）', 'United States'),
    ('company_size', '目标公司规模', '10-200 employees'),
)

DEFAULTS = {
    'industries': ['SaaS'],
    'titles': ['VP Sales'],
    'geography': ['Global'],
    'company_size': '10-200 employees',
    'product': '',
}

FIELDS = ('industries', 'titles', 'geography', 'company_size', 'product')

INDUSTRY_HINTS = {
    'SaaS': ('saas', 'software as a service', 'b2b software'),
    'E-commerce': ('ecommerce', 'e-commerce', 'shopify', 'online store', 'dtc'),
    'Marketing Agency': ('marketing agency', 'agency', 'agencies'),
    'IT Services': ('it services', 'managed services', 'msp'),
    'Fintech': ('fintech', 'payments', 'banking'),
    'Healthcare': ('healthcare', 'health tech', 'clinical'),
    'Manufacturing': ('manufacturing', 'factory', 'industrial'),
    'Logistics': ('logistics', 'freight', 'supply chain'),
}

GEO_HINTS = {
    'China': ('china', 'chinese', '中文', '中国', '国内'),
    'United States': ('united states', 'usa', 'u.s.', 'america'),
    'United Kingdom': ('united kingdom', 'uk', 'london'),
    'Germany': ('germany', 'german'),
    'Canada': ('canada', 'canadian'),
    'Australia': ('australia', 'australian'),
    'Singapore': ('singapore',),
    'Global': ('global', 'worldwide', 'international'),
}

TITLE_HINTS = (
    'VP Marketing', 'VP Sales', 'VP of Sales', 'Head of Growth', 'Head of Marketing',
    'Sales Director', 'Marketing Director', 'CMO', 'CRO', 'Founder', 'CEO',
)


def normalize_url(raw: str) -> str:
    '''Accept acme.com and https://acme.com; reject anything without a host.'''
    url = (raw or '').strip()
    if not url:
        raise ValueError('empty url')
    if '://' not in url:
        url = 'https://' + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError('not a website: ' + raw)
    return parsed.scheme + '://' + parsed.netloc


def _as_list(value, limit: int = 8, maxlen: int = 48) -> list[str]:
    '''Split a model answer (string or list) into a deduped list of short strings.'''
    if isinstance(value, str):
        parts = re.split(r'[,，;；、/\n]+', value)
    elif isinstance(value, (list, tuple)):
        parts = [str(x) for x in value]
    else:
        parts = []
    out: list[str] = []
    lowered: set[str] = set()
    for part in parts:
        text = str(part).strip().strip('."\'')
        key = text.lower()
        if text and key not in lowered:
            lowered.add(key)
            out.append(text[:maxlen])
    return out[:limit]


def _confidence(data: dict) -> int:
    try:
        value = int(float(data.get('confidence') or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, value))


def clean_icp(data: dict) -> dict:
    '''Coerce whatever the model returned into exactly the keys the pipeline reads.'''
    data = data if isinstance(data, dict) else {}
    return {
        'industries': _as_list(data.get('industries'), 6) or list(DEFAULTS['industries']),
        'titles': _as_list(data.get('titles'), 6) or list(DEFAULTS['titles']),
        'geography': _as_list(data.get('geography'), 4) or list(DEFAULTS['geography']),
        'company_size': (str(data.get('company_size') or DEFAULTS['company_size']).strip() or
                         DEFAULTS['company_size'])[:40],
        'product': str(data.get('product') or '').strip()[:200],
    }


def _meta(data: dict, method: str, source: str = '', pages: list[str] | None = None) -> dict:
    return {
        'method': method,
        'source': source,
        'pages': pages or [],
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'confidence': _confidence(data),
        'evidence': _as_list((data or {}).get('evidence'), 6, 120),
    }


def write_icp(icp: dict, path: Path | str = ICP_PATH, meta: dict | None = None) -> Path:
    '''Persist the ICP next to its provenance, in the shape ICP.from_dict reads.'''
    target = Path(path)
    payload = dict(icp)
    if meta:
        payload['_meta'] = meta
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return target


def read_icp(path: Path | str = ICP_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise DeepSeekError('cannot read ICP file: ' + str(e))


def heuristic_from_site(text: str) -> dict:
    '''No model: keyword spotting over the fetched pages.'''
    lowered = (text or '').lower()
    industries = [name for name, words in INDUSTRY_HINTS.items()
                  if any(word in lowered for word in words)][:4]
    titles = [title for title in TITLE_HINTS if title.lower() in lowered][:4]
    geography = [name for name in ('United States', 'United Kingdom', 'Germany', 'Canada',
                                   'Australia', 'Singapore')
                 if name.lower() in lowered][:3]
    return {
        'industries': industries,
        'titles': titles,
        'geography': geography,
        'company_size': DEFAULTS['company_size'],
        'product': '',
        'confidence': 40,
        'evidence': ['heuristic keyword scan, no model used'],
    }


def field_statuses(raw: dict, icp: dict, digest: str) -> dict:
    '''Where each field came from: evidence, inferred, default, missing.'''
    text = (digest or '').lower()
    raw = raw if isinstance(raw, dict) else {}
    statuses: dict = {}
    for key in FIELDS:
        given = bool(raw.get(key))
        value = icp.get(key)
        if key == 'industries':
            supported = bool(value) and all(
                any(word in text for word in INDUSTRY_HINTS.get(item, (item.lower(),)))
                for item in value
            )
            status = 'evidence' if (given and supported) else ('inferred' if given else 'default')
        elif key == 'geography':
            supported = bool(value) and all(
                any(word in text for word in GEO_HINTS.get(item, (item.lower(),)))
                for item in value
            )
            status = 'evidence' if (given and supported) else ('inferred' if given else 'default')
        elif key == 'product':
            status = 'evidence' if given else 'missing'
        else:
            status = 'inferred' if given else 'default'
        statuses[key] = {'status': status}
    return statuses


def gaps_of(statuses: dict) -> list[str]:
    return [key for key, info in statuses.items()
            if info.get('status') in ('default', 'missing')]


def coverage_of(statuses: dict) -> float:
    score = sum(1.0 if info.get('status') in ('evidence', 'user') else
                0.5 if info.get('status') == 'inferred' else 0.0
                for info in statuses.values())
    return round(score / max(1, len(statuses)), 2)


def verify_icp(icp: dict, digest: str) -> tuple[dict, list[str]]:
    '''Drop industries and regions the fetched text does not actually contain.

    Model output is a guess until it is checked against the page; this keeps the
    "do not fabricate" rule enforceable instead of aspirational.
    '''
    text = (digest or '').lower()
    notes: list[str] = []

    industries = [
        item for item in icp.get('industries') or []
        if any(word in text for word in INDUSTRY_HINTS.get(item, (item.lower(),)))
    ]
    dropped = [item for item in icp.get('industries') or [] if item not in industries]
    if dropped:
        notes.append('dropped unsupported industries: ' + ', '.join(dropped))
    if not industries:
        industries = (icp.get('industries') or DEFAULTS['industries'])[:1]
        notes.append('no industry matched the page text, kept: ' + industries[0])

    geography = [
        item for item in icp.get('geography') or []
        if any(word in text for word in GEO_HINTS.get(item, (item.lower(),)))
    ]
    dropped_geo = [item for item in icp.get('geography') or [] if item not in geography]
    if dropped_geo:
        notes.append('dropped unsupported regions: ' + ', '.join(dropped_geo))
    if not geography:
        geography = ['Global']
        notes.append('no region evidence, set to Global')

    verified = dict(icp)
    verified['industries'] = industries
    verified['geography'] = geography
    return verified, notes


def _site_digest(chunks: list[tuple[str, str]]) -> str:
    parts = []
    for path, content in chunks:
        body = (content or '').strip()
        if not body:
            continue
        parts.append('--- PAGE ' + path + ' ---\n' + body[:6000])
    return '\n\n'.join(parts)[:24000]


async def from_site(
    url: str,
    *,
    pages: int = 4,
    use_llm: bool = True,
    client: DeepSeekClient | None = None,
    fetcher=None,
) -> dict:
    '''Fetch the seller site and infer the ICP from it.'''
    base = normalize_url(url)
    client = client or DeepSeekClient()
    client.check_credentials()
    fetch = (fetcher or client).fetch

    chunks: list[tuple[str, str]] = []
    home = await fetch(base)
    chunks.append((base, home.content or ''))

    for path in KEY_PAGES:
        if len(chunks) >= max(1, pages) + 1:
            break
        try:
            page = await fetch(base + path)
        except DeepSeekError:
            continue
        if len((page.content or '').strip()) > 300:
            chunks.append((path, page.content))

    digest = _site_digest(chunks)
    if len(digest) < 200:
        raise DeepSeekError('the site returned almost no text: ' + base)

    data: dict = {}
    if use_llm and load_prompt('icp_from_site'):
        prompt = load_prompt('icp_from_site') + '\n\n---\n## 网站正文\n' + digest
        try:
            data = parse_json(await client.complete(prompt, max_tokens=1200))
        except DeepSeekError:
            data = {}
    if not data:
        data = heuristic_from_site(digest)

    icp, notes = verify_icp(clean_icp(data), digest)
    statuses = field_statuses(data, icp, digest)
    meta = _meta(data, 'website', base, [path for path, _ in chunks])
    meta['fields'] = statuses
    meta['gaps'] = gaps_of(statuses)
    meta['coverage'] = coverage_of(statuses)
    if len(chunks) <= 1:
        meta['confidence'] = min(meta['confidence'], 70)
        notes.append('only one page was readable, confidence capped at 70')
    meta['evidence'] = (meta.get('evidence') or []) + notes
    return {'icp': icp, 'meta': meta}


def _answer_text(answers: dict) -> str:
    lines = []
    for index, (key, question, _default) in enumerate(QUESTIONS, start=1):
        lines.append('Q' + str(index) + ' ' + question)
        lines.append('A' + str(index) + ' ' + str(answers.get(key) or '').strip())
    return '\n'.join(lines)


async def from_answers(
    answers: dict,
    *,
    use_llm: bool = True,
    client: DeepSeekClient | None = None,
) -> dict:
    '''No website: normalise the interview answers into the ICP schema.'''
    given = {k: (v or '').strip() for k, v in (answers or {}).items()}
    if not any(given.values()):
        raise ValueError('need at least one answered field')
    text = _answer_text(given)

    data: dict = {}
    if use_llm and load_prompt('icp_from_answers'):
        client = client or DeepSeekClient()
        client.check_credentials()
        prompt = load_prompt('icp_from_answers') + '\n\n---\n## 问答\n' + text
        try:
            data = parse_json(await client.complete(prompt, max_tokens=900))
        except DeepSeekError:
            data = {}
    if not data:
        data = dict(given)

    return {'icp': clean_icp(data), 'meta': _meta(data, 'interview', '', [])}


def ask_questions(keys=None, defaults=None, input_fn=input) -> dict:
    '''The guided path: all five questions, or only the ones the ICP is missing.'''
    wanted = set(keys) if keys else {key for key, _q, _d in QUESTIONS}
    defaults = defaults or {}
    answers: dict = {}
    for key, question, default in QUESTIONS:
        if key not in wanted:
            continue
        default = defaults.get(key) or default
        try:
            reply = input_fn(question + ' [' + default + ']: ')
        except EOFError:
            reply = ''
        answers[key] = (reply or '').strip() or default
    return answers


async def fill_gaps(result: dict, answers: dict, *, use_llm: bool = True, client=None) -> dict:
    '''Merge only the answered fields back into an ICP that came from a thin site.'''
    cleaned = {k: (v or '').strip() for k, v in (answers or {}).items() if (v or '').strip()}
    if not cleaned:
        return result
    filled = await from_answers(cleaned, use_llm=use_llm, client=client)
    icp = dict(result['icp'])
    for key in cleaned:
        value = filled['icp'].get(key)
        if value:
            icp[key] = value
    statuses = dict((result.get('meta') or {}).get('fields') or {})
    for key in cleaned:
        statuses[key] = {'status': 'user', 'source': 'answered in the interview'}
    meta = dict(result.get('meta') or {})
    meta['fields'] = statuses
    meta['gaps'] = gaps_of(statuses)
    meta['coverage'] = coverage_of(statuses)
    return {'icp': icp, 'meta': meta}


def template() -> dict:
    '''A fill-in-the-blank ICP, for people who prefer editing JSON by hand.'''
    return {
        'industries': ['SaaS', 'Marketing Agency'],
        'titles': ['VP Marketing', 'Head of Growth', 'VP Sales'],
        'geography': ['United States'],
        'company_size': '10-200 employees',
        'product': '一句话：卖什么 + 给谁 + 解决什么问题',
    }
