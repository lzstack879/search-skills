'''Email verification for a lead: MX check, then search snippets, then the site.

Billing note: this module is the only place that spends a search call just to
find an address. One call per lead, two at most, and never a guessed pattern.
'''

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from .client import DeepSeekClient, usage_summary
from .pipeline import extract_emails
from .store import Store, host_of

logger = logging.getLogger('search_skills.emailver')

# Never usable for outbound sales.
BLOCK_LOCAL = {
    'hr', 'jobs', 'careers', 'career', 'recruiting', 'privacy', 'datenschutz', 'legal',
    'impressum', 'noreply', 'no-reply', 'webmaster', 'postmaster', 'abuse', 'spam',
    'gdpr', 'dpo', 'mailer-daemon', 'unsubscribe', 'pr', 'press', 'media',
    # recruiting inboxes in the languages the EU actually uses
    'karriere', 'karrieren', 'bewerbung', 'bewerbungen', 'personal', 'recrutement',
    'candidature', 'candidatures', 'recrutamento', 'candidaturas', 'empleo', 'empleos',
    'rh', 'rrhh', 'vacatures', 'sollicitatie', 'rekrytering', 'rekruttering',
    'talent', 'talents', 'recruit', 'recruiting', 'stage', 'stages', 'praktikum',
    'internship', 'internships', 'azubi', 'ausbildung',
}

# Usable, but a shared inbox: keep it, rank it last, mark it generic.
GENERIC_LOCAL = {
    'info', 'sales', 'contact', 'hello', 'hi', 'service', 'support', 'office',
    'enquiry', 'inquiry', 'enquiries', 'team', 'marketing', 'bd', 'business',
    'admin', 'mail', 'email', 'general', 'kontakt', 'welcome', 'hey', 'accounts',
}

# Documentation placeholders and template leftovers that show up in snippets.
PLACEHOLDER_RE = re.compile(
    r'(jane|john|j|m)[._-]?doe|your[._-]?name|yourname|username|user@|test@|example|'
    r'placeholder|lorem|sample|template|application[._-]?development|'
    r'(^|\.)name@|^(user|test|demo|sample|example)[0-9]*$',
    re.I,
)

# A snippet email is only trusted when a contact word sits right next to it.
CONTACT_WORDS = ('contact', 'e-mail', 'email', 'mail us', 'write to', 'reach us',
                 'get in touch', 'enquir', 'inquir', 'kontakt', 'impressum')

# Contact pages worth a free local fetch, most-likely first.
# German sites must publish an Impressum by law, so it usually carries an address.
FALLBACK_PATHS = ('/impressum', '/imprint', '/kontakt', '/contact', '/mentions-legales',
                  '/legal-notice', '/about')

EMAIL_SPLIT = re.compile(r'@')


def email_domain(email: str) -> str:
    parts = (email or '').strip().lower().split('@')
    return parts[1] if len(parts) == 2 else ''


def classify(domain: str, email: str) -> tuple[str, str]:
    '''Return (kind, reason) where kind is named / generic / blocked.'''
    email = (email or '').strip().lower()
    host = email_domain(email)
    if not host:
        return 'blocked', 'not-an-email'
    domain = (domain or '').lower()
    if domain and host != domain and not host.endswith('.' + domain):
        return 'blocked', 'foreign-domain'
    local = email.split('@')[0].split('+')[0]
    if len(local) < 2:
        return 'blocked', 'too-short'
    if any(part in BLOCK_LOCAL for part in re.split(r'[._-]', local) if part):
        return 'blocked', 'role-mailbox'
    if PLACEHOLDER_RE.search(local):
        return 'blocked', 'placeholder'
    if local.count('.') >= 3:
        return 'blocked', 'looks-like-a-sentence'
    if local in BLOCK_LOCAL:
        return 'blocked', 'role-mailbox'
    if local in GENERIC_LOCAL:
        return 'generic', 'shared-inbox'
    if '.' in local or '_' in local or '-' in local:
        return 'named', 'person-mailbox'
    return 'generic', 'unknown-single-word'


def contact_links(markdown: str, domain: str) -> list[str]:
    '''Same-domain links whose URL says contact / imprint / legal.'''
    from urllib.parse import urlparse

    out: list[str] = []
    for _label, url in re.findall(r'\[([^\]]{0,60})\]\((https?://[^)\s]+)\)', markdown or ''):
        host = (urlparse(url).hostname or '').lower()
        host = host[4:] if host.startswith('www.') else host
        if host != domain:
            continue
        if any(word in url.lower() for word in
               ('contact', 'kontakt', 'impressum', 'imprint', 'legal', 'about')):
            if url not in out:
                out.append(url)
    return out[:3]


VERDICTS = ('valid', 'catch_all', 'invalid', 'greylisted', 'smtp_unreachable', 'no_mx')

