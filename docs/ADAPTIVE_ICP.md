# Skills 自适应 ICP 设计方案

## 问题场景

**现状**：使用固定 ICP 搜索 1 小时后，发现：
- 同样的公司反复出现（seen.json 里已经 200+ 条）
- 搜索结果质量下降（match_score 越来越低）
- 话题查询开始重复（queries 列表已经 50+ 条）

**本质问题**：Skills 是"执行机器"，不是"思考机器"
- ❌ 无法判断"搜索已经枯竭"
- ❌ 无法自主调整 ICP（扩展行业、换地区、改信号类型）
- ❌ 无法学习"什么样的查询命中率高"

---

## 当前架构的限制

### 1. ICP 是静态输入，不是动态变量

```python
# 当前设计
icp = ICP.from_dict(json.loads(Path('data/icp.json').read_text()))
gen = LeadGen(client, store, icp)
await gen.round(count=4)  # 每轮用同样的 ICP
```

**问题**：
- ICP 只在启动时读取一次
- `loop` 模式跑 12 小时，始终用同一个 ICP
- 即使发现"德国搜不到了"，也不会自动切到法国

### 2. Seen 机制只防重复，不触发调整

```python
# search_skills/leadgen.py:141-177
class Seen:
    def __init__(self):
        self.names: set[str] = set()  # 已试过的公司名
        self.hosts: set[str] = set()  # 已抓过的域名
        self.queries: list[str] = []  # 已用过的查询
        self.round: int = 0           # 轮次计数器
```

**问题**：
- 只记录"用过什么"，不记录"效果如何"
- 没有"连续 5 轮都是重复 → 该换策略了"的判断
- `self.round` 只用于轮换信号类型，不用于触发 ICP 调整

### 3. 话题规划无反馈循环

```python
# search_skills/leadgen.py:331-357
async def plan_topics(self, count: int = 4) -> list[str]:
    label, geo, _key = self.focus()  # 按 round % 5 轮换信号
    recent = self.seen.queries[-40:]
    prompt = load_prompt('search_topics') + '\n已用过的查询：' + '\n'.join(recent)
    reply = await self.client.complete(prompt)
    queries = parse_json(reply).get('queries')
    return queries[:count]
```

**问题**：
- 只告诉模型"这些查询用过了"，没告诉"这些查询效果如何"
- 模型不知道"德国 Manufacturing 查了 20 次，只找到 3 家新公司"
- 无法学习"融资信号比招聘信号命中率高 2 倍"

---

## 自适应 ICP 的三个层次

### 层次 1：基于统计的自动扩展（无需 LLM）

**触发条件**：
```python
# 连续 N 轮新增公司 < 阈值
if last_N_rounds_avg_inserted < 2:
    trigger_icp_expansion()
```

**扩展策略**：
```python
def expand_icp(icp: ICP, seen: Seen) -> ICP:
    # 策略 1: 地区轮换
    if len(seen.hosts) > 100:
        unused_geos = [g for g in ALL_EUROPE if g not in icp.geography]
        if unused_geos:
            icp.geography.append(unused_geos[0])
            log.info('ICP 扩展：地区 +%s', unused_geos[0])
    
    # 策略 2: 行业相邻扩展
    if icp.industries == ['Manufacturing']:
        icp.industries.append('Industrial Machinery')  # 手工映射表
    
    # 策略 3: 职位泛化
    if 'Head of Service' in icp.titles:
        icp.titles.append('Service Manager')  # 从 Head 降到 Manager
    
    return icp
```

**优点**：简单、确定性、无 token 成本  
**缺点**：需要预定义扩展规则，无法处理意外情况

---

### 层次 2：基于效果分析的 LLM 调整（轻度思考）

**收集反馈数据**：
```python
@dataclass
class RoundStats:
    round_id: int
    focus_signal: str      # 招聘/招标/展会/融资/名录
    focus_geo: str         # Germany/France/...
    queries: list[str]     # 本轮生成的查询
    candidates: int        # 挖到的候选名
    resolved: int          # 成功解析域名
    inserted: int          # 最终入库
    dropped: int           # 被过滤掉
    avg_score: float       # 平均 match_score
    time_cost: float       # 耗时（秒）
```

