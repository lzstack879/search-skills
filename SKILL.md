---
name: company-search
description: 公司解析与潜在客户搜索。只用 DeepSeek Search API 搜索、DeepSeek Fetch API 抓取，结果落 SQLite，每周自动生成一份 Excel 报表（公司名称/网址/联系人/邮箱/标签/简介/匹配程度）。
---

# company-search

把「找公司 → 认官网 → 读页面 → 抽字段 → 存档 → 出日报」做成一件事，
**搜索只用 DeepSeek Search，抓取只用 DeepSeek Fetch，不接任何其他搜索供应商**。

## 什么时候用它

- 手里只有 ICP（行业 / 职位 / 地区 / 规模），要一份可发信的潜在客户清单
- 拿到一批公司名或域名，要补官网、联系人、邮箱、简介
- 需要每周固定交付一份 Excel 报表给业务方

不适合：需要登录 / 埋点渲染的页面（Fetch 返回的是服务端 Markdown），
以及需要**精确到某一个人邮箱**的场景（本 skill 只抄页面上真实出现的邮箱，不做邮箱猜测）。

## 一分钟上手

```bash
cd search-skills
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# .venv/bin/pip install -r requirements.txt        # macOS / Linux

cp .env.example .env      # 填入 DEEPSEEK_API_KEY

python -m search_skills run --count 8
python -m search_skills report --force
```

## 三条硬约束

1. **搜索只有 DeepSeek Search**（`POST /v1/tools/search`）。没有 Tavily、Brave、Serper、DuckDuckGo 的兜底链。
2. **抓取分两层**：先本地 crawl4ai 渲染（能跑 JS、免费），内容过短或失败再回退 DeepSeek Fetch（`POST /v1/tools/fetch`）。两者都直接产出 Markdown，不自己写 HTML 解析。
3. **不猜邮箱、不编字段**。页面上没有的信息在报表里就是空字符串。

## 为什么能轻

| 常见做法 | 本 skill 的做法 |
| --- | --- |
| 5 个搜索供应商 + 轮换 + 各自限流 | 1 个接口，1 套重试 |
| httpx + BeautifulSoup + Playwright 抓页 | 1 个 Fetch 接口，直接给 Markdown |
| 自己写正文抽取、去广告、去导航 | 引擎已经返回 `summary` 与干净 Markdown |
| 需要维护目录站黑名单 | 引擎给 `authority` / `rerank_score`，只做一层兜底过滤 |

必需依赖：`httpx`、`openpyxl`、`python-dotenv`、`dnspython`；`crawl4ai` 可选。

## 文件结构

```text
search-skills/
├── SKILL.md                  本文件
├── README.md                 安装与调度
├── requirements.txt
├── .env.example
├── prompts/
│   ├── search_query.md       run · ICP → 查询
│   ├── search_judge.md       run · 结果判读 → 官网
│   ├── fetch_extract.md      官网 Markdown → 七个字段
│   ├── search_topics.md      leadgen · ICP → 名单页查询
│   ├── mine_names.md         leadgen · 名单页 → 公司名
│   ├── icp_from_site.md      icp · 官网 → ICP
│   ├── icp_from_answers.md   icp · 5 问 → ICP
│   ├── mine_exhibitors.md    seedpages · 参展商页 → 公司
│   └── gen_seeds.md          seeds · 模型提名候选公司
├── search_skills/
│   ├── client.py             DeepSeek Search / Fetch / Chat
│   ├── fetcher.py            抓取两层：crawl4ai 本地渲染 → DeepSeek Fetch 兜底
│   ├── pipeline.py           编排 + 无模型降级路径
│   ├── leadgen.py            名单挖掘：榜单/招聘页 → 公司名 → 官网 → 入库
│   ├── adaptive.py           自适应 ICP：产出下降时由模型扩展地区/行业/职位
│   ├── emailver.py           邮箱验证：MX → snippet → 官网 → SMTP
│   ├── seedpages.py          参展商/协会名单页 → seeds.csv
│   ├── seeds.py              模型提名候选公司 + MX 校验
│   ├── icp_builder.py        官网/问卷 → data/icp.json
│   ├── store.py              SQLite
│   ├── report.py             日报 xlsx
│   └── cli.py                命令行
├── data/                     运行时生成（不入库）：companies.db / icp.json / seen.json / usage.json
└── reports/                  运行时生成（不入库）：YYYY/MM/DD潜在客户搜索报表.xlsx
```

