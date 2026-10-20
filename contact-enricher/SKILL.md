---
name: contact-enricher
description: 把通用邮箱（contact@、info@）补成有名字的决策人（张三（销售副总）+ 直达邮箱）。半自动：脚本筛出、agent 研究、结果带证据回填。
---

# contact-enricher

`emails` 找到的邮箱 70% 是 contact@、info@、sales@ 这类通用邮箱，发信容易石沉大海。
本 skill 把通用邮箱升级成**有名字的决策人 + 直达邮箱（可选）**。

## 为什么半自动

要把 `contact@acme.com` 补成「张三（销售副总）+ zhangsan@acme.com」，需要：

- 读团队页、Impressum、新闻稿、LinkedIn，判断职位是否决策层
- 判断名字是否占位符（John Doe、Test User、某某某）
- 从多个候选人里挑优先级最高的（优先销售、增长、产品、运营）
- 证据链：哪个 URL 写着这个人的名字、职位、邮箱

这些判断在流水线里跑会拖慢速度，而且成功率只有 30%～50%（很多公司根本不公开决策人）。
所以设计成**并行外包**：脚本挑出高价值的（匹配分 ≥ 60、有通用邮箱、无具名联系人），
导出给 agent 深挖，agent 的结果带证据链回填。

## 流程

```bash
# 1. 导出待补全清单（默认 10 家，可以 --limit 50）
python -m search_skills contacts --export-tasks data/contact_tasks.json --limit 10

# 2. 把 tasks.json 交给 /contacts 或其他 agent 研究
#    agent 填好 results.json（格式见下）

# 3. 导回结果
python -m search_skills contacts --import-results data/contact_results.json
```

### tasks.json 格式

```json
{
  "generated_at": "2026-10-20T14:30:00",
  "count": 10,
  "tasks": [
    {
      "dedupe_key": "acme.com",
      "company_name": "Acme Corp",
      "domain": "acme.com",
      "website": "https://acme.com",
      "current_email": "contact@acme.com",
      "match_score": 75,
      "industry": "SaaS",
      "try_urls": [
        "https://acme.com/about",
        "https://acme.com/team",
        "https://acme.com/leadership",
        "https://acme.com/impressum",
        "https://acme.com/imprint",
        "https://acme.com/contact"
      ],
      "search_hints": [
        "site:acme.com (CEO OR founder OR \"head of\" OR director)",
        "\"Acme Corp\" CEO founder press release"
      ],
      "ask": "Find ONE decision-maker (C-level, VP, Head of X, or Founder) with their full name, title, and optionally a direct email. Quote the exact URL where you found them. Prioritize sales, growth, product, or operations roles."
    }
  ],
  "format": {
    "dedupe_key": "echo it back",
    "name": "full name (e.g., Jane Doe)",
    "title": "job title (e.g., VP Sales)",
    "email": "direct email if found, else empty string",
    "evidence_url": "page where the contact is visible (team page, press release, etc.)"
  }
}
```

### results.json 格式

Agent 填好的结果，每家一条：

```json
{
  "results": [
    {
      "dedupe_key": "acme.com",
      "name": "Jane Doe",
      "title": "VP Sales",
      "email": "jane@acme.com",
      "evidence_url": "https://acme.com/team"
    },
    {
      "dedupe_key": "firma.de",
      "name": "Hans Müller",
      "title": "Geschäftsführer",
      "email": "",
      "evidence_url": "https://firma.de/impressum"
    }
  ]
}
```

`email` 可以留空（agent 找到了人但没有直达邮箱），这种情况仍会回填 `company_perple`。

## 验证规则

脚本只接受符合以下条件的结果：

- `name` 和 `title` 非空，长度 ≥ 2 字符
- `evidence_url` 是 https/http，且在公司域名上或已知新闻站（prnewswire、businesswire、linkedin、crunchbase）
- `email` 如果非空，必须在公司域名上，且不能是 contact@、info@ 等通用邮箱

不符合规则的会被拒绝（`rejected`），拒绝原因记在导入报告里。

## 回填字段

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `company_perple` | `name + " (" + title + ")"` | 「Jane Doe (VP Sales)」 |
| `contact_status` | `'found'` | 区分补全过 vs 未补全 |
| `contact_source` | `'agent'` | 区分人工找到 vs 脚本抄的 |
| `contact_evidence` | `evidence_url` | 溯源：哪个页面证明这个人的存在 |
| `company_email` | `email`（如果非空） | 直达邮箱升级通用邮箱 |
| `email_source` | `'agent_direct'`（如果有直达邮箱） | 区分直达 vs 通用 |
| `email_kind` | `'personal'`（如果有直达邮箱） | 个人邮箱评级高于通用邮箱 |

## 成本与优先级

- 每导出一次清单，脚本把 `contact_status` 标记为 `needs_contact`，下次不再重复导出
- 默认只导出 `match_score ≥ 60` 的线索（可以 `--min-score` 调整）
- 一次导出 10～50 家，agent 每家研究 2～3 个页面，大约 20～150 次抓取
- 如果 agent 找到的邮箱经过 SMTP 验证，会自动标记为 A 级（优于脚本抄的通用邮箱）

## 使用建议

- 先跑 `leadgen` 和 `emails`，积累一批线索后再一次性 `contacts --export-tasks`，批量交给 agent
- Agent 研究时优先搜索**新闻稿**（prnewswire、businesswire、公司博客），比团队页更容易找到决策人
- 如果公司没有公开的团队页或新闻稿，可以搜索 LinkedIn（`site:linkedin.com "公司名" "VP Sales"`）
- 导回结果时，被拒绝的（missing evidence、wrong domain、generic email）可以手工补证据后再导一次

## 示例

```bash
# 筛出 20 家待补全的线索
python -m search_skills contacts --export-tasks data/contact_tasks.json --limit 20 --min-score 70

# （把 tasks.json 交给 /contacts agent，它会自动搜索、抓取、判断，填好 results.json）

# 导回结果
python -m search_skills contacts --import-results data/contact_results.json
# accepted 15 | rejected 5
# errors:
#   firma-a.de: missing evidence_url
#   firma-b.de: email is still generic, not a person
#   + Firma C             Hans Schmidt (CEO)
#   + Firma D             Sarah Lee (Head of Sales)
#   ...

# 查看回填结果
python -m search_skills list --limit 5
```

导入后，报表的 `company_perple` 列就有了真名 + 职位，而且如果 agent 找到了直达邮箱，`company_email` 也会升级。
