'''Offline tests for the lead finder: mining, roles, and the ICP gate.'''

import asyncio
import json

from search_skills.client import FetchPage, SearchHit
from search_skills.leadgen import LeadGen, Seen, heuristic_topics, host_score, norm_name
from search_skills.pipeline import ICP
from search_skills.store import Store


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.reply

    def check_credentials(self):
        return None

    async def search(self, query, count=6, **kwargs):
        return []

    async def fetch(self, url):
        return FetchPage(url=url, content='')


def build(reply, seen_path=None):
    store = Store(':memory:').init()
    seen = Seen(seen_path) if seen_path else None
    return LeadGen(FakeLLM(reply), store, ICP(industries=['Manufacturing'], geography=['China']),
                   seen=seen)


def test_mine_parses_role_and_hint():
    reply = json.dumps({'companies': [
        {'name': '苏州精工', 'hint': '在招 MES 工程师', 'role': 'buyer'},
        {'name': '某某云', 'hint': '知识库 SaaS', 'role': 'vendor'},
        {'name': '某某物流', 'hint': '', 'role': '???'},
    ]}, ensure_ascii=False)
    gen = build(reply)
    page = FetchPage(url='https://x.com/list', content='公司名单 ' * 80)
    names = asyncio.run(gen.mine(page, 'https://x.com/list'))
    assert names[0].role == 'buyer'
    assert names[0].hint == '在招 MES 工程师'
    assert names[1].role == 'vendor'
    assert names[2].role == 'unknown'


def test_icp_gate_drops_low_scores_and_foreign_industries():
    gen = build('{}')
    assert gen._fits_icp({'match_score': 20, 'industry': 'Manufacturing'}) is False
    assert gen._fits_icp({'match_score': 35, 'industry': 'Biotech'}) is False
    assert gen._fits_icp({'match_score': 55, 'industry': 'Biotech'}) is True
    assert gen._fits_icp({'match_score': 45, 'industry': 'Manufacturing'}) is True
    assert gen._fits_icp({'match_score': 45, 'industry': ''}) is True


def test_heuristic_topics_ask_for_buyer_signals():
    cn = heuristic_topics(ICP(industries=['Manufacturing'], geography=['China']), 4)
    assert any('招聘' in q or '招标' in q or '参展' in q for q in cn)
    assert not any('best' in q.lower() for q in cn)
    en = heuristic_topics(ICP(industries=['SaaS'], geography=['Germany']), 3)
    assert any('hiring' in q or 'tender' in q or 'exhibitor' in q for q in en)


def test_norm_name_strips_suffixes_and_pipes():
    assert norm_name('Spekit Inc.') == 'Spekit'
    assert norm_name('Acme | Contact') == 'Acme'
    assert norm_name('  * Beta LLC ') == 'Beta'


def test_host_score_rewards_brand_domains():
    assert host_score('spekit.com', 'Spekit') == 1.0
    assert host_score('dh-robotics.com', 'DH Robotics') > 0.5
    assert host_score('clari.com', 'Salesloft') == 0.0


def test_junk_names_are_rejected():
    from search_skills.leadgen import looks_like_junk_name

    assert looks_like_junk_name('About this website')
    assert looks_like_junk_name('Privacy Policy')
    assert looks_like_junk_name('DEVworkplaces.com')
    assert not looks_like_junk_name('LUGATO')
    assert not looks_like_junk_name('大寰机器人')


def test_public_sector_and_multilingual_nav_are_rejected():
    from search_skills.leadgen import is_public_sector, looks_like_junk_name

    assert is_public_sector('Gemeente Amstelveen', 'amstelveen.nl')
    assert is_public_sector('Anything', 'foo.bund.de')
    assert not is_public_sector('LUGATO', 'lugato.de')
    assert looks_like_junk_name('Over deze website')
    assert looks_like_junk_name('Contact gemeente Amstelveen')
    assert looks_like_junk_name('Impressum')


def test_rounds_rotate_signal_and_country():
    from search_skills.leadgen import SIGNAL_BUCKETS

    gen = build('{}')
    gen.seen.round = 0
    gen.icp.geography = ['Germany', 'Netherlands', 'France']
    seen_focus = []
    for _ in range(3):
        label, geo, _key = gen.focus()
        seen_focus.append((label, geo))
        gen.seen.round += 1
    assert len({label for label, _geo in seen_focus}) == 3
    assert [geo for _label, geo in seen_focus] == ['Germany', 'Netherlands', 'France']
    assert len(SIGNAL_BUCKETS) >= 5


def test_used_queries_are_never_asked_twice(tmp_path):
    reply = json.dumps({'queries': ['old query', 'new query']})
    gen = build(reply, tmp_path / 'seen.json')
    gen.seen.queries = ['old query']
    gen.seen.round = 0
    topics = asyncio.run(gen.plan_topics(2))
    assert 'old query' not in topics
    assert 'new query' in topics


def test_list_pages_get_a_bigger_budget():
    from search_skills.client import SearchHit

    gen = build('{}')
    gen.pages_per_topic = 2
    assert gen._page_budget([SearchHit(title='2026 Hannover Messe Ausstellerliste')]) == 4
    assert gen._page_budget([SearchHit(title='Random blog post')]) == 2



def test_non_buyer_depends_on_icp():
    from search_skills.leadgen import is_non_buyer

    consultancy = {'company_name': 'Acme Partners', 'domain': 'acme.com', 'industry': 'Consulting'}
    # Dropped when the ICP is about something else...
    assert is_non_buyer(consultancy, ['Manufacturing'])
    # ...but kept when consultancies are the target.
    assert not is_non_buyer(consultancy, ['Consulting'])
    # Public bodies and associations are dropped for any ICP.
    assert is_non_buyer({'company_name': 'X', 'domain': 'x.de', 'industry': 'Association'}, ['Association'])
    assert is_non_buyer({'company_name': 'Gemeente Utrecht', 'domain': 'utrecht.nl', 'industry': ''}, [])
    assert not is_non_buyer({'company_name': 'Haimer', 'domain': 'haimer.com',
                             'industry': 'Industrial Machinery'}, ['Manufacturing'])
