'''Offline tests for LLM seed generation.'''

import asyncio
import json

from search_skills.seeds import SeedBuilder, clean_seed
from search_skills.store import Store


def test_clean_seed_normalises_and_rejects():
    assert clean_seed({'name': 'Acme GmbH', 'domain': 'https://www.acme.de/x'})['domain'] == 'acme.de'
    assert clean_seed({'name': 'Acme', 'domain': 'not a domain'}) is None
    assert clean_seed({'name': '', 'domain': 'acme.de'}) is None
    assert clean_seed('nope') is None


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.reply


def test_generate_filters_duplicates_and_dead_domains(tmp_path, monkeypatch):
    store = Store(tmp_path / 'c.db').init()
    store.upsert({'company_name': 'Old Co', 'company_website': 'https://old.de', 'domain': 'old.de'})
    reply = json.dumps({'companies': [
        {'name': 'Fresh GmbH', 'domain': 'fresh.de', 'country': 'Germany'},
        {'name': 'Old Co', 'domain': 'old.de', 'country': 'Germany'},
        {'name': 'Dead AG', 'domain': 'dead-domain-xyz.de', 'country': 'Germany'},
    ]})
    builder = SeedBuilder(FakeLLM(reply), store)

    async def fake_mx(domain):
        return domain != 'dead-domain-xyz.de'

    monkeypatch.setattr('search_skills.seeds.mx_ok', fake_mx)
    result = asyncio.run(builder.generate(3, country='Germany'))
    names = [s['name'] for s in result['seeds']]
    assert names == ['Fresh GmbH']
    assert result['stats']['duplicate'] == 1
    assert result['stats']['dead'] == 1


def test_save_marks_leads_pending_with_domain(tmp_path):
    store = Store(tmp_path / 'c.db').init()
    builder = SeedBuilder(FakeLLM('{}'), store)
    saved = builder.save([{'name': 'Fresh GmbH', 'domain': 'fresh.de',
                           'country': 'Germany', 'industry': 'Machinery',
                           'why_fit': '文档量大'}])
    assert saved == 1
    row = store.list_recent(1)[0]
    assert row['domain'] == 'fresh.de'
    assert row['email_status'] == 'pending'
    assert row['source_url'] == 'llm-seed'

def test_prompt_is_filled_from_the_icp():
    from search_skills.leadgen import ICP
    from search_skills.seeds import build_prompt

    icp = ICP(industries=['Machinery'], titles=['Head of Service'],
              geography=['Germany'], company_size='100-800 employees',
              product='企业知识库远程部署与技术支持', signals=['售后文档量大'])
    text = build_prompt(icp, 10)
    assert '企业知识库远程部署与技术支持' in text
    assert 'Head of Service' in text
    assert '100-800 employees' in text
    assert '售后文档量大' in text
    assert '{{COUNT}}' not in text and '{{PRODUCT}}' not in text


def test_prompt_handles_an_icp_without_signals():
    from search_skills.leadgen import ICP
    from search_skills.seeds import build_prompt

    text = build_prompt(ICP(product='', signals=[]), 5)
    assert '{{PRODUCT}}' not in text
    assert '自己反推' in text
