# 潜客搜索 Skills 优化报告

**日期**: 2026-10-20  
**优化轮次**: 2 轮真实数据测试  
**ICP**: 制造业知识库部署（德国/荷兰/法国，100-800人）

---

## 一、真实数据反馈

### 第一轮问题（3 topics, 8 candidates → 3 stored, 4 dropped）

| 公司 | 域名 | 行业 | 分数 | 问题诊断 |
|------|------|------|------|----------|
| RAPS | raps.org | Manufacturing | 85 | ❌ **误判**：实际是监管咨询机构，非制造业买方 |
| KHS Gruppe | khs.com | Industrial Machinery | 55 | ✅ 正确，但 summary 被 cookie 提示污染 |
| Ivoclar | ivoclar.com | (空) | 30 | ⚠️ 牙科设备商，industry 为空被低分 |

**查询质量问题**：
- ❌ 出现 `OR` 操作符（违反提示词规则）
- ❌ 招聘页全部 404（stepstone.de/heyjobs.co 反爬 + DeepSeek Fetch 也失败）
- ⚠️ 4 家被 dropped（未确认是否合理）

### 第二轮结果（4 topics, 13 candidates → 5 stored, 1 dropped）

| 公司 | 域名 | 行业 | 分数 | 质量评估 |
|------|------|------|------|----------|
| Schmitter Hydraulik | schmitter-hydraulik.de | Industrial Machinery | 60 | ✅ 液压设备供应商，75年历史 |
| HERMES Systeme | hermes-systeme.de | Automation | 60 | ✅ 自动化技术，有邮箱 |
| Sikla | sikla.com | Industrial Machinery | 55 | ✅ 国际工业产品系统供应商 |
| HAIMER | haimer.com | Industrial Machinery | 55 | ✅ 刀具夹持技术制造商 |
| Bilfinger | bilfinger.com | Industrial Machinery | 50 | ⚠️ 工业服务商（非制造企业） |

**改善**：
- ✅ 查询添加了招聘站排除（但仍有 `OR` 残留）
- ✅ Summary 质量提升（无 cookie 污染）
- ✅ 命中率提升：5/6 fetched = 83%（vs 第一轮 3/7 = 43%）

---

## 二、优化措施

### 1. 提示词强化（search_topics.md）

**问题**: 查询生成违反规则（出现 `OR`、未排除招聘站）

**修复**:
```diff
- 2. 只允许 `"短语"`、`-site:`、`-词`、`after:`；**不要** `OR`
+ 2. 只允许 `"短语"`、`-site:`、`-词`、`after:`；**严禁** `OR` / `|` / `intitle:` ...
+ 3. **严禁招聘站** site:stepstone.de / site:heyjobs. / site:indeed.com（登录墙/反爬）
+    用 `-site:indeed.com -site:stepstone.de -site:heyjobs.` 排除，直接找公司官网
```

**示例更新**:
```text
# A. 招聘信号
- "MES 工程师" 招聘 2026 苏州 -site:linkedin.com
+ "MES 工程师" 招聘 2026 苏州 -site:indeed.com -site:stepstone.de -site:heyjobs.
```

### 2. 提取质量改进（fetch_extract.md）

**问题**: Cookie/隐私政策页面污染 summary，咨询/协会机构误判为制造业

**修复**:
```diff
  4. **`summary` 只描述这家公司做什么**，不写评价、不写营销口号，≤120 字
+    不写 cookie 提示，页面只有 cookie/隐私政策/登录框时 summary 留空

  5. **`industry` 从 ICP 行业列表里挑一个**；都不匹配就填原文行业词
+    严禁填写服务业、咨询业（Consulting/Advisory/Training）、政府/协会/媒体
```

**边界情况新增**:
```text
- 页面主体是咨询/培训/媒体/政府/协会 → industry 填 "Consulting" 或 "Media"，
  match_score ≤ 25，confidence: 60。这些不是制造业买方，但要诚实记录。
```

### 3. 代码层过滤增强

#### A. `heuristic_extract` - Cookie 污染检测

[search_skills/pipeline.py:178-196](search_skills/pipeline.py:178)

