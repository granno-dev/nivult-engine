# Nivult — Job Postings & Company Data (RapidAPI listing)

Contenuto completo della vetrina RapidAPI (08/10/2026): overview, spotlights
e documentazione, da incollare in Studio → Docs / Hub Listing.
Tutto in inglese: il marketplace e' internazionale. Ogni numero citato
viene dall'indice vero — gli stessi della landing, mai inventati.

---

## 1. LONG DESCRIPTION (Hub Listing → General → Long description)

Not a scraper. A maintained index.

Nivult reads job postings directly from the career systems of tens of
thousands of employers — Workday, Greenhouse, SAP SuccessFactors, iCIMS,
SmartRecruiters and 60+ more ATS platforms, across 240+ countries and
territories. Where other job APIs scrape job boards or recycle stale
aggregators, every Nivult record comes from the employer's own page:
a posting is marked closed only when it disappears at the source,
never because it grew old.

What you get in one subscription:

- **Live job postings** — title, full text, location, seniority, remote
  policy, employment type, salary (observed and modelled, never mixed),
  technologies extracted from the text by our own measured classifiers.
- **Company profiles** — tech stack, industry with its registry source,
  size band, locations and 30-day hiring pace. One record per employer,
  linkable from every posting.
- **A delta feed** — new, updated and closed postings since your last
  sync. Your copy never goes stale, and you only pull what changed.
- **Declared coverage** — the fill rate of every field, measured on the
  latest export and published. Gaps are declared, never hidden.

Refreshed every morning. Every field carries its source.

Typical users: labor-market analytics, talent intelligence, sales
prospecting (who is hiring for what, where), academic research, HR tech
products, competitive intelligence.


---

## 2. SPOTLIGHTS (Overview → Add Spotlight — 4 card, titolo + testo)

**Spotlight 1**
Title: Direct from employers, never job boards
Body: Every record is read from the employer's own career system across
60+ ATS platforms. A posting is closed only when it disappears at the
source — so your dataset does not silently rot.

**Spotlight 2**
Title: A delta feed, not just snapshots
Body: GET /v1/changes?since= delivers new, updated and closed postings
from your last sync. Sync daily, pull only what changed, keep your copy
alive without re-downloading millions of rows.

**Spotlight 3**
Title: Coverage you never have to ask for
Body: The fill rate of every field is measured on the latest export and
served at GET /v1/coverage. Salary observed vs modelled is never mixed,
and low-coverage fields stay visible by design.

**Spotlight 4**
Title: Enriched by measured models
Body: Job family, seniority, remote policy and technologies come from
our own classifiers, scored class by class against hand-labeled
benchmarks. The per-class scores ship with the data, not the marketing.


---

## 3. DOCUMENTATION (Overview → Add API Documentation)

# Nivult — Job Postings & Company Data

Millions of live job postings and the companies behind them, read
directly from employer career systems. This guide gets you from sign-up
to a production sync in five minutes.

## Authentication

Every request needs two headers, already filled in by RapidAPI when you
use the code snippets on this page:

```
X-RapidAPI-Key: <your key>
X-RapidAPI-Host: nivult-job-postings-company-data.p.rapidapi.com
```

One API call = one request on your plan quota. Each list call returns
up to 100 records.

## Quickstart: your first call

```bash
curl -H "X-RapidAPI-Key: $KEY" \
     -H "X-RapidAPI-Host: nivult-job-postings-company-data.p.rapidapi.com" \
     "https://nivult-job-postings-company-data.p.rapidapi.com/v1/jobs?technology=SAP&country=DE&limit=100"
```

The answer is JSON with three keys:

```json
{
  "data": [ { "title": "Data Engineer", "company": "...", "...": "..." } ],
  "next_cursor": "eyJvZmZzZXQiOi...",
  "count_page": 100
}
```

**Pagination:** when `next_cursor` is not null, pass it back as
`&cursor=<value>` to get the next page. Keep going until it is null.
Never build offsets yourself: the cursor is the only contract that
stays correct while the index moves underneath you.

## Endpoints

### GET /v1/jobs — live job postings

One record per opening, up to 100 per call.

Parameters (all optional except where noted):

- `q` (string) — free text over title and description
- `country` (string) — ISO 3166-1 alpha-2, e.g. `DE`, `US`, `IT`
- `technology` (string) — technology mentioned in the text: `SAP`, `Snowflake`, `Kubernetes`…
- `category` (string) — job family from our measured classifier
- `ats` (string) — source platform: `workday`, `greenhouse`, `successfactors`…
- `seniority` (string) — `intern`, `junior`, `mid`, `senior`, `lead`, `head`
- `remote` (string) — `remote`, `hybrid`, `onsite`
- `language` (string) — posting language, e.g. `en`, `de`, `fr`
- `posted_after` (string) — posted on/after this date (YYYY-MM-DD)
- `limit` (int) — page size, 1–100 (default 100)
- `cursor` (string) — from the previous page's `next_cursor`

Every record includes, where present at the source: title, company,
company_slug, ats, url, country, city, location, posted_at, seniority,
remote, employment_type, skills, technologies, salary_min/max/currency,
salary_estimate (modelled — never mixed with observed values),
description (full text), language, and per-field provenance for the
enriched fields.

### GET /v1/companies — company profiles

One record per employer. Join it to postings via `company_slug`.

Parameters (all optional):

- `q` (string) — company name search
- `country` (string) — ISO code
- `industry` (string) — e.g. `financial services`
- `technology` (string) — stack currently used
- `employees_min`, `employees_max` (int) — headcount band
- `limit`, `cursor` — as above

Records include: company, domain, country, industry (+ its registry
source), size band, locations, technologies, top_skills, active_jobs
and jobs_posted_30d — the 30-day hiring pace.

### GET /v1/changes — the delta feed

What changed since your last sync: events of type `new`, `updated`,
`closed`.

Parameters:

- `since` (string, **required**) — ISO 8601, e.g. `2026-09-01T00:00:00Z`
- `limit`, `cursor` — as above

The standard pattern: store the timestamp of your last successful sync,
call `/v1/changes?since=<timestamp>` once a day, apply the events to
your copy. This is the cheapest way to keep a local mirror alive.

### GET /v1/coverage — declared field fill rates

No parameters. Returns the measured fill rate of every field on the
latest export, for postings and for companies. Use it to decide which
fields your pipeline can rely on before you build on them.

## Errors

- **400** — bad parameter (e.g. missing `since`, unreadable cursor)
- **401** — missing or invalid key
- **403** — resource not included in this channel (e.g. the daily bulk export)
- **404** — not found
- **429** — plan quota or rate limit reached

Error bodies are `{"detail": "..."}` in English.

## Notes and limits

- Data is refreshed every morning (European time). `posted_at` is the
  employer's own date; `first_seen` is when we first read the posting.
- Salary: `salary_*` fields are observed at the source;
  `salary_estimate_*` fields are modelled. They are never mixed — pick
  one family per analysis.
- The daily bulk export of the full index is licensed separately:
  hello@nivult.com.
- Terms of use: lawful business use, no resale of API access outside
  your organization, no profiling of individuals. Full text on the
  listing page.
