---
name: icp-builder
description: 把一家公司变成 data/icp.json。有官网时用 DeepSeek Fetch 抓首页+关键页，模型反推 ICP；没官网时用 5 个问题引导填写并归一化。输出与 company-search 读取的 schema 完全一致。
---

# icp-builder

一句话：**你卖什么 → 谁最该买 → icp.json**。

## 什么时候用

- 手里只有产品和官网，没有一份能直接喂给搜索的客户画像
- 换产品线、换市场（美国 → 欧洲）要重出一份 ICP
- 团队对“我们的客户是谁”理解不一致，需要一份带证据、可复核的 JSON

## 两条路径

### 路径 A：有官网（推荐）

```bash
python -m search_skills icp --url https://acme.com
```

流程：

1. DeepSeek Fetch 抓首页
2. 依次尝试 `/about` `/about-us` `/pricing` `/product` `/solutions` `/customers`，最多 `--pages` 页
3. 合并成 ≤24k 字正文
4. 模型按 `prompts/icp_from_site.md` 反推「谁会买」
5. 归一化 + 校验，写 `data/icp.json`，附 `_meta`（来源、页数、置信度、证据）

成本：1~5 次 fetch ≈ 0.005~0.025 元。

### 路径 B：没有官网

交互式（5 个问题，每个都有默认值，回车即用默认）：

```bash
python -m search_skills icp
```

非交互式（适合脚本/CI）：

```bash
python -m search_skills icp --answers answers.json
```

`answers.json` 只认 5 个键：`product` `industries` `titles` `geography` `company_size`。

也可以直接手填模板：

```bash
python -m search_skills icp --template    # 生成 templates/icp.template.json
```

## 输出 schema

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `industries` | string[] | 英文通用行业名，≤6 个 |
| `titles` | string[] | 英文决策岗位，≤6 个 |
| `geography` | string[] | 英文地区，≤4 个 |
| `company_size` | string | `"10-200 employees"` 这种区间 |
| `product` | string | 中文一句话：卖什么 + 给谁 + 解决什么 |
| `_meta` | object | 不给搜索用，记录 method / source / pages / confidence / evidence |

生成的文件能被 `ICP.from_dict()` 直接读取，`run` 和 `leadgen` 共用。

## 官网抓不全怎么办（--fill / --strict）

抓完会算一遍字段覆盖度，写进 `_meta`：

```json
"_meta": {
  "fields": {
    "industries": {"status": "evidence"},
    "titles": {"status": "inferred"},
    "geography": {"status": "user"},
    "company_size": {"status": "default"},
    "product": {"status": "missing"}
  },
  "gaps": ["company_size", "product"],
  "coverage": 0.6
}
```

状态四种：`evidence`（正文可查）/ `inferred`（模型推断）/ `default`（模板值）/ `missing`（空）/ `user`（用户回答）。

| 命令 | 行为 |
| --- | --- |
| `icp --url X --fill` | 只问 `gaps`；覆盖度 < 0.6 时把 `inferred` 字段也拿来确认（方括号里是当前值，回车即沿用） |
| `icp --url X --strict` | `gaps` 非空 → 退出码 2，**不写文件** |
| 默认 | 照写，但在输出里打印 `coverage` 和 `gaps` |

`--fill` 的答案会再走一次归一化（中文 → 英文行业/职位），然后把对应字段标成 `user`。

> PowerShell 管道传中文要加 `$OutputEncoding = [Text.Encoding]::UTF8`，否则中文会乱码；
> 脚本化场景建议用 `--answers answers.json` 更稳。

## 置信度怎么用

| 分值 | 处理方式 |
| --- | --- |
| ≥ 75 | 直接用 |
| 50~74 | 扫一眼 `_meta.evidence`，确认行业/规模没跑偏 |
| < 50 | CLI 会提示；人工改 `industries` 和 `company_size` 两个词再跑 |

## 校验

```bash
python -m search_skills icp --show                      # 打印当前 ICP
python -m search_skills icp --url https://x.com --out data/icp.b.json   # 不覆盖旧文件
```

写完建议立刻验证一次：

```bash
python -m search_skills leadgen --topics 3 --fetch 2
```

能挖出像样的候选名，说明 ICP 的行业/规模写得够具体；挖出来全是巨无霸或无关公司，回去调 `company_size` 和 `industries`。

## 两份提示词

| 文件 | 输入 | 关键约束 |
| --- | --- | --- |
| `prompts/icp_from_site.md` | 官网正文 | 只写官网能支撑的结论；行业/职位英文；规模按客单价反推；≤60 字产品描述 |
| `prompts/icp_from_answers.md` | 5 问 5 答 | 只整理不发明；中文→英文归一；多项拆数组；地区缺失标默认 |

## 铁律

1. **不编造**：官网没证据的行业/地区就落默认值，并在 `_meta.evidence` 里写明“使用默认”。
2. **行业/职位一律英文通用写法**（`SaaS`、`Marketing Agency`、`VP Sales`），否则搜索命中率会掉。
3. **规模用区间**：`N-M employees`，不要写“中小”“大企业”。
4. **产品描述用中文**：给业务流程的人看，不是给搜索引擎看。
5. **生成后必须可被 `ICP.from_dict()` 读取**，字段名不能改。
