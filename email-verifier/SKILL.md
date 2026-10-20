---
name: email-verifier
description: 给已有线索查邮箱：MX 校验 → 搜索结果 snippet 取邮箱 → 回官网兜底 → 过滤占位符/角色邮箱 → 写回状态。
---

# email-verifier（验证邮箱）

一句话：**domain → 可信邮箱**，或者诚实地标 `no_contact`。

## 命令

```bash
python -m search_skills emails --limit 20          # 处理积压
python -m search_skills emails --retry-days 7     # 重试老的 no_contact
```

## 流程

```text
pending 线索（domain 非空）
 ├─ MX 校验（dnspython）  查不到 MX → domain_dead，不花搜索调用
 ├─ search '"@域名"'      邮箱常直接出现在 snippet 里（实测 4/4 命中）
 ├─ 过滤                  域名必须一致 / 挡占位符 / 挡角色邮箱 / 要求附近有 contact 字样
 ├─ 兜底                  搜到本人域的 contact/impressum 链接 → 抓；没有就抓根域
 └─ 写回                  found / no_contact / domain_dead + kind + attempts
```

## 状态机

| 状态 | 含义 | 后续 |
| --- | --- | --- |
| `pending` | 还没验证 | 下次跑会处理 |
| `found` | 找到合格邮箱 | 进报表 |
| `no_contact` | 试过，没有公开邮箱 | 7 天后重试，最多 3 次 |
| `domain_dead` | 域名没有 MX 记录 | 不再处理 |
| `rejected` | 找到的全是废邮箱 | 不再处理 |

## 邮箱分类

| kind | 例子 | 报表 |
| --- | --- | --- |
| `named` | `zhang.san@acme.com` | 排前面 |
| `generic` | `info@` `sales@` `contact@` | 排后面，报表标 `generic` |
| blocked | `hr@` `pr@` `jane.doe@` `someone@gmail.com` | 直接丢 |

### 拦截规则

- 角色邮箱：`hr jobs careers privacy datenschutz legal impressum noreply postmaster abuse pr press media`
- 占位符：`jane.doe` `yourname` `username` `example` `template` `application.development` 等
- 形似句子：local part 里 3 个以上点（`zoom.application.development.use`）
- 跨域：邮箱域名 ≠ 线索域名（第三方目录页里别人的邮箱）
- 无上下文：snippet 里的地址周围 70 字符内没有 contact/email/kontakt 等字样

## 成本

每条 1 次搜索 + 0~2 次抓取（本地渲染免费）≈ **0.005~0.01 元**。
18 条积压实测 16 次调用。

## 铁律

1. **绝不猜** `info@域名` 这种模式，只抄页面上出现的。
2. MX 查不到就不花搜索调用（省钱且诚实）。
3. 找不到就标 `no_contact`，不要留在 `pending` 里反复烧钱。