**定期触发分析**（每 5-10 轮）：
```python
async def analyze_and_adjust_icp(
    icp: ICP,
    recent_stats: list[RoundStats],
    store: Store
) -> tuple[ICP, str]:
    '''让模型分析最近的搜索效果，提出 ICP 调整建议'''
    
    # 构造反馈
    feedback = {
        'current_icp': icp.to_dict(),
        'recent_rounds': [
            {
                'round': s.round_id,
                'signal': s.focus_signal,
                'geo': s.focus_geo,
                'inserted': s.inserted,
                'dropped': s.dropped,
                'avg_score': s.avg_score,
                'sample_queries': s.queries[:2]
            }
            for s in recent_stats[-10:]
        ],
        'total_stored': store.stats()['companies'],
        'seen_names': len(seen.names),
        'seen_hosts': len(seen.hosts),
    }
    
    prompt = f'''
你是潜在客户搜索优化器。当前 ICP 已经搜索了 10 轮，效果如下：

{json.dumps(feedback, ensure_ascii=False, indent=2)}

**观察**：
- 最近 5 轮平均入库：{sum(s.inserted for s in recent_stats[-5:]) / 5:.1f} 家/轮
- 已去重公司名：{len(seen.names)} 个
- 已去重域名：{len(seen.hosts)} 个

**任务**：判断是否需要调整 ICP，如果需要，提出具体修改方案。

**输出 JSON**：
```json
{{
  "need_adjust": true/false,
  "reason": "为什么需要/不需要调整",
  "adjustments": {{
    "geography": ["新增的国家"],
    "industries": ["新增的行业"],
    "titles": ["新增的职位"],
    "drop_geography": ["移除的国家（搜索枯竭）"],
    "signals": ["切换信号类型的建议"]
  }},
  "expected_improvement": "预期效果"
}}
```

**规则**：
1. 连续 3 轮入库 < 2 家 → 必须调整
2. 某个地区连续 5 轮命中率 < 10% → 暂时移除该地区
3. 某个信号类型（招聘/展会）效果好 → 建议增加该类型的 topic 数量
4. 不要一次调整太多（最多改 2 个维度）
'''
    
    reply = await client.complete(prompt, max_tokens=800)
    data = parse_json(reply)
    
    if not data.get('need_adjust'):
        return icp, data.get('reason', 'No adjustment needed')
    
    # 应用调整
    new_icp = copy.deepcopy(icp)
    adj = data.get('adjustments', {})
    
    if adj.get('geography'):
        new_icp.geography.extend(adj['geography'])
    if adj.get('drop_geography'):
        new_icp.geography = [g for g in new_icp.geography 
                             if g not in adj['drop_geography']]
    if adj.get('industries'):
        new_icp.industries.extend(adj['industries'])
    if adj.get('titles'):
        new_icp.titles.extend(adj['titles'])
    
    reason = data.get('reason', '') + ' | ' + data.get('expected_improvement', '')
    return new_icp, reason
```

**调用点**（在 `loop` 里）：
```python
# search_skills/cli.py:493-563 (cmd_loop)
round_stats = []
check_interval = 5  # 每 5 轮检查一次

while True:
    stats = await gen.round(args.topics)
    round_stats.append(RoundStats(
        round_id=round_no,
        focus_signal=gen.focus()[0],
        focus_geo=gen.focus()[1],
        queries=stats['queries'],
        candidates=stats['candidates'],
        inserted=stats['inserted'],
        dropped=stats['dropped'],
        avg_score=sum(r['match_score'] for r in stats['records']) / max(1, len(stats['records'])),
        time_cost=time.time() - round_start,
    ))
    
    # 每 N 轮触发一次自适应
    if round_no % check_interval == 0 and round_no > 0:
        new_icp, reason = await analyze_and_adjust_icp(
            gen.icp, round_stats[-check_interval:], store
        )
        if new_icp != gen.icp:
            print(f'🔄 ICP 自适应调整：{reason}')
            gen.icp = new_icp
            # 可选：写回文件
            # Path('data/icp.json').write_text(json.dumps(new_icp.to_dict(), ensure_ascii=False, indent=2))
```

**优点**：
- 模型能理解"搜索枯竭"的信号
- 能根据多轮数据做综合判断
- 调整有理有据（reason 字段）

**缺点**：
- 每 5 轮消耗 1 次 LLM 调用（~800 tokens）
- 模型可能过度调整（比如第 3 轮就说"搜完了"）
- 需要设计合理的触发阈值

---

### 层次 3：完全自主的 Agent 模式（重度思考）

**架构变化**：把 Skills 从"执行工具"变成"Agent 的工具箱"

