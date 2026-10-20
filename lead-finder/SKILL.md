---
name: lead-finder
description: 按 ICP 找潜在客户公司：话题查询 → 名单页 → 抽公司名 → 解析官网 → 入库。只负责找公司，不负责找邮箱。
---

# lead-finder（找线索）

一句话：**ICP → 公司 + 官网 + 匹配分**，邮箱交给 email-verifier。

## 什么时候用

- 已经有 `data/icp.json`，要持续往库里灌新公司
- 复盘“为什么这批线索不对”：先把公司找对，再谈邮箱

## 命令

```bash
python -m search_skills leadgen --topics 4          # 跑一轮
python -m search_skills loop --hours 12 --interval 1200   # 后台持续跑
```

## 流程

```text
ICP
 ├─ search_topics       3 条「找名单页」查询（榜单/名录/融资/招聘）
 ├─ search × 3          每条约 8 个结果
 ├─ _minable()          按标题里的 top/best/hiring/list 打分，挑每主题前 2 页
 ├─ fetcher             本地 crawl4ai 渲染 → 太短才回退 DeepSeek Fetch
 ├─ mine_names          每页 10~15 个公司名
 └─ 逐个候选：
      search '"名字" official website' → 域名（host_score ≥ 0.6 才认）
      fetcher 抓 https://域名
      fetch_extract → 7 字段
      硬门槛：score < 30 → 丢；行业不在 ICP 且 score < 40 → 丢
      store.upsert（domain 非空，email_status = pending）
```

## 硬门槛（2026-10 加）

| 条件 | 处理 |
| --- | --- |
| `match_score < 30` | 丢弃 |
| `industry` 不在 ICP 且 `score < 40` | 丢弃 |
| 域名已被收录 | 跳过（`web:https://xxx` 去重） |

实测：12 条里砍掉 4 条废数据（生物医药、消费电子、错误页、大学站点）。

## 成本

一轮 4 个 topic ≈ 0.04~0.05 元，产出 3~4 家；抓取走本地渲染，抓取成本 0。

## 不做什么

- ❌ 不找邮箱（那是 email-verifier 的事）
- ❌ 不猜域名（必须靠 search 结果匹配）
- ❌ 不抓登录墙/领英（fetch 拿不到，site:linkedin.com 实测 0 命中）
