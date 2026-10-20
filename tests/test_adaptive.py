'''Tests for adaptive ICP adjustment.'''

import asyncio
from datetime import datetime

import pytest

from search_skills.adaptive import (
    RoundStats,
    _should_trigger,
    analyze_and_adjust_icp,
    format_adjustment_report,
)
from search_skills.client import DeepSeekClient
from search_skills.pipeline import ICP
from search_skills.store import Store


def test_round_stats_from_dict():
    data = {
        'round_id': 5,
        'focus': '招聘信号 @ Germany',
        'queries': ['query1', 'query2'],
        'candidates': 10,
        'resolved': 8,
        'fetched': 6,
        'inserted': 4,
        'updated': 1,
        'dropped': 1,
        'skipped': 0,
        'records': [
            {'match_score': 70},
            {'match_score': 50},
            {'match_score': 60},
        ],
        'time_cost': 120.5,
    }

    stats = RoundStats.from_dict(data)
    assert stats.round_id == 5
    assert stats.focus_signal == '招聘信号'
    assert stats.focus_geo == 'Germany'
    assert stats.candidates == 10
    assert stats.inserted == 4
    assert stats.avg_score == 60.0  # (70 + 50 + 60) / 3


def test_should_trigger_not_enough_rounds():
    recent = [
        RoundStats(1, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 4, 0, 1, 0, 65.0, 100.0),
        RoundStats(2, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 3, 0, 2, 0, 60.0, 100.0),
    ]
    should, reason = _should_trigger(recent, min_rounds=5)
    assert not should
    assert 'not enough rounds' in reason


def test_should_trigger_consecutive_low_insertions():
    recent = [
        RoundStats(1, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 4, 0, 1, 0, 65.0, 100.0),
        RoundStats(2, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 3, 0, 2, 0, 60.0, 100.0),
        RoundStats(3, datetime.now().isoformat(), 'hiring', 'Germany', [], 8, 5, 4, 1, 0, 3, 0, 55.0, 100.0),
        RoundStats(4, datetime.now().isoformat(), 'hiring', 'Germany', [], 6, 4, 3, 1, 0, 2, 0, 50.0, 100.0),
        RoundStats(5, datetime.now().isoformat(), 'hiring', 'Germany', [], 5, 3, 2, 0, 0, 2, 0, 45.0, 100.0),
    ]
    should, reason = _should_trigger(recent, min_rounds=5)
    assert should
    assert 'consecutive 3 rounds' in reason


def test_should_trigger_high_drop_rate():
    recent = [
        RoundStats(1, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 3, 0, 3, 0, 40.0, 100.0),
        RoundStats(2, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 2, 0, 4, 0, 40.0, 100.0),
        RoundStats(3, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 2, 0, 4, 0, 38.0, 100.0),
        RoundStats(4, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 2, 0, 4, 0, 40.0, 100.0),
        RoundStats(5, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 2, 0, 4, 0, 38.0, 100.0),
    ]
    # Total: fetched=30, dropped=19, rate=63% > 60%, but inserted >= 2 so won't trigger consecutive rule
    should, reason = _should_trigger(recent, min_rounds=5)
    assert should
    assert 'drop rate' in reason


def test_format_adjustment_report():
    old_icp = ICP(
        industries=['Manufacturing'],
        titles=['Head of Service', 'CTO'],
        geography=['Germany'],
        company_size='100-500 employees',
    )
    new_icp = ICP(
        industries=['Manufacturing', 'Industrial Machinery'],
        titles=['Head of Service', 'CTO', 'Service Manager'],
        geography=['Germany', 'Austria'],
        company_size='100-500 employees',
    )

    report = format_adjustment_report(
        old_icp,
        new_icp,
        '德国搜索枯竭 | 地区+Austria, 行业+Industrial Machinery | 预期: 可再找 30-50 家',
        round_id=10,
    )

    assert '第 10 轮' in report
    assert 'Germany' in report
    assert 'Austria' in report
    assert 'Industrial Machinery' in report
    assert '德国搜索枯竭' in report


