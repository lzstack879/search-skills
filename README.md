# search-skills

B2B 潜在客户搜索工具：从 ICP（理想客户画像）出发，找公司、认官网、抽字段、验证邮箱，结果落 SQLite，每天出一份 Excel 报表。

搜索只用 DeepSeek Search，抓取先走本地 crawl4ai 渲染，失败再回退 DeepSeek Fetch。

## 组成

| 模块 | 作用 | 入口 |
| --- | --- | --- |
| [icp-builder](icp-builder/SKILL.md) | 官网或 5 个问题 → `data/icp.json` | `icp` |
| [lead-finder](lead-finder/SKILL.md) | ICP → 名单页 → 公司名 → 官网 → 匹配分 | `leadgen` / `loop` |
| [email-verifier](email-verifier/SKILL.md) | 域名 → MX + SMTP 验证过的邮箱，或 `no_contact` | `emails` |
| 自适应 ICP | `loop` 中产出连续下降时，让模型扩展地区/行业/职位 | `loop`（默认开启） |

总体说明见 [SKILL.md](SKILL.md)。

## 安装

需要 Python 3.10+ 和一个 DeepSeek API key（[platform.deepseek.com](https://platform.deepseek.com)，账户需有余额）。

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# .venv/bin/pip install -r requirements.txt        # macOS / Linux
cp .env.example .env                                # 填入 DEEPSEEK_API_KEY
```

可选：本地 JS 渲染（不装也能跑，只走 DeepSeek Fetch）

```bash
.venv/Scripts/pip install crawl4ai
.venv/Scripts/python -m playwright install chromium
```

## 使用

```bash
# 1. 生成 ICP
python -m search_skills icp --url https://your-company.com   # 从官网反推
python -m search_skills icp --answers answers.json            # 从 5 个答案生成
python -m search_skills icp                                   # 交互式问答

# 2. 找公司
python -m search_skills leadgen --topics 4 --pages 2 --fetch 6
python -m search_skills loop --hours 12 --interval 1200       # 后台持续跑

# 3. 找邮箱
python -m search_skills emails --limit 20

# 4. 查看与报表
python -m search_skills list --limit 20
python -m search_skills report --force                        # reports/YYYY/MM/DD潜在客户搜索报表.xlsx
python -m search_skills usage                                 # 调用量与花费
```

`answers.json` 只认 5 个键：`product` `industries` `titles` `geography` `company_size`。

### loop 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--hours` | 12 | 运行时长，0 = 不限 |
| `--rounds` | 0 | 轮数上限，0 = 不限 |
| `--interval` | 1200 | 每轮间隔（秒） |
| `--budget` | 400 | 每日 search + fetch 调用上限 |
| `--adaptive-check` | 5 | 每 N 轮检查一次是否需要调整 ICP |
| `--no-adaptive` | 关 | 关闭自适应 ICP |

创建 `data/STOP` 文件可让 loop 优雅退出；日志写在 `data/leadgen.log`。

### 自适应 ICP

满足任一条件时，`loop` 会把最近几轮的统计交给模型，由它决定是否扩展 ICP：

- 连续 3 轮新增 < 2 家
- 最近 5 轮平均新增 < 2 家
- 最近 5 轮丢弃率 > 60%

调整只改内存里的 ICP，不覆盖 `data/icp.json`。每次调整另存为 `data/icp_adaptive_rN.json`，附带原因。细节见 [docs/ADAPTIVE_ICP_GUIDE.md](docs/ADAPTIVE_ICP_GUIDE.md)。

## 目录

```text
search_skills/    代码：cli / client / fetcher / pipeline / leadgen / emailver / adaptive / store / report
prompts/          提示词（Markdown，改完下次运行即生效）
icp-builder/ lead-finder/ email-verifier/   子 skill 说明
templates/        ICP 模板
tests/            离线测试（不需要网络和 key）
docs/             设计文档与优化记录
data/             运行时数据：companies.db、icp.json、seen.json、usage.json（不入库）
reports/          生成的 Excel 报表（不入库）
```

## 成本

DeepSeek Search / Fetch 按成功请求计费，5 元 / 1000 次。本地 crawl4ai 抓取不计费。
2026-10-20 的两轮实测：12 次调用入库 3 家，23 次调用入库 5 家，单家约 0.02 元（不含邮箱验证和模型 token）。账本见 `python -m search_skills usage`。

## 测试

```bash
.venv/Scripts/pip install pytest
.venv/Scripts/python -m pytest tests -q
```

## 已知限制

- 聚合招聘站（StepStone、HeyJobs、Indeed）基本都被反爬挡住，名单页主要靠展会、协会、融资新闻类页面。
- 模型生成的查询偶尔仍带 `OR`，提示词已经禁止，但代码层还没有拦截。
- 自适应 ICP 有离线测试覆盖，还没在长时间的真实 `loop` 中验证效果。