# Grade weights: A = mailbox proven, B = domain accepts everything,
# C = published on the official site + domain takes mail, but the gateway blocked us.
GRADE_ORDER = {'A': 3, 'B': 2, 'C': 1, '': 0}
PUBLISHED_SOURCES = ('own_site', 'extract', 'agent', 'snippet')


def grade_of(verdict: str, source: str, *, mx_ok: bool = True) -> str:
    '''Turn a verdict plus its provenance into A/B/C (empty = not usable).'''
    if verdict == 'valid':
        return 'A'
    if verdict == 'catch_all':
        return 'B'
    if verdict in ('smtp_unreachable', 'greylisted') and mx_ok and source in PUBLISHED_SOURCES:
        return 'C'
    return ''


def verdict_from_codes(target: int, catchall: int | None) -> str:
    '''SMTP reply codes -> one verdict. Catch-all is what makes this honest.'''
    if target in (250, 251):
        if catchall in (250, 251):
            return 'catch_all'
        if catchall is not None and catchall >= 500:
            return 'valid'
        return 'valid' if catchall is None else 'catch_all'
    if target in (550, 551, 553):
        return 'invalid'
    if 400 <= target < 500:
        return 'greylisted'
    return 'smtp_unreachable'


def smtp_probe(email: str, domain: str, *, timeout: float = 12.0) -> tuple[str, int, int | None]:
    '''Ask the MX server whether the mailbox exists, plus a catch-all control.

    Returns (verdict, target_code, catchall_code). Never raises.
    '''
    import random
    import smtplib
    import string

    try:
        import dns.resolver
    except ImportError:
        return 'smtp_unreachable', 0, None
    try:
        answers = dns.resolver.resolve(domain, 'MX', lifetime=5)
        hosts = sorted((int(r.preference), str(r.exchange).rstrip('.')) for r in answers)
    except Exception as e:
        if type(e).__name__ in ('NXDOMAIN', 'NoAnswer'):
            return 'no_mx', 0, None
        return 'smtp_unreachable', 0, None
    if not hosts:
        return 'no_mx', 0, None

    control = ''.join(random.choices(string.ascii_lowercase, k=10)) + '-nope@' + domain
    for _priority, host in hosts[:2]:
        try:
            server = smtplib.SMTP(host, 25, timeout=timeout)
            server.ehlo('verify.local')
            server.mail('verify@example.com')
            target_code, _ = server.rcpt(email)
            catchall_code, _ = server.rcpt(control)
            try:
                server.quit()
            except Exception:
                pass
            return verdict_from_codes(target_code, catchall_code), target_code, catchall_code
        except Exception:
            continue
    return 'smtp_unreachable', 0, None


def validate_agent_result(domain: str, email: str,
                          evidence_url: str) -> tuple[bool, str, str]:
    '''An agent's answer is only accepted with a URL that shows the address.'''
    if not evidence_url or not re.match(r'^https?://', evidence_url):
        return False, 'missing evidence_url', ''
    if not email:
        return False, 'no email', ''
    kind, reason = classify(domain, email)
    if kind == 'blocked':
        return False, 'rejected: ' + reason, ''
    if host_of(evidence_url) != domain:
        return False, 'evidence is not on the company domain', ''
    return True, '', kind