```python
# 新增：search_skills/agent.py
class AdaptiveLeadAgent:
    '''自主决策的潜客搜索 Agent'''
    
    def __init__(self, client, store, initial_icp):
        self.client = client
        self.store = store
        self.icp = initial_icp
        self.leadgen = LeadGen(client, store, self.icp)
        self.history = []  # 完整决策历史
    
    async def run(self, target_companies: int = 50, max_hours: float = 12):
        '''自主搜索，直到找到目标数量或超时'''
        
        start = time.time()
        found = 0
        
        while found < target_companies and time.time() - start < max_hours * 3600:
            # 1. 让模型决策下一步做什么
            action = await self._decide_next_action()
            
            # 2. 执行决策
            if action['type'] == 'search':
                stats = await self.leadgen.round(action['topics'])
                found += stats['inserted']
                self.history.append({
                    'action': 'search',
                    'topics': action['topics'],
                    'result': stats
                })
            
            elif action['type'] == 'adjust_icp':
                self.icp = action['new_icp']
                self.leadgen.icp = self.icp
                self.history.append({
                    'action': 'adjust_icp',
                    'changes': action['changes'],
                    'reason': action['reason']
                })
            
            elif action['type'] == 'try_new_signal':
                # 尝试用户自定义的查询（超出 5 类信号）
                custom_queries = action['queries']
                # ... 执行自定义搜索
            
            elif action['type'] == 'done':
                print(f'Agent 判断：{action["reason"]}')
                break
    
    async def _decide_next_action(self) -> dict:
        '''让模型分析当前状态，决定下一步'''
        
        recent = self.history[-5:]
        context = {
            'current_icp': self.icp.to_dict(),
            'total_found': self.store.stats()['companies'],
            'recent_actions': recent,
            'seen_stats': {
                'names': len(self.leadgen.seen.names),
                'hosts': len(self.leadgen.seen.hosts),
                'queries': len(self.leadgen.seen.queries),
            }
        }
        
        prompt = f'''
你是自主潜客搜索 Agent。当前状态：

{json.dumps(context, ensure_ascii=False, indent=2)}

**任务**：分析最近的搜索效果，决定下一步做什么。

**可选动作**：
1. `search` - 继续用当前 ICP 搜索（话题数可调）
2. `adjust_icp` - 调整 ICP（扩展/收缩某个维度）
3. `try_new_signal` - 尝试新的搜索策略（不在 5 类信号里的）
4. `done` - 判断已经搜索充分，可以停止

**决策原则**：
- 连续 3 轮新增 < 2 家 → 必须调整 ICP 或尝试新信号
- 某个维度已经搜索 10 轮仍有效 → 继续搜索
- 总数已达标 + 最近质量下降 → 停止

**输出 JSON**：
```json
{{
  "type": "search" | "adjust_icp" | "try_new_signal" | "done",
  "reason": "为什么选择这个动作",
  "topics": 3,  // 如果是 search
  "new_icp": {{}},  // 如果是 adjust_icp
  "changes": ["具体改了什么"],  // 如果是 adjust_icp
  "queries": ["自定义查询"],  // 如果是 try_new_signal
}}
```
'''
        
        reply = await self.client.complete(prompt, max_tokens=1200)
        return parse_json(reply)
```

**使用方式**：
```bash
# 新命令：agent 模式
python -m search_skills agent --target 50 --max-hours 6
```

**优点**：
- 完全自主决策，不需要预设规则
- 能探索新方法（比如"去行业论坛找公司名单"）
- 适合"找到 50 家就行，怎么找都行"的开放需求

**缺点**：
- Token 消耗大（每轮决策 ~1k tokens）
- 可能走弯路（模型判断失误）
- 难以调试（为什么它选择了这个动作？）

---

## 推荐方案：层次 2（轻度思考）

**原因**：
1. **平衡了自动化和可控性**
   - 不是每轮都调用模型（成本可控）
   - 但在关键节点（连续低产出）会自动调整

2. **可解释性好**
   - 模型输出 `reason` 和 `expected_improvement`
   - 用户可以看到"为什么改了 ICP"

3. **渐进式实现**
   - 先在 `loop` 模式里加入自适应逻辑
   - 验证效果后，再考虑是否升级到层次 3

---

## 实现计划（2-3 小时）

### 第 1 步：增加统计字段（30 分钟）

```python
# search_skills/leadgen.py
@dataclass
class RoundStats:
    round_id: int
    timestamp: str
    focus_signal: str
    focus_geo: str
    queries: list[str]
    candidates: int
    resolved: int
    fetched: int
    inserted: int
    updated: int
    dropped: int
    avg_score: float
    time_cost: float

# 在 round() 结束时返回 RoundStats
def round(self, count: int = 4) -> dict:
    # ... 现有逻辑
    stats['round_stats'] = RoundStats(
        round_id=self.seen.round,
        timestamp=datetime.now().isoformat(),
        focus_signal=self.focus()[0],
        focus_geo=self.focus()[1],
        # ...
    )
    return stats
```

### 第 2 步：实现 `analyze_and_adjust_icp()` 函数（1 小时）

参考上面"层次 2"的代码，加入 `search_skills/adaptive.py`。

### 第 3 步：在 `loop` 里集成（30 分钟）

