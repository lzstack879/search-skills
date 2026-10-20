# 自适应 ICP 使用指南

## 功能概述

自适应 ICP（层次 2 实现）能让潜客搜索系统在运行过程中**自动调整搜索策略**，避免重复搜索同一批公司。

### 核心能力

1. **自动检测搜索枯竭** - 连续 3 轮新增 < 2 家 → 触发调整
2. **智能扩展维度** - 地区/行业/职位自动扩展到相邻选项
3. **保留调整历史** - 每次调整生成 `data/icp_adaptive_rN.json`
4. **可追溯决策** - 输出调整原因和预期效果

---

## 快速开始

### 1. 启用自适应模式（默认开启）

```bash
# 标准用法：每 5 轮检查一次
python -m search_skills loop --hours 12 --topics 4

# 自定义检查频率：每 3 轮检查
python -m search_skills loop --hours 12 --topics 4 --adaptive-check 3

# 禁用自适应（使用固定 ICP）
python -m search_skills loop --hours 12 --topics 4 --no-adaptive
```

### 2. 观察自适应调整

运行时输出示例：

```text
======================================================================
🔄 ICP 自适应调整 (第 10 轮)
======================================================================

【调整原因】
德国连续 5 轮新增 < 2 家，搜索接近枯竭 | 地区+Austria, 行业+Industrial Machinery | 预期: 奥地利和瑞士预计有 30-50 家制造企业

【变更内容】
  地区: Germany
     → Germany, Austria, Switzerland
  行业: Manufacturing
     → Manufacturing, Industrial Machinery

======================================================================
```

### 3. 查看调整历史

```bash
# 查看所有自适应 ICP 文件
ls data/icp_adaptive_r*.json

# 查看某次调整的详情
cat data/icp_adaptive_r10.json
```

输出示例：

```json
{
  "industries": ["Manufacturing", "Industrial Machinery"],
  "titles": ["Head of Service", "After-Sales Manager", "CTO"],
  "geography": ["Germany", "Austria", "Switzerland"],
  "company_size": "100-800 employees",
  "product": "企业知识库远程部署与技术支持",
  "_adaptive": {
    "round": 10,
    "reason": "德国连续 5 轮新增 < 2 家 | 地区+Austria, 地区+Switzerland, 行业+Industrial Machinery | 预期: 可再找 30-50 家",
    "timestamp": "2026-10-20T15:30:45"
  }
}
```

---

## 触发条件

自适应调整会在以下情况**自动触发**（无需人工干预）：

### 规则 1：连续低产出

```python
# 连续 3 轮新增 < 2 家
if all(round.inserted < 2 for round in last_3_rounds):
    trigger_adjustment()
```

**示例场景**：
- 第 8 轮：插入 1 家
- 第 9 轮：插入 1 家
- 第 10 轮：插入 0 家
- → 触发调整

### 规则 2：平均产出下降

```python
# 最近 5 轮平均入库 < 2 家
if avg(last_5_rounds.inserted) < 2.0:
    trigger_adjustment()
```

**示例场景**：
- 第 6-10 轮平均：(3 + 2 + 2 + 1 + 1) / 5 = 1.8 家/轮
- → 触发调整

### 规则 3：高丢弃率

```python
# 最近 5 轮丢弃率 > 60%
if dropped / fetched > 0.6:
    trigger_adjustment()
```

**示例场景**：
- 最近 5 轮：抓取 30 家，丢弃 19 家 = 63% > 60%
- → 触发调整

---

## 调整策略

模型会根据数据分析，提出以下调整方案：

### 策略 1：地区扩展

**触发条件**：某个国家连续 5 轮命中率低

**扩展规则**：
- 德国 → 奥地利、瑞士（德语区）
- 法国 → 比利时、瑞士（法语区）
- 英国 → 爱尔兰
- 荷兰 → 比利时

**示例**：
```
原 ICP: ["Germany"]
调整后: ["Germany", "Austria", "Switzerland"]
原因: 德国搜索枯竭，扩展到德语区邻国
```

### 策略 2：行业扩展

