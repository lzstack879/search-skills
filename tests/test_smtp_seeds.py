'''Offline tests for SMTP verdicts and the seeds.csv entry.'''

import asyncio
import csv

from search_skills.emailver import verdict_from_codes
from search_skills.leadgen import LeadGen, Seen, parse_seeds_csv
from search_skills.pipeline import ICP
from search_skills.store import Store


def test_verdict_prefers_catch_all_detection():
    assert verdict_from_codes(250, 550) == 'valid'
    assert verdict_from_codes(250, 250) == 'catch_all'
    assert verdict_from_codes(250, None) == 'valid'
    assert verdict_from_codes(550, 550) == 'invalid'
    assert verdict_from_codes(451, 250) == 'greylisted'
    assert verdict_from_codes(0, None) == 'smtp_unreachable'


def test_parse_seeds_csv_handles_urls_and_duplicates(tmp_path):
    path = tmp_path / 'seeds.csv'
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['company', 'domain', 'country', 'industry'])
        writer.writerow(['Firma A', 'https://www.firma-a.de/kontakt', 'Germany', 'Machinery'])
        writer.writerow(['Firma A dup', 'firma-a.de', 'Germany', 'Machinery'])
        writer.writerow(['Firma B', 'firma-b.pl', 'Poland', 'Automation'])
        writer.writerow(['Bad', '', '', ''])
    rows = parse_seeds_csv(str(path))
    assert [r['domain'] for r in rows] == ['firma-a.de', 'firma-b.pl']
    assert rows[0]['name'] == 'Firma A'


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url):
        from search_skills.client import FetchPage
        self.calls.append(url)
        return FetchPage(url=url, content=self.pages.get(url, '# Firma\n\nWir bauen Maschinen fuer die Industrie.'))


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def check_credentials(self):
        return None

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return '{"company_name": "Firma A", "industry": "Manufacturing", "match_score": 60, "match_result": "中（60/100）", "summary": "baut Maschinen"}'

    async def search(self, *a, **k):
        return []


def test_seed_round_stores_domains_without_search(tmp_path):
    store = Store(tmp_path / 'c.db').init()
    gen = LeadGen(FakeLLM(), store, ICP(industries=['Manufacturing'], geography=['Germany']),
                  fetcher=FakeFetcher({}), seen=Seen(tmp_path / 'seen.json'))
    stats = asyncio.run(gen.seed_round([{'name': 'Firma A', 'domain': 'firma-a.de',
                                         'country': 'Germany', 'industry': 'Machinery'}]))
    assert stats['fetched'] == 1
    assert stats['inserted'] == 1
    assert gen.client.prompts  # extraction prompt was used
    row = store.list_recent(1)[0]
    assert row['domain'] == 'firma-a.de'
    assert row['source_url'] == 'seeds.csv'