```python
# search_skills/cli.py:cmd_loop
from .adaptive import analyze_and_adjust_icp

round_stats_history = []
check_interval = 5

while True:
    stats = await gen.round(args.topics)
    round_stats_history.append(stats['round_stats'])
    
    if round_no % check_interval == 0 and round_no >= check_interval:
        new_icp, reason = await analyze_and_adjust_icp(
            gen.icp,
            round_stats_history[-check_interval:],
            store,
            client
        )
        if new_icp.to_dict() != gen.icp.to_dict():
            print(f'\n🔄 ICP 自适应调整 (第 {round_no} 轮)')
            print(f'   原因: {reason}')
            print(f'   新 ICP: {json.dumps(new_icp.to_dict(), ensure_ascii=False, indent=2)}')
            gen.icp = new_icp
            gen.pipeline.icp = new_icp
            # 可选：持久化
            adaptive_icp_path = DATA_DIR / f'icp_adaptive_round{round_no}.json'
            adaptive_icp_path.write_text(json.dumps(new_icp.to_dict(), ensure_ascii=False, indent=2))
```

### 第 4 步：测试验证（30 分钟）

```bash
# 用小 budget 测试
python -m search_skills loop --hours 1 --interval 60 --budget 50 --topics 2

# 观察：
# - 是否在第 5/10 轮触发了调整
# - 调整的理由是否合理
# - 调整后命中率是否改善
```

---

## 预期效果

**场景 1：地区枯竭**
```
第 1-5 轮：德国，每轮入库 4-6 家
第 6-10 轮：德国，每轮入库 1-2 家（开始重复）
第 10 轮触发：
  - 分析：德国 Manufacturing 已搜索 200+ 公司，新增率下降 70%
  - 调整：geography += ["France"]
  - 预期：法国 Manufacturing 能找到 30-50 家新公司
第 11-15 轮：德国 + 法国，每轮入库 3-5 家（恢复）
```

**场景 2：行业过窄**
```
第 1-8 轮：Manufacturing，每轮入库 3-4 家
第 9-12 轮：Manufacturing，每轮入库 0-1 家（枯竭）
第 12 轮触发：
  - 分析：Manufacturing 在 3 个国家已搜索充分
  - 调整：industries += ["Industrial Machinery", "Automation"]
  - 预期：相邻行业能找到 40-60 家新公司
第 13-18 轮：3 个行业，每轮入库 4-6 家（持续）
```

**场景 3：信号类型优化**
```
统计前 10 轮：
  - 招聘信号：20 次查询 → 8 家入库（40% 命中）
  - 展会信号：8 次查询 → 12 家入库（150% 命中）
  - 融资信号：12 次查询 → 3 家入库（25% 命中）

第 10 轮触发：
  - 分析：展会信号效果最好，招聘页反爬严重
  - 调整：下 5 轮聚焦展会和名录，减少招聘
  - 实现：修改 SIGNAL_BUCKETS 权重，或直接在 plan_topics 里偏向展会关键词
```

---

## 风险与对策

### 风险 1：过度调整（过早放弃）

**场景**：第 3 轮就判断"德国搜完了"，实际还有 100 家没发现

**对策**：
- 设置最小轮次阈值（至少跑 10 轮才能说"枯竭"）
- 要求"连续 N 轮低产出"而非"单次低产出"
- 调整后保留原维度，只是降低优先级（而非完全删除）

### 风险 2：模型幻觉（瞎调整）

**场景**：模型建议"加上 Aerospace 行业"，但用户卖的是知识库，航空航天不相关

**对策**：
- 白名单机制：只允许从预定义的"相邻行业表"里选
- 人工确认模式：调整前打印建议，用户输入 y/n
- 回滚机制：调整后 3 轮效果更差，自动回滚

### 风险 3：成本失控

**场景**：每 5 轮调用一次 LLM，12 小时跑 100 轮 → 20 次调用 × 1k tokens = 2 万 tokens

**对策**：
- 只在"明显需要调整"时调用（启发式预判：连续 3 轮 < 2 家）
- 使用更便宜的模型做分析（DeepSeek-Chat 而非 GPT-4）
- 设置调整次数上限（最多 5 次/天）

---

## 总结

### 回答你的问题："能不能自己思考并自动修改 ICP？"

**短答案**：可以，通过"层次 2：基于效果分析的 LLM 调整"方案。

**实现路径**：
1. 收集每轮的统计数据（入库数、平均分、耗时）
2. 每 N 轮触发一次分析（让模型看数据，判断是否需要调整）
3. 模型输出调整方案（扩展地区/行业/职位，或切换信号类型）
4. 应用调整，继续搜索

**成本**：每次调整 ~800 tokens，12 小时可能触发 3-5 次，约 ¥0.02

**效果预期**：
- 避免"重复搜索同一批公司"
- 自动发现"某个维度枯竭"并扩展
- 提高长时间运行的效率（从 1 小时找 10 家 → 1 小时找 20 家）

**要不要现在实现？** 我可以花 2-3 小时把"层次 2"加进去，你觉得值得吗？