**触发条件**：当前行业搜索充分（已找到 50+ 公司）

**扩展规则**：
- Manufacturing → Industrial Machinery, Automation
- Healthcare → Medical Devices, Pharmaceuticals
- Legal Services → Consulting, Professional Services
- SaaS → Software Development, IT Services

**示例**：
```
原 ICP: ["Manufacturing"]
调整后: ["Manufacturing", "Industrial Machinery"]
原因: 制造业已搜索充分，扩展到工业机械
```

### 策略 3：职位泛化

**触发条件**：高级职位找不到

**扩展规则**：
- Head of XX → XX Manager
- CTO → IT Director
- VP Sales → Sales Manager

**示例**：
```
原 ICP: ["Head of Service", "CTO"]
调整后: ["Head of Service", "CTO", "Service Manager", "IT Director"]
原因: 高级职位搜索困难，降级到中级职位
```

### 策略 4：地区收缩

**触发条件**：某个国家连续 8 轮都是重复/丢弃

**操作**：暂时移除该国家，给其他国家更多机会

**示例**：
```
原 ICP: ["Germany", "France", "Netherlands"]
调整后: ["France", "Netherlands"]
原因: 德国连续 8 轮无新增，暂时移除
```

---

## 成本与性能

### Token 消耗

| 操作 | 频率 | Token/次 | 成本/次 |
|------|------|----------|---------|
| 检查触发条件 | 每轮 | 0 | ¥0（纯启发式） |
| LLM 分析 | 每 5 轮触发 1 次 | ~800 | ¥0.0008 |
| 12 小时运行 | 约 2-3 次调整 | ~2400 | ¥0.0024 |

**结论**：自适应功能的成本可以忽略不计（< 0.5% 总成本）。

### 效率提升

还没有实测数据。验证方法：同一份 ICP 分别用 `--no-adaptive` 和默认模式各跑 12 小时，对比新增公司数和 `seen.json` 里的重复查询数。

---

## 高级用法

### 1. 强制触发调整

```python
# 在代码中手动触发
from search_skills.adaptive import analyze_and_adjust_icp

new_icp, reason, adjusted = await analyze_and_adjust_icp(
    current_icp,
    recent_stats,
    store,
    client,
    seen_counts,
    force=True,  # 强制分析，跳过启发式检查
)
```

### 2. 调整检查频率

```bash
# 每 3 轮检查（更激进）
python -m search_skills loop --adaptive-check 3

# 每 10 轮检查（更保守）
python -m search_skills loop --adaptive-check 10
```

**建议**：
- 小预算（< 100 调用/天）：`--adaptive-check 3`（快速反应）
- 大预算（> 300 调用/天）：`--adaptive-check 8`（充分验证）

### 3. 回滚到历史 ICP

```bash
# 如果某次调整效果不好，手动回滚
cp data/icp_adaptive_r10.json data/icp.json

# 重新运行（会使用回滚后的 ICP）
python -m search_skills loop --hours 6
```

---

## 监控与调试

### 1. 查看日志

```bash
# 查看完整日志（包含调整决策）
tail -f data/leadgen.log

# 只看调整事件
grep "ICP 自适应调整" data/leadgen.log
```

### 2. 分析调整效果

```python
# 对比调整前后 5 轮的产出
import json
from pathlib import Path

for f in sorted(Path('data').glob('icp_adaptive_r*.json')):
    data = json.loads(f.read_text())
    round_no = data['_adaptive']['round']
    print(f"第 {round_no} 轮调整:")
    print(f"  地区: {', '.join(data['geography'])}")
    print(f"  行业: {', '.join(data['industries'])}")
    print(f"  原因: {data['_adaptive']['reason']}")
    print()
```

### 3. 禁用某类调整

如果你不想要某类调整（比如不想扩展行业），可以修改提示词：

编辑 `search_skills/adaptive.py` 第 140-165 行：

```python
**调整原则**：
1. **地区扩展**：某个国家连续 5 轮命中率低 → 添加相邻国家
2. **行业扩展**：❌ 禁用（用户只想搜索当前行业）
3. **职位泛化**：高级职位找不到 → 降级到中级职位
4. **地区收缩**：某个国家连续 8 轮都是重复/丢弃 → 暂时移除
```

