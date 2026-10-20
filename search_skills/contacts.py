'''Contact-person enrichment: turn generic mailboxes into named decision-makers.

This module exports leads that have a working contact@/info@/sales@ mailbox but no
named person in the company_perple field, so an agent can dig through team pages,
press releases, and news articles to find a named contact with title and direct email.
'''

from __future__ import annotations

import re
from datetime import datetime

from .store import Store


def looks_generic(email: str) -> bool:
    '''Generic mailboxes (info@, contact@, sales@) vs personal (j.doe@, jdoe@).'''
    if not email or '@' not in email:
        return False
    local = email.split('@')[0].lower()
    generic_prefixes = ('info', 'contact', 'sales', 'hello', 'support', 'inquiry',
                        'kontakt', 'anfrage', 'vertrieb')
    return local in generic_prefixes


def looks_named(perple: str) -> bool:
    '''Whether the perple field has a real name (latin or CJK) with a title.'''
    if not perple or len(perple.strip()) < 3:
        return False
    # Has letters and at least one parenthesis or title keyword.
    has_letters = bool(re.search(r'[A-Za-z一-鿿]', perple))
    has_role = bool(re.search(r'[()（）]|head|director|manager|vp|ceo|cto|founder', perple, re.I))
    return has_letters and has_role


def validate_contact_result(domain: str, name: str, title: str, email: str,
                             evidence_url: str) -> tuple[bool, str]:
    '''Accept an agent's contact only when it has name + title + evidence on the domain.'''
    if not evidence_url or not re.match(r'^https?://', evidence_url):
        return False, 'missing evidence_url'
    if not name or len(name.strip()) < 2:
        return False, 'missing or too-short name'
    if not title or len(title.strip()) < 2:
        return False, 'missing or too-short title'
    # Email is optional (agent may find a name but no direct email).
    if email:
        from .emailver import host_of
        email_host = host_of('https://' + email.split('@')[-1])
        if email_host and email_host != domain:
            return False, 'email is not on the company domain'
        if looks_generic(email):
            return False, 'email is still generic, not a person'
    # Evidence must be on the company domain or a known press/news site.
    from .emailver import host_of
    ev_host = host_of(evidence_url)
    # Allow press releases that quote the contact.
    ok_hosts = {domain, 'linkedin.com', 'crunchbase.com', 'prnewswire.com',
                'businesswire.com', 'globenewswire.com'}
    if ev_host not in ok_hosts and not ev_host.endswith('.pr.co'):
        return False, 'evidence is not on the company domain or known press site'
    return True, ''


class ContactEnricher:
    '''Parallel agent workflow for finding named contacts to replace generic mailboxes.'''

    def __init__(self, store: Store):
        self.store = store

    def export_tasks(self, path: str, limit: int = 10, min_score: int = 60) -> dict:
        '''Export leads with generic mailboxes but no named contact.'''
        import json
        from pathlib import Path

        # Fetch leads with a working email but no named person.
        rows = self.store.list_for_contact_enrichment(limit=limit, min_score=min_score)
        tasks = []
        for row in rows:
            domain = row.get('domain') or ''
            tasks.append({
                'dedupe_key': row['dedupe_key'],
                'company_name': row.get('company_name'),
                'domain': domain,
                'website': row.get('company_website'),
                'current_email': row.get('company_email'),
                'match_score': row.get('match_score'),
                'industry': row.get('industry'),
                'try_urls': [
                    'https://' + domain + p for p in
                    ('/about', '/team', '/leadership', '/impressum', '/imprint', '/contact')
                ],
                'search_hints': [
                    f'site:{domain} (CEO OR founder OR "head of" OR director)',
                    f'"{row.get("company_name")}" CEO founder press release',
                ],
                'ask': (
                    'Find ONE decision-maker (C-level, VP, Head of X, or Founder) '
                    'with their full name, title, and optionally a direct email. '
                    'Quote the exact URL where you found them. '
                    'Prioritize sales, growth, product, or operations roles.'
                ),
            })
        payload = {
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'count': len(tasks),
            'tasks': tasks,
            'format': {
                'dedupe_key': 'echo it back',
                'name': 'full name (e.g., Jane Doe)',
                'title': 'job title (e.g., VP Sales)',
                'email': 'direct email if found, else empty string',
                'evidence_url': 'page where the contact is visible (team page, press release, etc.)',
            },
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                              encoding='utf-8')
        self.store.mark_needs_contact([t['dedupe_key'] for t in tasks])
        return {'exported': len(tasks), 'path': str(path)}

    def import_results(self, path: str) -> dict:
        '''Take agent's contacts back, accepting only those with evidence.'''
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
            name = str(item.get('name') or '').strip()
            title = str(item.get('title') or '').strip()
            email = str(item.get('email') or '').strip().lower()
            evidence = str(item.get('evidence_url') or '').strip()
            ok, reason = validate_contact_result(domain, name, title, email, evidence)
            if not ok:
                report['rejected'] += 1
                report['errors'].append({'dedupe_key': key, 'reason': reason})
                continue
            # Format: "Name (Title)"
            perple = f'{name} ({title})'
            updates = {
                'company_perple': perple,
                'contact_status': 'found',
                'contact_source': 'agent',
                'contact_evidence': evidence,
            }
            # If agent also found a direct email, upgrade it.
            if email:
                updates['company_email'] = email
                if lead.get('email_status') in ('found', ''):
                    updates['email_status'] = 'found'
                    updates['email_source'] = 'agent_direct'
                    updates['email_kind'] = 'personal'
            self.store.update_contact(key, **updates)
            report['accepted'] += 1
            report['rows'].append({
                'company_name': lead.get('company_name'),
                'contact': perple,
                'email': email or lead.get('company_email', ''),
                'evidence': evidence,
            })
        return report
