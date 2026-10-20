# Contact Enrichment Workflow

How to turn `contact@acme.com` into `Jane Doe (VP Sales) + jane@acme.com` using agents.

## Step 1: Export contact tasks

```bash
python -m search_skills contacts --export-tasks data/contact_tasks.json --limit 20 --min-score 70
# exported: 20 contact tasks -> data/contact_tasks.json
# 把 tasks 交给 /contacts 或其他 agent 研究，结果写成 results.json 再 --import-results 导回
```

This selects leads that:
- Have a generic mailbox (contact@, info@, sales@, ...)
- No named contact yet (or only a role like "Sales Team")
- Match score ≥ 70
- Not already marked as `needs_contact`

## Step 2: Hand the tasks file to an agent

### Option A: Claude's /contacts skill (if available)

```bash
# In a Claude session:
Read data/contact_tasks.json, research each company as described in its "ask" field, and write data/contact_results.json
```

The agent will:
1. Read the task list
2. For each company, try the suggested URLs (team page, about, impressum, contact)
3. Search for press releases if the homepage doesn't list decision-makers
4. Pick ONE decision-maker (C-level, VP, Head of X, or Founder)
5. Quote the evidence URL
6. Write results to `data/contact_results.json`

### Option B: Manual agent prompt

Open `data/contact_tasks.json` and copy the `ask` field from one task. Then prompt an agent:

```
Read these pages and find ONE decision-maker for Acme Corp:
- https://acme.com/about
- https://acme.com/team
- https://acme.com/leadership

If those don't list decision-makers, search:
- site:acme.com (CEO OR founder OR "head of" OR director)
- "Acme Corp" CEO founder press release

Priority: sales, growth, product, or operations roles.

Output format:
{
  "dedupe_key": "acme.com",
  "name": "Jane Doe",
  "title": "VP Sales",
  "email": "jane@acme.com",
  "evidence_url": "https://acme.com/team"
}

If you can't find a direct email, leave "email" as an empty string.
```

Repeat for each company in the task list, collect the results into a JSON array:

```json
{
  "results": [
    { "dedupe_key": "acme.com", "name": "Jane Doe", "title": "VP Sales", "email": "jane@acme.com", "evidence_url": "https://acme.com/team" },
    { "dedupe_key": "firma.de", "name": "Hans Müller", "title": "Geschäftsführer", "email": "", "evidence_url": "https://firma.de/impressum" }
  ]
}
```

Save as `data/contact_results.json`.

## Step 3: Import results

```bash
python -m search_skills contacts --import-results data/contact_results.json
```

Output:
```
  + Acme Corp                    Jane Doe (VP Sales)
  + Firma GmbH                   Hans Müller (Geschäftsführer)
  + Other Corp                   Sarah Lee (Head of Growth)
  - some-company.com             missing evidence_url
  - another.de                   email is still generic, not a person

accepted: 3 | rejected: 2
```

The script validates each result:
- Rejects if `name` or `title` is too short
- Rejects if `evidence_url` is missing or on the wrong domain
- Rejects if `email` is generic (contact@, info@, ...) or on a different domain
- Accepts evidence on the company domain (incl. subdomains) or a press wire (prnewswire, businesswire, globenewswire, presseportal, openpr). LinkedIn is not accepted: it is behind a login wall

Accepted results are written to the database:
- `company_perple` = "Jane Doe (VP Sales)"
- `contact_status` = "found"
- `contact_source` = "agent"
- `contact_evidence` = "https://acme.com/team"
- `company_email` = "jane@acme.com" (if the agent found a direct email)
- `email_kind` = "personal" (direct emails are graded higher than generic ones)

## Step 4: Verify and export the report

```bash
python -m search_skills list --limit 10
python -m search_skills report --force
```

The report now shows:
- **公司名称**: Acme Corp
- **联系人**: Jane Doe (VP Sales)
- **邮箱**: jane@acme.com

## Tips

**Where to look for decision-makers**:
1. Company team page (`/team`, `/about`, `/leadership`, `/management`)
2. Impressum / Imprint (common on German sites, lists Geschäftsführer)
3. Press releases (prnewswire.com, businesswire.com, company blog)
4. LinkedIn can help find a name, but quote the company page or a press release as evidence

**Priority roles** (in order):
1. VP Sales, Head of Sales, Sales Director
2. Head of Growth, VP Marketing
3. Head of Product, VP Product
4. COO, VP Operations
5. CEO, Founder (only if no one else is listed)

**Red flags** (skip these):
- Placeholder names: John Doe, Test User, Example Name
- Generic roles: Sales Team, Support, Customer Service
- Personal LinkedIn profiles (unless you're copying from their work experience)
- Profiles on recruiting sites (glassdoor, indeed, etc.)

**If the company has no public team page**:
- Search for their most recent press release
- Check if they have a LinkedIn company page with employees listed
- Look for conference speaker bios
- If nothing, leave the task out of results.json — better to skip than to guess

## Batch processing

For 50+ companies, consider:
1. Export tasks in chunks of 20
2. Process each chunk with a separate agent session (parallel)
3. Merge all results into one `results.json`
4. Import once

This avoids context-window overflow and speeds up the process.
