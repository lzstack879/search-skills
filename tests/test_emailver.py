'''Offline tests for the email verifier (no DNS, no network).'''

import asyncio

import pytest

from search_skills import emailver
from search_skills.client import FetchPage, SearchHit
from search_skills.emailver import BLOCK_LOCAL, EmailVerifier, classify
from search_skills.store import Store


@pytest.fixture(autouse=True)
def _offline_smtp(monkeypatch):
    '''Never touch port 25 in tests: pretend every mailbox exists and is precise.'''
    monkeypatch.setattr(emailver, 'smtp_probe',
                        lambda email, domain, **kw: ('valid', 250, 550))


def test_classify_marks_named_generic_and_blocked():
    assert classify('acme.com', 'zhang.san@acme.com')[0] == 'named'
    assert classify('acme.com', 'sales@acme.com')[0] == 'generic'
    assert classify('acme.com', 'hr@acme.com')[0] == 'blocked'
    assert classify('acme.com', 'someone@gmail.com')[0] == 'blocked'
    assert classify('acme.com', 'zhang.san@gmail.com')[1] == 'foreign-domain'
    assert 'hr' in BLOCK_LOCAL


def test_classify_accepts_subdomains():
    assert classify('acme.com', 'sales@mail.acme.com')[0] == 'generic'


class FakeSearch:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    async def search(self, query, count=6, **kwargs):
        self.queries.append(query)
        return list(self.hits)


class FakeFetch:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        return FetchPage(url=url, content=self.pages.get(url, ''), backend='local')


def make_verifier(hits, pages=None, mx=True):
    store = Store(':memory:').init()
    verifier = EmailVerifier(FakeSearch(hits), store, FakeFetch(pages or {}))
    async def fake_mx(_domain):
        return mx
    verifier.mx_ok = fake_mx
    return verifier


def test_snippet_email_wins_without_fetching():
    hits = [SearchHit(url='https://acme.com/imprint', title='Imprint | Acme',
                      snippet='Contact us at sales@acme.com')]
    verifier = make_verifier(hits)
    outcome = asyncio.run(verifier.verify({'domain': 'acme.com', 'company_website': 'https://acme.com'}))
    assert outcome['email_status'] == 'found'
    assert outcome['company_email'] == 'sales@acme.com'
    assert outcome['email_kind'] == 'generic'
    assert outcome['email_source'] == 'snippet'
    assert verifier.fetcher.calls == []


def test_named_email_beats_generic_in_the_same_batch():
    hits = [SearchHit(url='https://acme.com', title='Acme',
                      snippet='Contact us: info@acme.com or e-mail zhang.san@acme.com')]
    verifier = make_verifier(hits)
    outcome = asyncio.run(verifier.verify({'domain': 'acme.com'}))
    assert outcome['company_email'] == 'zhang.san@acme.com'
    assert outcome['email_kind'] == 'named'


def test_placeholder_and_template_emails_are_blocked():
    assert classify('acme.com', 'jane.doe@acme.com')[0] == 'blocked'
    assert classify('acme.com', 'yourname@acme.com')[0] == 'blocked'
    assert classify('acme.com', 'zoom.application.development.use@acme.com')[0] == 'blocked'
    assert classify('acme.com', 'pr@acme.com')[0] == 'blocked'
    assert classify('acme.com', 'karriere@acme.de')[0] == 'blocked'
    assert classify('acme.com', 'bewerbung@acme.de')[0] == 'blocked'
    assert classify('acme.com', 'recrutement@acme.fr')[0] == 'blocked'
    assert classify('acme.com', 'rh@acme.fr')[0] == 'blocked'
    assert classify('acme.com', 'm@acme.com')[0] == 'blocked'
    assert classify('acme.com', 'info.privacy@acme.com')[0] == 'blocked'
    assert classify('acme.com', 'j.doe@acme.com')[0] == 'blocked'