---

## 常见问题

### Q1: 调整太频繁，能否降低敏感度？

**A**: 增加检查间隔，或修改触发阈值：

```bash
# 方法 1: 每 10 轮才检查
python -m search_skills loop --adaptive-check 10

# 方法 2: 修改代码中的阈值
# adaptive.py:56-58
if all(s.inserted < 2 for s in last_3):  # 改为 < 1
```

### Q2: 调整后效果反而变差了，怎么办？

**A**: 两个方案：

1. **手动回滚**：
   ```bash
   cp data/icp_adaptive_r10.json data/icp.json  # 回到调整前
   python -m search_skills loop --hours 6 --no-adaptive  # 禁用自适应
   ```

2. **等待下一次调整**：系统会在 5 轮后再次分析，可能会调整回来

### Q3: 能否只扩展地区，不扩展行业？

**A**: 修改提示词（见"监控与调试" 第 3 点），或在调整后手动编辑：

```bash
# 调整发生后，手动删除不想要的行业
vi data/icp.json  # 删除新增的行业
```

### Q4: 自适应会覆盖我手动设置的 ICP 吗？

**A**: 不会。自适应只在 `loop` 模式运行时生效，且只修改内存中的 ICP。原始 `data/icp.json` 不变，调整结果另存为 `icp_adaptive_rN.json`。

---

## 最佳实践

### 场景 1：初次运行（探索阶段）

```bash
# 1. 用较小的 ICP 开始
cat > data/icp.json << EOF
{
  "industries": ["Manufacturing"],
  "titles": ["Head of Service", "CTO"],
  "geography": ["Germany"],
  "company_size": "100-500 employees",
  "product": "企业知识库远程部署"
}
EOF

# 2. 运行 6 小时，启用自适应（每 3 轮检查）
python -m search_skills loop --hours 6 --topics 4 --adaptive-check 3

# 3. 查看扩展了哪些维度
ls data/icp_adaptive_r*.json

# 4. 如果效果好，用最终 ICP 做基准
cp data/icp_adaptive_r15.json data/icp_baseline.json
```

### 场景 2：长期运行（生产阶段）

```bash
# 1. 用验证过的 ICP 开始
cp data/icp_baseline.json data/icp.json

# 2. 运行 24 小时，保守检查（每 8 轮）
python -m search_skills loop --hours 24 --topics 4 --adaptive-check 8

# 3. 定期查看调整频率
grep "ICP 自适应调整" data/leadgen.log | wc -l

# 4. 如果 24h 内调整 > 5 次，说明 ICP 太窄，手动扩宽
```

### 场景 3：多市场测试

```bash
# 每个市场跑 12 小时，对比哪个市场更高产
for geo in Germany France Netherlands; do
  cat > data/icp_${geo}.json << EOF
{
  "geography": ["${geo}"],
  "industries": ["Manufacturing"],
  ...
}
EOF
  python -m search_skills loop --hours 12 --icp data/icp_${geo}.json \
    --adaptive-check 5
done

# 对比结果
sqlite3 data/companies.db "SELECT COUNT(*), industry FROM companies GROUP BY industry"
```

---

## 总结

**自适应 ICP 解决的核心问题**：

1. ✅ **避免重复搜索** - 德国搜完自动切法国
2. ✅ **提高长时间运行效率** - 预期效果，待实测
3. ✅ **无需人工监控** - 自动检测枯竭并调整
4. ✅ **成本可控** - 每次调整 < ¥0.001

**推荐使用场景**：

- ✅ 长时间运行（> 6 小时）
- ✅ 目标数量大（> 50 家）
- ✅ ICP 初期较窄（单一国家/行业）
- ❌ 短时间运行（< 2 小时）：不够时间触发调整
- ❌ ICP 已经很宽（5 个国家 × 5 个行业）：调整空间小

**下一步**：运行 `python -m search_skills loop --hours 12` 实际测试效果！