class EmailVerifier:
    '''Turns a lead's domain into a verified address, or an honest "none".'''

    def __init__(
        self,
        client: DeepSeekClient | None = None,
        store: Store | None = None,
        fetcher=None,
        *,
        max_fetches: int = 4,
    ) -> None:
        self.client = client or DeepSeekClient()
        self.store = store or Store()
        self.fetcher = fetcher or self.client
        self.max_fetches = max_fetches
        self.stats = {'mx_dead': 0, 'snippet_hit': 0, 'page_hit': 0, 'none': 0}

    async def mx_ok(self, domain: str) -> bool | None:
        '''True / False from DNS; None when the lookup itself failed.'''
        try:
            import dns.resolver
        except ImportError:
            return None
        try:
            answers = await asyncio.to_thread(
                lambda: dns.resolver.resolve(domain, 'MX', lifetime=5)
            )
        except Exception as e:
            name = type(e).__name__
            if name in ('NXDOMAIN', 'NoAnswer', 'NoNameservers'):
                return False
            logger.info('mx lookup failed for %s: %s', domain, name)
            return None
        return any(True for _ in answers)

    @staticmethod
    def _near_contact(text: str, email: str) -> bool:
        '''True when a contact word sits within ~70 chars of the address.'''
        lowered = (text or '').lower()
        start = lowered.find(email.lower())
        if start < 0:
            return False
        window = lowered[max(0, start - 70): start + len(email) + 70]
        return any(word in window for word in CONTACT_WORDS)

    @classmethod
    def _pick(cls, domain: str, emails: list[str], text: str = '',
              require_context: bool = False) -> tuple[str, str]:
        '''Best (email, kind) from a batch, named before generic.'''
        named, generic = [], []
        for email in emails:
            kind, _reason = classify(domain, email)
            if kind == 'blocked':
                continue
            if require_context and not cls._near_contact(text, email):
                continue
            (named if kind == 'named' else generic).append(email)
        if named:
            return named[0], 'named'
        if generic:
            return generic[0], 'generic'
        return '', ''

    async def _confirm(self, email: str, domain: str) -> str:
        '''SMTP + catch-all verdict, run off the event loop.'''
        try:
            verdict, _t, _c = await asyncio.to_thread(smtp_probe, email, domain)
        except Exception as e:
            logger.info('smtp probe failed for %s: %s', email, str(e)[:60])
            return 'smtp_unreachable'
        self.stats[verdict] = self.stats.get(verdict, 0) + 1
        return verdict

    async def verify(self, lead: dict) -> dict:
        '''Return the fields to write back for one lead.'''
        domain = (lead.get('domain') or host_of(lead.get('company_website') or '')).lower()
        attempts = int(lead.get('email_attempts') or 0) + 1
        if not domain:
            return {'email_status': 'rejected', 'email_source': 'no-domain',
                    'email_attempts': attempts}

        mx = await self.mx_ok(domain)
        if mx is False:
            self.stats['mx_dead'] += 1
            return {'email_status': 'domain_dead', 'email_source': 'mx',
                    'email_attempts': attempts}

        # 0) an address was already harvested from the site: just verify it (P0)
        invalid_existing = False
        existing = str(lead.get('company_email') or '').strip()
        if existing and not lead.get('email_verdict'):
            verdict = await self._confirm(existing, domain)
            if verdict == 'invalid':
                invalid_existing = True
            else:
                kind, _reason = classify(domain, existing)
                source = lead.get('email_source') or 'extract'
                return {'company_email': existing, 'email_status': 'found',
                        'email_source': source,
                        'email_kind': kind or (lead.get('email_kind') or 'generic'),
                        'email_verdict': verdict,
                        'email_grade': grade_of(verdict, source, mx_ok=mx is not False),
                        'email_attempts': attempts}

        # 1) the cheap way: addresses are often printed in the search snippet itself
        found: list[str] = []
        hits = []
        try:
            hits = await self.client.search('"@' + domain + '"', 6)
        except Exception as e:
            logger.warning('snippet search failed for %s: %s', domain, str(e)[:100])
        blobs = []
        for hit in hits:
            blob = ' '.join([hit.title or '', hit.summary or '', hit.snippet or ''])
            blobs.append(blob)
            found.extend(extract_emails(blob))
        email, kind = self._pick(domain, found, ' '.join(blobs), require_context=True)
        if email:
            verdict = await self._confirm(email, domain)
            if verdict != 'invalid':
                self.stats['snippet_hit'] += 1
                return {'company_email': email, 'email_status': 'found',
                        'email_source': 'snippet', 'email_kind': kind,
                        'email_verdict': verdict,
                        'email_grade': grade_of(verdict, 'snippet', mx_ok=mx is not False),
                        'email_attempts': attempts}

        # 2) fall back to the company's own pages: search hits, root, then the
        #    legal/contact pages every German (and most EU) sites must publish.
        candidates: list[str] = []
        for hit in hits:
            if hit.url and host_of(hit.url) == domain and any(
                word in hit.url.lower()
                for word in ('contact', 'kontakt', 'impressum', 'imprint', 'legal', 'about')
            ):
                candidates.append(hit.url)
        candidates.append('https://' + domain)
        candidates.extend('https://' + domain + path for path in FALLBACK_PATHS)

        tried: set[str] = set()
        fetches = 0
        for url in candidates:
            if fetches >= self.max_fetches:
                break
            if url in tried:
                continue
            tried.add(url)
            try:
                page = await self.fetcher.fetch(url)
            except Exception as e:
                logger.info('page fetch failed for %s: %s', url, str(e)[:80])
                continue
            fetches += 1
            email, kind = self._pick(domain, extract_emails(page.content or ''))
            if email:
                verdict = await self._confirm(email, domain)
                if verdict != 'invalid':
                    self.stats['page_hit'] += 1
                    return {'company_email': email, 'email_status': 'found',
                            'email_source': 'own_site', 'email_kind': kind,
                            'email_verdict': verdict,
                            'email_grade': grade_of(verdict, 'own_site', mx_ok=mx is not False),
                            'email_attempts': attempts}
            # a link found on this page that looks like a contact page is worth one more
            for link in contact_links(page.content, domain):
                if link not in tried:
                    candidates.append(link)

        self.stats['none'] += 1
        outcome = {'email_status': 'no_contact', 'email_source': '',
                   'email_verdict': 'invalid' if invalid_existing else '',
                   'email_attempts': attempts}
        if invalid_existing:
            outcome['company_email'] = ''
            outcome['email_grade'] = ''
        return outcome

    # -- agent hand-off -------------------------------------------------

    def export_tasks(self, path: str, limit: int = 20, min_score: int = 55) -> dict:
        '''Write the leads a script could not solve, for an agent to research.'''
        import json
        from pathlib import Path

        rows = self.store.agent_tasks(limit=limit, min_score=min_score)
        tasks = []
        for row in rows:
            domain = row.get('domain') or ''
            tasks.append({
                'dedupe_key': row['dedupe_key'],
                'company_name': row.get('company_name'),
                'domain': domain,
                'website': row.get('company_website'),
                'match_score': row.get('match_score'),
                'try_urls': ['https://' + domain + p for p in
                             ('/impressum', '/imprint', '/kontakt', '/contact', '/about')],
                'ask': 'Find one business email on the company domain and quote the URL that shows it.',
            })
        payload = {'generated_at': datetime.now().isoformat(timespec='seconds'),
                   'count': len(tasks), 'tasks': tasks,
                   'format': {'dedupe_key': 'echo it back', 'email': 'address found',
                              'evidence_url': 'page where the address is visible'}}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                              encoding='utf-8')
        self.store.mark_needs_agent([t['dedupe_key'] for t in tasks])
        return {'exported': len(tasks), 'path': str(path)}

    def import_results(self, path: str) -> dict:
        '''Take an agent's answers back, accepting only those with evidence.'''
        import json
        from pathlib import Path

        data = json.loads(Path(path).read_text(encoding='utf-8'))
        items = data.get('results') if isinstance(data, dict) else data
        report = {'accepted': 0, 'rejected': 0, 'errors': [], 'rows': []}
        for item in items or []:
            key = str(item.get('dedupe_key') or '')
            lead = self.store.get(key) if key else None
            if not lead:
                report['rejected'] += 1
                report['errors'].append({'dedupe_key': key, 'reason': 'unknown lead'})
                continue
            domain = (lead.get('domain') or '').lower()
            email = str(item.get('email') or '').strip().lower()
            evidence = str(item.get('evidence_url') or '').strip()
            ok, reason, kind = validate_agent_result(domain, email, evidence)
            if not ok:
                report['rejected'] += 1
                report['errors'].append({'dedupe_key': key, 'reason': reason})
                continue
            self.store.save_email(
                key, company_email=email, email_status='found', email_source='agent',
                email_kind=kind, email_evidence=evidence,
                email_grade=grade_of('smtp_unreachable', 'agent'),
                email_attempts=int(lead.get('email_attempts') or 0) + 1,
            )
            report['accepted'] += 1
            report['rows'].append({'company_name': lead.get('company_name'),
                                   'email': email, 'kind': kind,
                                   'evidence': evidence})
        return report

    def regrade(self) -> dict:
        '''Recompute A/B/C from the verdicts already stored (no network).'''
        self.store.init()
        changed = 0
        rows = self.store.list_recent(500)
        for row in rows:
            email = row.get('company_email') or ''
            verdict = row.get('email_verdict') or ''
            if not email or not verdict:
                continue
            grade = grade_of(verdict, row.get('email_source') or '', mx_ok=True)
            if grade != (row.get('email_grade') or ''):
                self.store.save_email(row['dedupe_key'], email_grade=grade)
                changed += 1
        return {'changed': changed}

    async def run(self, limit: int = 20, *, retry_days: int = 7) -> dict:
        '''Verify a batch of pending leads and write the outcomes back.'''
        self.store.init()
        leads = self.store.pending_email(limit=limit, retry_days=retry_days)
        report = {'picked': len(leads), 'found': 0, 'generic': 0, 'no_contact': 0,
                  'rejected': 0, 'domain_dead': 0, 'rows': []}
        for lead in leads:
            outcome = await self.verify(lead)
            self.store.save_email(lead['dedupe_key'], **outcome)
            status = outcome.get('email_status', '')
            report[status] = report.get(status, 0) + 1
            if status == 'found' and outcome.get('email_kind') == 'generic':
                report['generic'] += 1
            report['rows'].append({
                'company_name': lead.get('company_name'),
                'domain': lead.get('domain'),
                'email': outcome.get('company_email', ''),
                'kind': outcome.get('email_kind', ''),
                'grade': outcome.get('email_grade', ''),
                'status': status,
            })
        usage = usage_summary()
        report['calls_today'] = usage['today_search'] + usage['today_fetch']
        return report