class FakeClient:
    '''Mock client that returns predefined adjustment JSON.'''

    def __init__(self, need_adjust=True, add_geo=None):
        self.need_adjust = need_adjust
        self.add_geo = add_geo or ['Austria']

    async def complete(self, prompt, max_tokens=1000, temperature=0.3):
        if self.need_adjust:
            return '''```json
{
  "need_adjust": true,
  "reason": "德国连续 5 轮新增 < 2 家",
  "adjustments": {
    "add_geography": %s,
    "add_industries": ["Industrial Machinery"],
    "add_titles": [],
    "remove_geography": [],
    "comment": "扩展到邻国"
  },
  "expected_improvement": "预计可再找 30-50 家"
}
```''' % str(self.add_geo).replace("'", '"')
        else:
            return '''```json
{
  "need_adjust": false,
  "reason": "搜索效果正常",
  "adjustments": {},
  "expected_improvement": ""
}
```'''


def test_analyze_and_adjust_icp_no_trigger():
    icp = ICP(industries=['Manufacturing'], geography=['Germany'])
    recent = [
        RoundStats(i, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 4, 0, 1, 0, 65.0, 100.0)
        for i in range(1, 6)
    ]
    store = Store(':memory:').init()
    client = FakeClient()

    new_icp, reason, adjusted = asyncio.run(
        analyze_and_adjust_icp(
            icp, recent, store, client, {'names': 50, 'hosts': 40, 'queries': 20}, force=False
        )
    )

    assert not adjusted
    assert 'metrics are healthy' in reason


def test_analyze_and_adjust_icp_force():
    icp = ICP(industries=['Manufacturing'], geography=['Germany'])
    recent = [
        RoundStats(i, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 4, 0, 1, 0, 65.0, 100.0)
        for i in range(1, 6)
    ]
    store = Store(':memory:')
    store.init()  # Ensure tables exist
    client = FakeClient(need_adjust=True, add_geo=['Austria', 'Switzerland'])

    new_icp, reason, adjusted = asyncio.run(
        analyze_and_adjust_icp(
            icp, recent, store, client, {'names': 50, 'hosts': 40, 'queries': 20}, force=True
        )
    )

    assert adjusted
    assert 'Austria' in new_icp.geography
    assert 'Switzerland' in new_icp.geography
    assert 'Industrial Machinery' in new_icp.industries
    assert '德国连续 5 轮新增 < 2 家' in reason


def test_analyze_and_adjust_icp_triggered():
    icp = ICP(industries=['Manufacturing'], geography=['Germany'])
    # 连续 3 轮低产出
    recent = [
        RoundStats(1, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 3, 0, 2, 0, 60.0, 100.0),
        RoundStats(2, datetime.now().isoformat(), 'hiring', 'Germany', [], 10, 8, 6, 2, 0, 3, 0, 55.0, 100.0),
        RoundStats(3, datetime.now().isoformat(), 'hiring', 'Germany', [], 8, 5, 4, 1, 0, 3, 0, 50.0, 100.0),
        RoundStats(4, datetime.now().isoformat(), 'hiring', 'Germany', [], 6, 4, 3, 1, 0, 2, 0, 48.0, 100.0),
        RoundStats(5, datetime.now().isoformat(), 'hiring', 'Germany', [], 5, 3, 2, 1, 0, 1, 0, 45.0, 100.0),
    ]
    store = Store(':memory:')
    store.init()
    client = FakeClient(need_adjust=True)

    new_icp, reason, adjusted = asyncio.run(
        analyze_and_adjust_icp(
            icp, recent, store, client, {'names': 100, 'hosts': 80, 'queries': 50}, force=False
        )
    )

    assert adjusted
    assert 'Austria' in new_icp.geography
    assert 'Industrial Machinery' in new_icp.industries


def test_analyze_and_adjust_icp_no_adjustment_needed():
    icp = ICP(industries=['Manufacturing'], geography=['Germany'])
    # 连续 3 轮低产出，但模型判断不需要调整
    recent = [
        RoundStats(i, datetime.now().isoformat(), 'hiring', 'Germany', [], 5, 3, 2, 1, 0, 1, 0, 45.0, 100.0)
        for i in range(1, 6)
    ]
    store = Store(':memory:')
    store.init()
    client = FakeClient(need_adjust=False)

    new_icp, reason, adjusted = asyncio.run(
        analyze_and_adjust_icp(
            icp, recent, store, client, {'names': 20, 'hosts': 15, 'queries': 10}, force=False
        )
    )

    assert not adjusted
    assert '搜索效果正常' in reason