def test_snippet_email_without_contact_context_is_ignored():
    hits = [SearchHit(url='https://acme.com', title='Acme',
                      snippet='our team shipped a new release last week sales@acme.com')]
    verifier = make_verifier(hits, pages={'https://acme.com': ''})
    outcome = asyncio.run(verifier.verify({'domain': 'acme.com'}))
    assert outcome['email_status'] == 'no_contact'


def test_foreign_domain_email_is_ignored():
    hits = [SearchHit(url='https://directory.com/acme', title='Acme profile',
                      snippet='reach them at acme@gmail.com')]
    verifier = make_verifier(hits, pages={'https://acme.com': ''})
    outcome = asyncio.run(verifier.verify({'domain': 'acme.com'}))
    assert outcome['email_status'] == 'no_contact'


def test_falls_back_to_the_company_page():
    hits = [SearchHit(url='https://acme.com/contact', title='Contact | Acme')]
    pages = {'https://acme.com/contact': 'Reach us: hello@acme.com'}
    verifier = make_verifier(hits, pages=pages)
    outcome = asyncio.run(verifier.verify({'domain': 'acme.com'}))
    assert outcome['email_status'] == 'found'
    assert outcome['email_source'] == 'own_site'
    assert outcome['company_email'] == 'hello@acme.com'


def test_impressum_is_tried_when_root_has_no_email():
    hits = [SearchHit(url='https://firma.de', title='Firma')]
    pages = {
        'https://firma.de': 'Willkommen bei Firma. Kein Kontakt hier.',
        'https://firma.de/impressum': 'Impressum: E-Mail: info@firma.de',
    }
    verifier = make_verifier(hits, pages=pages)
    outcome = asyncio.run(verifier.verify({'domain': 'firma.de'}))
    assert outcome['email_status'] == 'found'
    assert outcome['company_email'] == 'info@firma.de'
    assert 'https://firma.de/impressum' in verifier.fetcher.calls


def test_contact_link_found_on_the_homepage_is_followed():
    hits = [SearchHit(url='https://acme.com', title='Acme')]
    pages = {
        'https://acme.com': '[Kontakt](https://acme.com/kontakt) | [Jobs](https://acme.com/jobs)',
        'https://acme.com/kontakt': 'Mail: hello@acme.com',
    }
    verifier = make_verifier(hits, pages=pages)
    outcome = asyncio.run(verifier.verify({'domain': 'acme.com'}))
    assert outcome['company_email'] == 'hello@acme.com'
    assert 'https://acme.com/kontakt' in verifier.fetcher.calls


def test_dead_domain_is_marked_without_spending_a_search():
    verifier = make_verifier([], mx=False)
    outcome = asyncio.run(verifier.verify({'domain': 'dead.example'}))
    assert outcome['email_status'] == 'domain_dead'
    assert verifier.client.queries == []


def test_no_contact_increments_attempts():
    verifier = make_verifier([], pages={'https://acme.com': 'no addresses here'})
    outcome = asyncio.run(verifier.verify({'domain': 'acme.com', 'email_attempts': 1}))
    assert outcome['email_status'] == 'no_contact'
    assert outcome['email_attempts'] == 2


def test_batch_writes_back_to_the_store(tmp_path):
    store = Store(tmp_path / 'c.db').init()
    store.upsert({'company_name': 'Acme', 'company_website': 'https://acme.com',
                  'match_score': 70})
    hits = [SearchHit(url='https://acme.com', title='Acme',
                      snippet='Contact: sales@acme.com')]
    verifier = EmailVerifier(FakeSearch(hits), store, FakeFetch({}))
    async def fake_mx(_domain):
        return True
    verifier.mx_ok = fake_mx
    report = asyncio.run(verifier.run(limit=5))
    assert report['picked'] == 1
    assert report['found'] == 1
    row = store.list_recent(1)[0]
    assert row['company_email'] == 'sales@acme.com'
    assert row['email_status'] == 'found'


