'''Offline tests for the ICP builder: no network, no API key.'''

import asyncio
import json

import pytest

from search_skills.client import FetchPage
from search_skills.icp_builder import (
    DEFAULTS,
    ask_questions,
    clean_icp,
    from_answers,
    from_site,
    heuristic_from_site,
    normalize_url,
    coverage_of,
    field_statuses,
    fill_gaps,
    gaps_of,
    read_icp,
    template,
    verify_icp,
    write_icp,
)
from search_skills.pipeline import ICP


def test_normalize_url_accepts_bare_domain():
    assert normalize_url('acme.com') == 'https://acme.com'
    assert normalize_url('https://acme.com/pricing?x=1') == 'https://acme.com'
    with pytest.raises(ValueError):
        normalize_url('')


def test_clean_icp_splits_strings_and_dedupes():
    icp = clean_icp({
        'industries': 'SaaS，Marketing Agency、SaaS',
        'titles': ['VP Sales', 'VP Sales', ' Head of Growth '],
        'geography': 'United States',
        'company_size': '10-200 employees',
        'product': 'AI 销售助手',
    })
    assert icp['industries'] == ['SaaS', 'Marketing Agency']
    assert icp['titles'] == ['VP Sales', 'Head of Growth']
    assert icp['product'] == 'AI 销售助手'


def test_clean_icp_falls_back_to_defaults():
    icp = clean_icp({})
    assert icp['industries'] == DEFAULTS['industries']
    assert icp['company_size'] == DEFAULTS['company_size']
    assert ICP.from_dict(icp).industries == DEFAULTS['industries']


def test_write_and_read_roundtrip_keeps_meta_out_of_the_icp():
    icp = {'industries': ['SaaS'], 'titles': ['VP Sales'], 'geography': ['United States'],
           'company_size': '10-200 employees', 'product': 'x'}
    path = write_icp(icp, '/tmp/_icp_test.json', {'method': 'website', 'confidence': 80})
    loaded = read_icp(path)
    assert loaded['_meta']['method'] == 'website'
    assert ICP.from_dict(loaded).titles == ['VP Sales']


def test_ask_questions_uses_defaults_when_empty():
    answers = ask_questions(input_fn=lambda _prompt: '')
    assert answers['industries'] == 'SaaS, Marketing Agency'
    assert set(answers) == {'product', 'industries', 'titles', 'geography', 'company_size'}


def test_from_answers_without_llm_maps_fields():
    result = asyncio.run(from_answers({
        'product': 'AI 销售助手',
        'industries': 'SaaS, 营销机构',
        'titles': 'VP Sales, 增长负责人',
        'geography': 'United States',
        'company_size': '10-200 employees',
    }, use_llm=False))
    icp = result['icp']
    assert icp['industries'] == ['SaaS', '营销机构']
    assert result['meta']['method'] == 'interview'
    assert icp['product'] == 'AI 销售助手'


def test_from_answers_rejects_empty_input():
    with pytest.raises(ValueError):
        asyncio.run(from_answers({'product': '', 'industries': ''}, use_llm=False))


def test_heuristic_from_site_spots_keywords():
    data = heuristic_from_site('We build ecommerce software for Shopify stores in the United States.')
    assert 'E-commerce' in data['industries']
    assert 'United States' in data['geography']


class FakeSiteClient:
    def __init__(self, content, reply=None):
        self.content = content
        self.reply = reply or ''
        self.fetched = []
        self.prompts = []

    def check_credentials(self):
        return None

    async def fetch(self, url):
        self.fetched.append(url)
        return FetchPage(url=url, title='Acme', content=self.content)

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.reply


def test_from_site_uses_the_prompt_and_returns_meta():
    reply = json.dumps({
        'industries': ['SaaS'], 'titles': ['VP Marketing', 'Head of Growth'],
        'geography': ['United States'], 'company_size': '10-200 employees',
        'product': 'AI 销售助手', 'confidence': 77, 'evidence': ['定价页'],
    })
    client = FakeSiteClient('Acme sells AI sales software. ' * 40, reply)
    result = asyncio.run(from_site('acme.com', pages=1, client=client))
    assert result['icp']['titles'] == ['VP Marketing', 'Head of Growth']
    assert result['meta']['method'] == 'website'
    assert result['meta']['source'] == 'https://acme.com'
    assert result['meta']['confidence'] == 77
    assert 'ICP' in client.prompts[0]
    assert client.fetched[0] == 'https://acme.com'


def test_from_site_falls_back_when_the_model_is_garbage():
    client = FakeSiteClient('We sell ecommerce tools in the United States. ' * 20, 'not json')
    result = asyncio.run(from_site('https://shop.example', pages=0, client=client))
    assert 'E-commerce' in result['icp']['industries']


def test_verify_icp_drops_industries_the_page_never_mentions():
    icp = {'industries': ['Manufacturing', 'E-commerce', 'SaaS'],
           'titles': ['COO'], 'geography': ['China', 'United States'],
           'company_size': '50-500 employees', 'product': 'x'}
    verified, notes = verify_icp(
        icp, 'We build MES for manufacturing teams. SaaS delivery. 合作伙伴在中国。'
    )
    assert verified['industries'] == ['Manufacturing', 'SaaS']
    assert verified['geography'] == ['China']
    assert any('E-commerce' in note for note in notes)


def test_verify_icp_falls_back_to_global_without_region_evidence():
    icp = {'industries': ['SaaS'], 'titles': ['VP Sales'], 'geography': ['United States'],
           'company_size': '10-200 employees', 'product': 'x'}
    verified, notes = verify_icp(icp, 'We sell software to teams.')
    assert verified['geography'] == ['Global']
    assert any('Global' in note for note in notes)


def test_field_statuses_marks_defaults_and_missing():
    icp = {'industries': ['Manufacturing'], 'titles': ['VP Sales'], 'geography': ['Global'],
           'company_size': '10-200 employees', 'product': ''}
    statuses = field_statuses({'industries': ['Manufacturing']}, icp, 'we do manufacturing mes')
    assert statuses['industries']['status'] == 'evidence'
    assert statuses['titles']['status'] == 'default'
    assert statuses['product']['status'] == 'missing'
    assert gaps_of(statuses) == ['titles', 'geography', 'company_size', 'product']
    assert coverage_of(statuses) < 0.5


def test_fill_gaps_merges_only_the_answered_fields():
    result = {
        'icp': {'industries': ['Manufacturing'], 'titles': ['VP Sales'], 'geography': ['Global'],
                'company_size': '10-200 employees', 'product': 'x'},
        'meta': {'fields': {k: {'status': 'default'} for k in
                            ('industries', 'titles', 'geography', 'company_size', 'product')},
                 'gaps': ['titles'], 'coverage': 0.2},
    }
    filled = asyncio.run(fill_gaps(result, {'company_size': '50-500 employees'}, use_llm=False))
    assert filled['icp']['company_size'] == '50-500 employees'
    assert filled['icp']['titles'] == ['VP Sales']          # untouched
    assert filled['meta']['fields']['company_size']['status'] == 'user'


def test_ask_questions_can_target_a_subset():
    asked = []

    def fake(prompt):
        asked.append(prompt)
        return ''

    answers = ask_questions(keys=['company_size'], input_fn=fake)
    assert list(answers) == ['company_size']
    assert len(asked) == 1


def test_template_has_the_pipeline_keys():
    assert set(template()) == {'industries', 'titles', 'geography', 'company_size', 'product'}