## 两套提示词

提示词是普通 Markdown，可以直接改；改完即生效（每次运行都会重新读取）。

### 搜索提示词

| 文件 | 作用 | 关键约束 |
| --- | --- | --- |
| `prompts/search_query.md` | ICP → 一组查询 | 每条 ≤128 字符、只允许站点/短语/排除语法、必须带排除项、至少 1 条 `site:linkedin.com/in` |
| `prompts/search_judge.md` | 结果 → 公司官网 | 排除目录站/社交/新闻；宁可判否不猜；公司名去法律后缀 |

### 抓取提示词

| 文件 | 作用 | 关键约束 |
| --- | --- | --- |
| `prompts/fetch_extract.md` | Markdown → 七个字段 | 不编造、邮箱只抄原文、人名带职位、`match_result` 必须含等级与分数 |

七字段与评分口径：

```json
{
  "company_name": "Auxia",
  "company_website": "https://auxia.io",
  "company_perple": "Rich Rao（Head of Growth）",
  "company_email": "rich@auxia.io",
  "industry": "SaaS",
  "summary": "Auxia 为 B2B SaaS 团队提供 AI 驱动的用户留存与增长分析平台。",
  "match_result": "高（85/100）· 行业与职位都符合 ICP",
  "match_score": 85
}
```

`match_score` 权重：行业 40 + 规模 25 + 职位 20 + 信息完整度 15。

## 数据落库

`data/companies.db`，主表 `companies`：

| 列 | 说明 |
| --- | --- |
| `company_name` | 公司名称 |
| `company_website` | 公司网址 |
| `company_perple` | 联系人（姓名 + 职位） |
| `company_email` | 联系邮箱（只来自页面原文） |
| `industry` | 公司标签 |
| `summary` | 公司简介 |
| `match_result` | 匹配程度（高/中/低 + 分数 + 理由） |
| `match_score` | 0~100，便于排序与筛选 |
| `query` / `source_url` | 溯源：哪条查询、哪个页面 |
| `raw_json` | 原始抽取结果，便于回溯 |

去重键 `dedupe_key`：有网址用网址，否则用公司名，再否则用邮箱。重复命中只更新、不新增。

## 日报

```bash
python -m search_skills report            # 今天（当天 00:00 ~ 24:00）
python -m search_skills report --force    # 重建今天
python -m search_skills daily             # 幂等钩子：今天没有就生成，有就跳过
```

命名规则按 `xxxx/xx/xx（年/月/日）`：

```text
reports/2026/10/19潜在客户搜索报表.xlsx
reports/2026/10/20潜在客户搜索报表.xlsx
```

即目录 `年/月`，文件名 `日潜在客户搜索报表.xlsx`（日期就是报表当天）。
`weekly` 保留为兼容别名，行为等同 `daily`。

表头是双语的（`公司名称 company_name`），首行冻结、带筛选、列宽已调好、简介自动换行。

## 成本

按次计费，**5 元 / 1000 次成功请求**，约等于每次 0.005 元。
一次 `run --count 8` 大约 8 次搜索 + 最多 4 次抓取 ≈ 0.06 元。

账本在 `data/usage.json`：

```bash
python -m search_skills usage
```

## 指令速查

| 命令 | 作用 |
| --- | --- |
| `python -m search_skills run --count 8` | 跑一轮搜索 |
| `--industry/--title/--geo` （可重复） | 临时覆盖 ICP |
| `--icp path.json` | 指定 ICP 文件 |
| `--no-llm` | 只用启发式，不调模型（最省） |
| `--per-query / --fetch-limit` | 控制每次的搜索条数与抓取页数 |
| `python -m search_skills list` | 看最近入库的公司 |
| `python -m search_skills usage` | 看调用量与花费 |