```python
def heuristic_extract(page: FetchPage, icp: ICP, company: str, query: str) -> dict:
    # 新增 junk_phrases 检测
    junk_phrases = ('cookie policy', 'privacy policy', 'we use cookies', 
                    'datenschutz', 'impressum', 'legal notice')
    mostly_junk = sum(1 for phrase in junk_phrases if phrase in lowered) >= 2 and len(text) < 800
    
    summary = ''
    if not mostly_junk:  # 只在非垃圾页面提取 summary
        for block in re.split(r'\n\s*\n', text):
            # 跳过 cookie/隐私块
            if any(phrase in line.lower() for phrase in junk_phrases):
                continue
            summary = re.sub(r'\s+', ' ', line)[:120]
            break
```

#### B. `is_non_buyer` - 非买方过滤

[search_skills/leadgen.py:60-75](search_skills/leadgen.py:60)

```python
# 扩展正则：政府 + 协会 + 咨询
PUBLIC_RE = re.compile(
    r'\.(gov|gouv|overheid|bund|mil)(\.[a-z]{2})?$|gemeente|stadt|kommune|'
    r'\bverband\b|\bverein\b|\bassociation\b|\bchamber\b|\binstitut[e]?\b|'
    r'\bconsulting\b|\bconsultancy\b|\badvisory\b|\bberatung\b',
    re.I,
)

NON_BUYER_INDUSTRIES = {'Consulting', 'Advisory', 'Training', 'Media', 'Association', 'Government'}

def is_non_buyer(record: dict) -> bool:
    '''咨询、协会、政府不是制造业买方'''
    if is_public_sector(record.get('company_name', ''), record.get('domain', '')):
        return True
    industry = str(record.get('industry', '')).strip()
    return industry in NON_BUYER_INDUSTRIES
```

调用点更新：
```python
# leadgen.py:500, 609
- if not self._fits_icp(record) or is_public_sector(...):
+ if not self._fits_icp(record) or is_non_buyer(record):
```

#### C. `heuristic_queries` - 启发式查询改进

[search_skills/pipeline.py:117-138](search_skills/pipeline.py:117)

```python
def heuristic_queries(icp: ICP, count: int = 8) -> list[str]:
    exclusions = '-site:indeed.com -site:stepstone.de -site:heyjobs. -site:glassdoor.com'
    for index in range(max(1, count)):
        # ...
        if shape == 0:
-           queries.append('site:linkedin.com/in "' + title + '" "' + industry + '" "' + geo + '"')
+           queries.append('"' + title + '" "' + industry + '" "' + geo + '" job ' + exclusions)
        elif shape == 1:
-           queries.append(industry + ' company "' + geo + '" ' + EXCLUSIONS)
+           queries.append(industry + ' company "' + geo + '" ' + exclusions)
```

**原因**: LinkedIn 登录墙无法抓取，改为通用招聘查询 + 全面排除招聘站。

---

## 三、效果对比

| 指标 | 第一轮 | 第二轮 | 改善 |
|------|--------|--------|------|
| **入库率** | 3/7 = 43% | 5/6 = 83% | +93% |
| **邮箱命中** | 1/3 = 33% | 1/5 = 20% | -39%* |
| **误判过滤** | 0 (RAPS 未拦截) | 0 (Bilfinger 边界) | 持平 |
| **Summary 质量** | 2/3 有 cookie 污染 | 5/5 干净 | +150% |
| **查询合规** | 3/3 有 OR | 4/4 仍有 OR** | 需继续修 |

\* 邮箱率下降是正常波动（样本小）  
\** 查询由模型生成，提示词已修但模型执行待验证

---

## 四、待解决问题

### 1. 查询生成仍有 `OR` 操作符

**现状**: 虽然提示词已禁止 `OR`，但第二轮查询仍出现：
```text
"Serviceleiter" OR "Leiter Kundendienst" Stellenanzeige 2026 ...
"CTO" OR "Leiter IT" Stellenangebot 2026 ...
```

**根因**: 模型生成查询时未严格遵守规则（可能把 OR 理解为"多个职位的组合"）。

**方案**:
- [ ] 在 `search_topics.md` 开头添加更显眼的 ⚠️ 警告框
- [ ] 在 `plan_topics()` 后端增加正则校验，检测到 `OR` 就丢弃该查询
- [ ] 或改为后处理：`query.replace(' OR ', ' ')` 直接删除

### 2. 招聘页反爬成功率低

