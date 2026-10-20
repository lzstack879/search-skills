'''Offline tests for exhibitor/association page -> seeds.csv.'''

from search_skills.seedpages import build_seed_rows, links_from_markdown, write_csv


def test_links_skip_navigation_and_directories():
    md = ('[Login](https://expo.de/login) [Acme GmbH](https://acme.de) '
          '[LinkedIn](https://www.linkedin.com/company/acme) [Contact](https://expo.de/contact)')
    links = links_from_markdown(md, 'expo.de')
    assert links == {'acme gmbh': 'https://acme.de'}


def test_build_rows_merges_links_and_keeps_names():
    companies = [
        {'name': 'Acme GmbH', 'website': ''},
        {'name': 'Beta AG', 'website': 'https://www.beta.ch/contact'},
        {'name': 'No Site Ltd', 'website': ''},
    ]
    rows = build_seed_rows(companies, {'acme gmbh': 'https://acme.de'}, 'https://expo.de/list')
    assert [r['domain'] for r in rows] == ['acme.de', 'beta.ch', '']
    assert rows[0]['source_url'] == 'https://expo.de/list'


def test_write_csv_has_bom_and_four_columns(tmp_path):
    path = tmp_path / 'seeds.csv'
    rows = [{'company': 'Acme', 'domain': 'acme.de', 'country': 'Germany', 'industry': 'Machinery'}]
    assert write_csv(rows, str(path)) == 1
    raw = path.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')
    text = path.read_text(encoding='utf-8-sig')
    assert text.splitlines()[0] == 'company,domain,country,industry'


def test_name_only_rows_survive_until_resolve():
    rows = build_seed_rows([{'name': 'Acme GmbH'}, {'name': 'Beta AG'}], {}, 'https://expo.de/list')
    assert [r['domain'] for r in rows] == ['', '']
    assert [r['company'] for r in rows] == ['Acme GmbH', 'Beta AG']

def test_diagnose_flags_js_apps_and_name_only_pages():
    from search_skills.seedpages import diagnose_page

    thin = diagnose_page('<div id="app"></div>' * 3, 'https://expo.de/floorplan')
    assert 'JS-rendered' in thin and 'wait-for' in thin
    name_only = diagnose_page('# Aussteller\n\nAcme GmbH\nBeta AG\n' * 40, 'https://expo.de/list')
    assert 'name-only' in name_only
    good = diagnose_page('# Liste\n\n[Acme](https://acme.de) ' * 40, 'https://expo.de/list')
    assert good == ''

def test_json_rows_maps_a_fair_api_payload():
    from search_skills.seedpages import json_rows

    payload = {'data': {'items': [
        {'name': 'Acme GmbH', 'website': 'https://acme.de', 'country': 'Germany'},
        {'name': 'No Site', 'website': ''},
        {'name': 'Beta AG', 'website': 'https://www.beta.ch'},
    ]}}
    rows = json_rows(payload, path='data.items')
    assert [r['domain'] for r in rows] == ['acme.de', 'beta.ch']
    assert rows[0]['company'] == 'Acme GmbH'
    assert json_rows({'data': []}, path='data.items') == []