## 无模型降级

`--no-llm` 或模型不可用时，走确定性路径：

- 查询：行业 × 职位轮转，套三种查询模板
- 判官：取 `authority` 最高且非目录站的命中
- 抽取：从 Markdown 正则取邮箱、匹配 ICP 行业词、首段做简介、按权重算分

质量低于模型路径，但结果结构完全一致，报表照常出。

## 抓取后端（fetcher.py）

默认两层，顺序固定：

1. **crawl4ai（本地 Playwright）**：能渲染 SPA/JS，免费；反爬站仍会被挡。
2. **DeepSeek Fetch（云端）**：不同 IP 池，0.005/页；504 率约 25%。

判定：本地内容 ≥ 500 字符就直接用，否则抓云端；两边都短就留长的那个。
`--cloud-fetch` 可强制只走云端（排查问题用）。

安装可选依赖：

```bash
.venv/Scripts/pip install crawl4ai
.venv/Scripts/python -m playwright install chromium
```

没装也能跑，自动只走 DeepSeek Fetch。

## 生成 ICP（配套 skill：icp-builder）

`data/icp.json` 不再手写，交给 [icp-builder/SKILL.md](icp-builder/SKILL.md) 生成，两条路径：

```bash
# A. 有官网：抓首页 + /about /pricing /product 等，反推 ICP
python -m search_skills icp --url https://acme.com

# B. 没官网：5 个问题引导填写（或 --answers 文件）
python -m search_skills icp
python -m search_skills icp --answers answers.json

# 查看 / 手填模板
python -m search_skills icp --show
python -m search_skills icp --template

# 官网抓不全时：只问缺的字段 / 拒绝落盘
python -m search_skills icp --url https://acme.com --fill
python -m search_skills icp --url https://acme.com --strict
```

生成的 JSON 与本节 schema 完全一致，并带 `_meta`（method / source / confidence / evidence），
可直接喂给 `run` 与 `leadgen`。

## 搜索层拆成三个 skill

`run`（一条查询找一个官网）命中率太低，已由三个专职 skill 取代：

| skill | 职责 | 入口 |
| --- | --- | --- |
| [lead-finder](lead-finder/SKILL.md) | ICP → 公司 + 官网 + 匹配分（**不找邮箱**） | `leadgen` / `loop` |
| [email-verifier](email-verifier/SKILL.md) | 域名 → 可信邮箱 或 `no_contact` | `emails` |
| [contact-enricher](contact-enricher/SKILL.md) | 通用邮箱 → 有名字的决策人 + 直达邮箱 | `contacts` |

```bash
python -m search_skills leadgen --topics 4     # A：灌新公司
python -m search_skills loop --hours 12        # A：后台持续跑
python -m search_skills emails --limit 20      # B：给积压线索找邮箱
python -m search_skills contacts --export-tasks data/contact_tasks.json --limit 10  # C1：导出待补全清单
# （把 tasks 交给 /contacts 或其他 agent 研究，填好 results.json）
python -m search_skills contacts --import-results data/contact_results.json         # C2：导回结果
```

A 写入 `domain` + `email_status='pending'`，B 消费 pending 并写回
`email_status / email_source / email_kind / email_attempts`。
C 消费 `company_email` 不为空但 `company_perple` 为空或只是角色（info@、contact@）的线索，
写回 `company_perple / contact_status / contact_source / contact_evidence`，
并在 agent 找到直达邮箱时一并升级 `company_email`。

状态机：
- **email_status**: `pending → found | no_contact | domain_dead | rejected | needs_agent`；
  `no_contact` 7 天后自动重试，最多 3 次
- **contact_status**: `(空) → needs_contact → found`

`loop` 支持 `--budget`（每日 search+fetch 调用上限）、`data/STOP` 文件（优雅停止）、
日志写 `data/leadgen.log`，已尝试过的公司名记在 `data/seen.json`。