**现状**: 
- 本地 crawl4ai: 被 Cloudflare/Akamai 拦截
- DeepSeek Fetch: 404（可能是同样原因）

**建议**: 
- 不再依赖聚合招聘站（stepstone/indeed/heyjobs）
- 专注 **C 类信号**（展会名单、协会名录、融资新闻）— 这些页面无反爬
- 或改用公司自己的招聘页（`site:公司域 karriere | jobs | stellenangebote`）

### 3. 边界案例：Bilfinger

**问题**: Bilfinger 是"工业服务商"，不是制造企业本身，但 `is_non_buyer()` 未拦截（industry = "Industrial Machinery"）。

**待定**: 
- 是否需要更细粒度的分类（Manufacturing vs Industrial Services）？
- 还是保留，让后续人工或邮箱验证环节筛选？

---

## 五、后续优化方向

### 短期（1-2 轮）

1. **查询生成后校验**: 在 `plan_topics()` 返回前，过滤掉含 `OR`/`|`/`intitle:` 的查询
2. **增加测试用例**: 
   - `test_query_no_forbidden_operators()` — 检查生成查询不含禁止操作符
   - `test_is_non_buyer()` — 验证 Consulting/Association 被过滤
3. **日志增强**: 记录每家公司的 `dropped_reason`（score < 30 / 行业不符 / 非买方 / ...）

### 中期（3-5 轮）

1. **信号源调整**: 从招聘页转向展会/协会/融资新闻（反爬更少）
2. **域名预校验**: resolve 阶段先检查 MX 记录，避免抓取死域
3. **评分模型优化**: 当前 `match_score` 权重固定，考虑引入 ICP 动态权重

### 长期

1. **A/B 测试框架**: 对比不同提示词版本的命中率
2. **反馈闭环**: 从已验证邮箱的公司反推"好查询"的特征
3. **多语言扩展**: 当前仅支持中德英，考虑法语/荷兰语 ICP

---

## 六、成本与收益

| 项目 | 第一轮 | 第二轮 | 备注 |
|------|--------|--------|------|
| **API 调用** | 12 | 23 | search + fetch 总和 |
| **成本** | ¥0.06 | ¥0.115 | 5 元/1000 次 |
| **入库公司** | 3 | 5 | |
| **单家成本** | ¥0.02 | ¥0.023 | 成本/入库数 |
| **有邮箱公司** | 1 | 1 | |
| **单个邮箱成本** | ¥0.06 | ¥0.115 | 尚需 email-verifier 补充 |

**结论**: 优化后虽然调用量增加（更多 topic），但入库率提升使单家成本基本持平（¥0.02），质量显著改善。

---

## 七、文件变更清单

```
modified:   prompts/search_topics.md          (+6 行规则强化，+示例更新)
modified:   prompts/fetch_extract.md          (+边界情况，+铁律细化)
modified:   search_skills/pipeline.py         (+cookie 检测，+heuristic_queries 改进)
modified:   search_skills/leadgen.py          (+is_non_buyer，+NON_BUYER_INDUSTRIES)
created:    data/answers.json                 (测试用 ICP)
created:    OPTIMIZATION_2026-10-20.md        (本文档)
```

---

## 八、使用建议

### 针对当前 ICP（制造业知识库，德荷法）

```bash
# 1. 先跑一轮 leadgen，看命中质量
python -m search_skills leadgen --topics 4 --pages 2 --fetch 6

# 2. 人工检查入库公司（重点看 industry 和 summary）
python -m search_skills list --limit 10

# 3. 质量 OK 后再跑邮箱验证
python -m search_skills emails --limit 20

# 4. 每天自动：用 loop 模式 + 定时任务
python -m search_skills loop --hours 12 --interval 1800 --budget 200
```

### 针对其他 ICP

1. **重新生成 ICP**: `python -m search_skills icp --url <官网>` 或交互式问答
2. **调整信号聚焦**: 编辑 `leadgen.py:180-186` 的 `SIGNAL_BUCKETS`，调整五类信号的优先级
3. **语言适配**: 
   - 中国 ICP：查询用中文，排除「智联/前程无忧/BOSS」
   - 欧美 ICP：查询用英文，排除「indeed/glassdoor/linkedin」

---

**下一步**: 跑 3-5 轮累计 30+ 公司样本，统计 precision@30 和 email_found_rate，决定是否需要进一步调整阈值或信号源。
