'''Adaptive ICP adjustment based on search performance analysis.

This module enables the lead generation system to automatically adjust the ICP
(Ideal Customer Profile) when search results become repetitive or low-quality.
'''

from __future__ import annotations

import copy
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from .client import DeepSeekClient
from .pipeline import ICP, parse_json
from .store import Store

logger = logging.getLogger('search_skills.adaptive')


@dataclass
class RoundStats:
    '''Statistics for one search round, used to judge whether ICP needs adjustment.'''

    round_id: int
    timestamp: str
    focus_signal: str      # 招聘/招标/展会/融资/名录
    focus_geo: str         # Germany/France/...
    queries: list[str]     # 本轮生成的查询
    candidates: int        # 挖到的候选公司名
    resolved: int          # 成功解析域名
    fetched: int           # 抓取主页
    inserted: int          # 最终入库（新增）
    updated: int           # 更新已有记录
    dropped: int           # 被过滤掉
    skipped: int           # 去重跳过
    avg_score: float       # 平均 match_score
    time_cost: float       # 本轮耗时（秒）

    @classmethod
    def from_dict(cls, data: dict) -> 'RoundStats':
        records = data.get('records') or []
        avg = sum(r.get('match_score', 0) for r in records) / max(1, len(records))
        return cls(
            round_id=data.get('round_id', 0),
            timestamp=datetime.now().isoformat(timespec='seconds'),
            focus_signal=data.get('focus', '').split('@')[0].strip(),
            focus_geo=data.get('focus', '').split('@')[-1].strip(),
            queries=data.get('queries', []),
            candidates=data.get('candidates', 0),
            resolved=data.get('resolved', 0),
            fetched=data.get('fetched', 0),
            inserted=data.get('inserted', 0),
            updated=data.get('updated', 0),
            dropped=data.get('dropped', 0),
            skipped=data.get('skipped', 0),
            avg_score=avg,
            time_cost=data.get('time_cost', 0.0),
        )


def _should_trigger(recent: list[RoundStats], min_rounds: int = 5) -> tuple[bool, str]:
    '''Heuristic: decide whether to trigger LLM analysis.'''
    if len(recent) < min_rounds:
        return False, 'not enough rounds'

    # Rule 1: 连续 3 轮新增 < 2 家
    last_3 = recent[-3:]
    if all(s.inserted < 2 for s in last_3):
        return True, 'consecutive 3 rounds with < 2 insertions'

    # Rule 2: 最近 5 轮平均入库 < 2 家
    avg_inserted = sum(s.inserted for s in recent[-5:]) / 5
    if avg_inserted < 2.0:
        return True, f'recent 5 rounds avg insertions = {avg_inserted:.1f} < 2.0'

    # Rule 3: 最近 5 轮 dropped 率 > 60%
    total_fetched = sum(s.fetched for s in recent[-5:])
    total_dropped = sum(s.dropped for s in recent[-5:])
    if total_fetched > 0 and total_dropped / total_fetched > 0.6:
        return True, f'recent 5 rounds drop rate = {total_dropped}/{total_fetched} > 60%'

    return False, 'metrics are healthy'