def test_agent_export_and_import_roundtrip(tmp_path):
    import json

    from search_skills.emailver import EmailVerifier

    store = Store(tmp_path / 'c.db').init()
    store.upsert({'company_name': 'Firma', 'company_website': 'https://firma.de',
                  'match_score': 70, 'email_status': 'no_contact', 'domain': 'firma.de'})
    verifier = EmailVerifier(None, store)

    tasks_path = tmp_path / 'tasks.json'
    out = verifier.export_tasks(str(tasks_path), limit=5)
    assert out['exported'] == 1
    payload = json.loads(tasks_path.read_text(encoding='utf-8'))
    key = payload['tasks'][0]['dedupe_key']
    assert store.get(key)['email_status'] == 'needs_agent'

    results = {'results': [{'dedupe_key': key, 'email': 'info@firma.de',
                            'evidence_url': 'https://firma.de/impressum'}]}
    results_path = tmp_path / 'results.json'
    results_path.write_text(json.dumps(results), encoding='utf-8')
    report = verifier.import_results(str(results_path))
    assert report['accepted'] == 1
    saved = store.get(key)
    assert saved['company_email'] == 'info@firma.de'
    assert saved['email_source'] == 'agent'
    assert saved['email_evidence'] == 'https://firma.de/impressum'


def test_agent_import_rejects_answers_without_evidence(tmp_path):
    import json

    from search_skills.emailver import EmailVerifier

    store = Store(tmp_path / 'c.db').init()
    store.upsert({'company_name': 'Firma', 'company_website': 'https://firma.de',
                  'match_score': 70, 'domain': 'firma.de'})
    verifier = EmailVerifier(None, store)
    key = store.list_recent(1)[0]['dedupe_key']

    cases = [
        {'dedupe_key': key, 'email': 'info@firma.de'},                       # no URL
        {'dedupe_key': key, 'email': 'info@firma.de',
         'evidence_url': 'https://elsewhere.com/x'},                          # wrong host
        {'dedupe_key': key, 'email': 'hr@firma.de',
         'evidence_url': 'https://firma.de/impressum'},                       # role mailbox
        {'dedupe_key': key, 'email': 'j.doe@firma.de',
         'evidence_url': 'https://firma.de/impressum'},                       # placeholder
    ]
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps({'results': cases}), encoding='utf-8')
    report = verifier.import_results(str(path))
    assert report['accepted'] == 0
    assert report['rejected'] == 4
    assert store.get(key)['company_email'] == ''

def test_grade_weights_match_the_spec():
    from search_skills.emailver import grade_of

    assert grade_of('valid', 'own_site') == 'A'
    assert grade_of('catch_all', 'own_site') == 'B'
    assert grade_of('smtp_unreachable', 'own_site') == 'C'
    assert grade_of('greylisted', 'extract') == 'C'
    assert grade_of('smtp_unreachable', 'unknown-source') == ''
    assert grade_of('invalid', 'own_site') == ''
    assert grade_of('no_mx', 'own_site') == ''


def test_existing_email_without_verdict_is_only_verified(tmp_path):
    store = Store(tmp_path / 'c.db').init()
    store.upsert({'company_name': 'Firma', 'company_website': 'https://firma.de',
                  'company_email': 'info@firma.de', 'domain': 'firma.de',
                  'match_score': 60, 'email_status': 'found'})
    lead = store.list_recent(1)[0]
    verifier = EmailVerifier(None, store)
    calls = {'searched': 0}

    async def no_search(*a, **k):
        calls['searched'] += 1
        return []

    verifier.client = type('C', (), {'search': no_search})()
    outcome = asyncio.run(verifier.verify(lead))
    assert calls['searched'] == 0
    assert outcome['email_verdict'] == 'valid'
    assert outcome['email_grade'] == 'A'
    assert outcome['company_email'] == 'info@firma.de'
