'''Tests for contact enrichment workflow.'''

from search_skills.contacts import looks_generic, looks_named, validate_contact_result
from search_skills.store import Store


def test_looks_generic():
    assert looks_generic('info@acme.com')
    assert looks_generic('contact@acme.de')
    assert looks_generic('sales@firma.ch')
    assert looks_generic('kontakt@example.de')
    assert not looks_generic('j.doe@acme.com')
    assert not looks_generic('jane.doe@acme.com')
    assert not looks_generic('john@acme.com')
    assert not looks_generic('sarah.smith@example.org')


def test_looks_named():
    assert looks_named('Jane Doe (VP Sales)')
    assert looks_named('John Smith (CEO)')
    assert looks_named('Sarah Lee (Head of Growth)')
    assert looks_named('张三 (销售总监)')
    assert not looks_named('')
    assert not looks_named('info')
    assert not looks_named('Sales Team')
    assert not looks_named('Mr.')


def test_validate_contact_result_accepts_good_submissions():
    ok, reason = validate_contact_result(
        'acme.com', 'Jane Doe', 'VP Sales', 'jane@acme.com',
        'https://acme.com/team',
    )
    assert ok
    assert reason == ''


def test_validate_contact_result_accepts_no_direct_email():
    ok, _ = validate_contact_result(
        'acme.com', 'John Smith', 'CEO', '',
        'https://acme.com/about',
    )
    assert ok


def test_validate_contact_result_rejects_missing_evidence():
    ok, reason = validate_contact_result(
        'acme.com', 'Jane Doe', 'VP Sales', 'jane@acme.com', '',
    )
    assert not ok
    assert 'missing evidence' in reason


def test_validate_contact_result_rejects_wrong_domain():
    ok, reason = validate_contact_result(
        'acme.com', 'Jane Doe', 'VP Sales', 'jane@acme.com',
        'https://elsewhere.com/team',
    )
    assert not ok
    assert 'company domain' in reason or 'press site' in reason


def test_validate_contact_result_accepts_press_release():
    ok, _ = validate_contact_result(
        'acme.com', 'Jane Doe', 'VP Sales', 'jane@acme.com',
        'https://www.prnewswire.com/news-releases/acme-announces-new-vp-123.html',
    )
    assert ok


def test_validate_contact_result_rejects_generic_email():
    ok, reason = validate_contact_result(
        'acme.com', 'Jane Doe', 'VP Sales', 'info@acme.com',
        'https://acme.com/team',
    )
    assert not ok
    assert 'generic' in reason


def test_validate_contact_result_rejects_email_wrong_domain():
    ok, reason = validate_contact_result(
        'acme.com', 'Jane Doe', 'VP Sales', 'jane@elsewhere.com',
        'https://acme.com/team',
    )
    assert not ok
    assert 'company domain' in reason


def test_export_and_import_roundtrip(tmp_path):
    import json

    from search_skills.contacts import ContactEnricher

    store = Store(tmp_path / 'c.db').init()
    store.upsert({
        'company_name': 'Firma',
        'company_website': 'https://firma.de',
        'company_email': 'info@firma.de',
        'match_score': 70,
        'domain': 'firma.de',
    })
    enricher = ContactEnricher(store)

    tasks_path = tmp_path / 'contact_tasks.json'
    out = enricher.export_tasks(str(tasks_path), limit=5)
    assert out['exported'] == 1
    payload = json.loads(tasks_path.read_text(encoding='utf-8'))
    key = payload['tasks'][0]['dedupe_key']
    assert store.get(key)['contact_status'] == 'needs_contact'

    results = {
        'results': [
            {
                'dedupe_key': key,
                'name': 'Hans Müller',
                'title': 'Geschäftsführer',
                'email': 'h.mueller@firma.de',
                'evidence_url': 'https://firma.de/impressum',
            }
        ]
    }
    results_path = tmp_path / 'contact_results.json'
    results_path.write_text(json.dumps(results), encoding='utf-8')
    report = enricher.import_results(str(results_path))
    assert report['accepted'] == 1
    assert report['rejected'] == 0
    saved = store.get(key)
    assert 'Hans Müller' in saved['company_perple']
    assert 'Geschäftsführer' in saved['company_perple']
    assert saved['company_email'] == 'h.mueller@firma.de'
    assert saved['contact_source'] == 'agent'
    assert saved['contact_evidence'] == 'https://firma.de/impressum'


def test_import_rejects_bad_answers(tmp_path):
    import json

    from search_skills.contacts import ContactEnricher

    store = Store(tmp_path / 'c.db').init()
    store.upsert({
        'company_name': 'Firma',
        'company_website': 'https://firma.de',
        'company_email': 'info@firma.de',
        'match_score': 70,
        'domain': 'firma.de',
    })
    enricher = ContactEnricher(store)
    key = store.list_recent(1)[0]['dedupe_key']

    cases = [
        {'dedupe_key': key, 'name': 'Hans', 'title': 'CEO'},
        {'dedupe_key': key, 'name': 'Hans Müller', 'title': 'CEO', 'evidence_url': 'nope'},
        {'dedupe_key': key, 'name': 'Hans Müller', 'title': 'CEO',
         'evidence_url': 'https://elsewhere.com/x'},
        {'dedupe_key': key, 'name': 'Hans Müller', 'title': 'CEO', 'email': 'info@firma.de',
         'evidence_url': 'https://firma.de/team'},
    ]
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(cases), encoding='utf-8')
    report = enricher.import_results(str(path))
    assert report['accepted'] == 0
    assert report['rejected'] == 4