async def analyze_and_adjust_icp(
    icp: ICP,
    recent_stats: list[RoundStats],
    store: Store,
    client: DeepSeekClient,
    seen_counts: dict,
    *,
    force: bool = False,
    min_rounds: int = 5,
) -> tuple[ICP, str, bool]:
    '''Analyze recent search performance and adjust ICP if needed.

    Returns:
        (new_icp, reason, adjusted)
        - new_icp: the adjusted ICP (same as input if no adjustment)
        - reason: why adjusted (or why not)
        - adjusted: True if ICP was changed
    '''
    if not force:
        should, reason = _should_trigger(recent_stats, min_rounds)
        if not should:
            return icp, f'No adjustment needed: {reason}', False

    # Build feedback for the model
    try:
        total_stored = store.stats().get('companies', 0)
    except Exception:
        total_stored = 0  # In-memory DB or table not yet created

    feedback = {
        'current_icp': {
            'industries': icp.industries,
            'titles': icp.titles,
            'geography': icp.geography,
            'company_size': icp.company_size,
            'product': icp.product,
        },
        'recent_rounds': [
            {
                'round': s.round_id,
                'signal': s.focus_signal,
                'geo': s.focus_geo,
                'inserted': s.inserted,
                'dropped': s.dropped,
                'skipped': s.skipped,
                'avg_score': round(s.avg_score, 1),
                'sample_queries': s.queries[:2],
            }
            for s in recent_stats[-10:]
        ],
        'total_stored': total_stored,
        'seen_names': seen_counts.get('names', 0),
        'seen_hosts': seen_counts.get('hosts', 0),
        'seen_queries': seen_counts.get('queries', 0),
    }

    # Calculate summary metrics
    last_5 = recent_stats[-5:]
    avg_inserted = sum(s.inserted for s in last_5) / len(last_5)
    avg_dropped = sum(s.dropped for s in last_5) / len(last_5)

    prompt = f'''你是潜在客户搜索优化器。当前 ICP 已经搜索了 {len(recent_stats)} 轮，效果如下：

{json.dumps(feedback, ensure_ascii=False, indent=2)}

**观察**：
- 最近 5 轮平均入库：{avg_inserted:.1f} 家/轮
- 最近 5 轮平均丢弃：{avg_dropped:.1f} 家/轮
- 已去重公司名：{seen_counts.get('names', 0)} 个
- 已去重域名：{seen_counts.get('hosts', 0)} 个
- 已用查询：{seen_counts.get('queries', 0)} 条

**任务**：判断是否需要调整 ICP，如果需要，提出具体修改方案。

**调整原则**：
1. **所有调整都要服务于 ICP 的「产品」**：新增的行业/地区里，必须仍然有公司需要这个产品。
2. **地区扩展**：某个地区连续多轮命中率低 → 添加语言/市场相近的相邻地区。
3. **行业扩展**：当前行业搜索充分 → 添加与现有行业相邻、且同样需要该产品的行业。
4. **职位泛化**：高级职位找不到 → 加入同一职能的中级职位（如 Head of X → X Manager）。
5. **地区收缩**：某个地区连续 8 轮都是重复/丢弃 → 暂时移除，给其他地区更多机会。
6. **不要一次调整太多**：最多改 2 个维度（比如：地区 +1 + 行业 +1）。

**输出 JSON**（严格格式）：
```json
{{
  "need_adjust": true/false,
  "reason": "为什么需要/不需要调整（一句话）",
  "adjustments": {{
    "add_geography": ["新增的国家"],
    "add_industries": ["新增的行业"],
    "add_titles": ["新增的职位"],
    "remove_geography": ["暂时移除的国家"],
    "comment": "调整的具体逻辑"
  }},
  "expected_improvement": "预期效果（一句话）"
}}
```

**示例**：
```json
{{
  "need_adjust": true,
  "reason": "<地区 A> 连续 5 轮新增 < 2 家，搜索接近枯竭",
  "adjustments": {{
    "add_geography": ["<相邻地区 B>"],
    "add_industries": ["<相邻行业>"],
    "add_titles": [],
    "remove_geography": [],
    "comment": "扩展到语言相近的邻近市场 + 一个同样需要该产品的相邻行业"
  }},
  "expected_improvement": "<一句话说明预期新增的公司来源>"
}}
```
'''

    try:
        reply = await client.complete(prompt, max_tokens=1000, temperature=0.3)
        data = parse_json(reply)
    except Exception as e:
        logger.warning('LLM analysis failed: %s', e)
        return icp, f'Analysis error: {str(e)[:100]}', False

    if not data.get('need_adjust'):
        reason = data.get('reason', 'No adjustment needed')
        logger.info('ICP analysis: %s', reason)
        return icp, reason, False

    # Apply adjustments
    new_icp = copy.deepcopy(icp)
    adj = data.get('adjustments') or {}
    changes = []

    if adj.get('add_geography'):
        for geo in adj['add_geography']:
            if geo not in new_icp.geography:
                new_icp.geography.append(geo)
                changes.append(f'地区+{geo}')

    if adj.get('remove_geography'):
        for geo in adj['remove_geography']:
            if geo in new_icp.geography and len(new_icp.geography) > 1:
                new_icp.geography.remove(geo)
                changes.append(f'地区-{geo}')

    if adj.get('add_industries'):
        for ind in adj['add_industries']:
            if ind not in new_icp.industries:
                new_icp.industries.append(ind)
                changes.append(f'行业+{ind}')

    if adj.get('add_titles'):
        for title in adj['add_titles']:
            if title not in new_icp.titles:
                new_icp.titles.append(title)
                changes.append(f'职位+{title}')

    if not changes:
        return icp, 'No valid adjustments in LLM response', False

    reason = data.get('reason', '') + ' | ' + ', '.join(changes)
    expected = data.get('expected_improvement', '')
    if expected:
        reason += ' | 预期: ' + expected

    logger.info('ICP adjusted: %s', reason)
    return new_icp, reason, True


def format_adjustment_report(
    old_icp: ICP,
    new_icp: ICP,
    reason: str,
    round_id: int,
) -> str:
    '''Format a human-readable adjustment report.'''
    lines = [
        '',
        '=' * 70,
        f'🔄 ICP 自适应调整 (第 {round_id} 轮)',
        '=' * 70,
        '',
        '【调整原因】',
        reason,
        '',
        '【变更内容】',
    ]

    old_dict = old_icp.to_dict() if hasattr(old_icp, 'to_dict') else {
        'industries': old_icp.industries,
        'titles': old_icp.titles,
        'geography': old_icp.geography,
        'company_size': old_icp.company_size,
    }
    new_dict = new_icp.to_dict() if hasattr(new_icp, 'to_dict') else {
        'industries': new_icp.industries,
        'titles': new_icp.titles,
        'geography': new_icp.geography,
        'company_size': new_icp.company_size,
    }

    if old_dict['geography'] != new_dict['geography']:
        lines.append(f'  地区: {", ".join(old_dict["geography"])}')
        lines.append(f'     → {", ".join(new_dict["geography"])}')

    if old_dict['industries'] != new_dict['industries']:
        lines.append(f'  行业: {", ".join(old_dict["industries"])}')
        lines.append(f'     → {", ".join(new_dict["industries"])}')

    if old_dict['titles'] != new_dict['titles']:
        old_titles = ', '.join(old_dict['titles'][:3])
        new_titles = ', '.join(new_dict['titles'][:3])
        if len(old_dict['titles']) > 3:
            old_titles += f' (+{len(old_dict["titles"]) - 3})'
        if len(new_dict['titles']) > 3:
            new_titles += f' (+{len(new_dict["titles"]) - 3})'
        lines.append(f'  职位: {old_titles}')
        lines.append(f'     → {new_titles}')

    lines.append('')
    lines.append('=' * 70)
    lines.append('')

    return '\n'.join(lines)
