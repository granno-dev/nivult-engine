# Nivult — Job Postings & Company Data (RapidAPI listing)

Customer-facing overview and technical documentation for the RapidAPI listing. Examples use the RapidAPI proxy host and headers.

---

## 1. LONG DESCRIPTION

Query **6.3M+ active job postings** and **72,000+ company records** across **240+ countries and territories**. The maintained Nivult index covers **66 source platforms**, including ATS and public employment sources, with daily refreshes and closed-posting detection.

Search records from **Workday, Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, SAP SuccessFactors**, and other supported sources. The API queries the existing index; it does not scrape employer sites on demand.

What you can access through RapidAPI:

- **Live job postings** — title, employer, source URL, description where available, geography, seniority, workplace, employment type, skills, technologies, and observed salary where available.
- **Company records** — keyed by `ats + company_slug`, with industry, size, locations, technology aggregate, legal data, and selected source fields where available.
- **Declared field coverage** — fill rates for the documented posting and company fields measured in the latest export.
- **Keyset pagination** — retrieve up to 500 records per call with opaque cursors.

Typical uses include labor-market analytics, talent intelligence, sales prospecting, academic research, HR tech products, and competitive intelligence.

### Enterprise and high-volume use cases

For **Daily Delta feeds, Daily bulk JSONL exports, more than 50K–100K credits per month, missing-company requests across the 66 supported source platforms, historical backfill, custom company lists including non-standard career sites, or redistribution rights**, use the direct API at [nivult.com](https://nivult.com). These capabilities are not included in RapidAPI plans. Direct Scale and Business plans include missing-company requests; Enterprise covers custom sourcing, backfill, redistribution licensing, and volumes from 5M credits per month.

---

## 2. SPOTLIGHTS

**Spotlight 1 — Maintained global coverage**

Access 6.3M+ active postings from 72,000+ company records across 240+ countries and territories. Sources include employer source platforms, career sites, staffing sources, and public employment portals.

**Spotlight 2 — Filters for precise slices**

Filter jobs by source platform, employer, domain, geography, technology, skills, department, employment type, posting date, and observed salary. Keyword filters accept comma-separated OR values; `q` and `title` require all search terms.

**Spotlight 3 — Declared coverage**

`GET /v1/coverage` returns fill rates for the documented set of posting and company fields. Enrichment and provenance are available on covered fields, not guaranteed on every row.

**Spotlight 4 — Separate observed and modelled salary fields**

Observed salary uses `salary_min`, `salary_max`, and `salary_currency`. Modelled salary fields, when populated, use the `salary_estimate_*` namespace and are never presented as employer-provided salary.

---

## 3. DOCUMENTATION

# Nivult — Job Postings & Company Data

## Authentication

RapidAPI fills these headers in its generated snippets:

```text
X-RapidAPI-Key: <your RapidAPI key>
X-RapidAPI-Host: nivult-job-postings-company-data.p.rapidapi.com
```

Direct Nivult API keys use `X-Api-Key` against `https://api.nivult.com` instead. Do not put either key in the URL.

RapidAPI manages plan quotas and billing. The custom `Credits` quota counts returned data records: 1 credit per job and 3 per company. Empty results and application errors below 500 bill 0 credits; coverage and usage also return `Credits=0`. Direct Nivult credit balances are not consumed. See the Pricing tab for the live quota and overage configuration.

List calls return 100 records by default and accept `limit=1` through `limit=500`. RapidAPI requests are limited to 15 requests/second with a burst of 30.

## Quickstart

```bash
export RAPIDAPI_KEY="your_key"
export HOST="nivult-job-postings-company-data.p.rapidapi.com"

curl -H "X-RapidAPI-Key: $RAPIDAPI_KEY" \
     -H "X-RapidAPI-Host: $HOST" \
     "https://$HOST/v1/jobs?technology=SAP&country=DE&limit=100"
```

The response has `data`, `next_cursor`, and `count_page`. When `next_cursor` is non-null, pass it back as `cursor`. Pages are separate live queries, not one transactionally isolated snapshot.

```json
{
  "data": [
    {
      "id": "01JABC...",
      "title": "Data Engineer",
      "company": "Example GmbH",
      "company_slug": "example-gmbh",
      "ats": "greenhouse",
      "country": "DE",
      "city": "Berlin",
      "remote": "hybrid",
      "technologies": ["Snowflake", "Python"],
      "url": "https://boards.greenhouse.io/example/jobs/123"
    }
  ],
  "next_cursor": "opaque-cursor",
  "count_page": 1
}
```

## GET /v1/jobs

Returns active postings. Unknown query parameters are ignored; unknown filter values generally produce an empty page.

### General filters

- `country` — ISO 3166-1 alpha-2; comma-separated values are ORed.
- `category`, `ats`, `seniority`, `remote`, `language`, `technology` — corresponding exported values; comma-separated values are ORed.
- `exclude_ats` — comma-separated source platform slugs to exclude. A value present in both `ats` and `exclude_ats` is excluded.
- `q` — full-text search over title and description; all terms are required (AND).
- `q_any` — at least one of up to 15 comma-separated keywords must match the title or description (OR). Can be combined with `q`.
- `title` — full-text search restricted to the job title; all terms are required (AND).
- `posted_after` — inclusive ISO 8601 date or timestamp; takes precedence over `dal` when both are supplied.
- `dal` — deprecated legacy alias for `posted_after`, used only when `posted_after` is absent.
- `posted_before` — exclusive ISO 8601 date or timestamp.
- `limit` — 1–500, default 100.
- `cursor` — previous response's `next_cursor`.

Successful RapidAPI responses include `X-RapidAPI-Billing: Credits=N`, where `N` is the number of returned jobs; an empty page returns `Credits=0`.

`technology` is a case-insensitive exact match against extracted, normalized technologies, not arbitrary text search. `ats=bundesagentur`, used alone, is an alias for `bundesanstellung`.

### Employer, location, and source filters

- `source_type` — `direct`, `aggregator`, `staffing`, or `public_board`; comma-separated OR.
- `company` — full-text match on employer name.
- `company_slug`, `company_domain` — comma-separated OR. Domain normalization removes scheme, `www`, and path.
- `city`, `state` — comma-separated OR.

### Role, skills, and salary filters

- `employment_type`, `department`, `skills`, `salary_currency` — comma-separated OR.
- `has_salary` — `true` or `false`; tests whether `salary_min` or `salary_max` is present.
- `salary_min`, `salary_max` — non-negative finite bounds on the corresponding observed salary fields. `salary_min` cannot exceed `salary_max`.

Job records can include title, company, `company_slug`, `ats`, URL, geography, `posted_at`, `first_seen`, `last_seen`, seniority, workplace, employment type, skills, technologies, and full description. Observed salary uses `salary_min`, `salary_max`, and `salary_currency`. Modelled salary uses `salary_is_estimate`, `salary_estimate_min`, `salary_estimate_median`, `salary_estimate_max`, `salary_estimate_currency`, and `salary_estimate_basis` when populated. Optional fields can be omitted, null, or empty.

## GET /v1/companies

Company records are keyed by **`ats + company_slug`**, not by slug alone.

- `q` — case-insensitive company-name search.
- `country` — country on the company record, not necessarily every posting location.
- `industry` — exact industry value.
- `technology` — company-level exported technology aggregate; this is not a distinct-employers query over a filtered job slice.
- `employees_min`, `employees_max` — employee-count bounds.
- `limit`, `cursor` — as above.

Available fields include employer name, domain, active-job count, industry, legal and registration data, size band, locations, technology stack, and selected source fields. Enrichment is optional and varies by employer.

Successful RapidAPI responses include `X-RapidAPI-Billing: Credits=3N`, where `N` is the number of returned companies; an empty page returns `Credits=0`.

## GET /v1/companies/{piattaforma}/{slug}

Returns one company record. Supply both the ATS/source and employer slug from a job or company record. A missing company returns 404 with `X-RapidAPI-Billing: Credits=0`. A successful record bills 3 custom credits and returns `X-RapidAPI-Billing: Credits=3`.

## GET /v1/coverage

Returns percentages from 0 to 100 for the documented set of fields measured in the latest export. Company-field percentages are nested under `aziende`. It does not claim coverage for every possible response field. Direct accounts pay 0 credits and can use it at zero balance. RapidAPI returns `X-RapidAPI-Billing: Credits=0`.

## GET /v1/usage

Direct usage costs 0 credits, does not change usage, and returns 200 at zero balance. RapidAPI returns a channel note because RapidAPI manages marketplace quotas, together with `X-RapidAPI-Billing: Credits=0`; it does not return a direct Nivult credit balance.

## Not included on RapidAPI

- `GET /v1/changes` — Daily Delta feed; direct Growth and higher only, 1 direct credit per returned event.
- `GET /v1/exports/latest` — daily export manifest; direct Scale and higher only, 0 direct credits including at zero balance.
- `GET /v1/exports/latest/{nome}` — Daily bulk JSONL download; direct Scale and higher only, 0 direct credits including at zero balance.
- Missing-company onboarding, custom company lists, historical backfill, and redistribution rights — direct plans or Enterprise agreements as applicable.

These endpoints return 403 through RapidAPI. One-time direct top-ups do not unlock Delta or Daily Bulk entitlements.

## Errors

- **400** — invalid date, cursor, source type, boolean, salary bound, more than 15 `q_any` terms, missing `since`, or an expired Delta retention window on the direct API.
- **401** — missing, invalid, or revoked credentials.
- **403** — the channel or direct subscription does not include the requested resource.
- **404** — company, export name, or file not found.
- **422** — FastAPI parameter validation failed, such as `limit=0`, `limit=501`, or a non-integer limit. Its `detail` is an array.
- **429** — RapidAPI plan quota/rate limit, backend rate limit, or exhausted direct credits on paid-data endpoints. Direct Usage, coverage, and Daily Bulk never return 429 for zero balance.
- **503** — the direct credit counter or database is temporarily unavailable; the request fails closed.

Authenticated RapidAPI application errors below 500 include `X-RapidAPI-Billing: Credits=0`. Server errors do not set the billing header.

A direct-key credit-exhausted response has an object in `detail`:

```json
{
  "detail": {
    "error": "monthly and purchased credits exhausted",
    "monthly_credits": 15000,
    "purchased_credits": 0,
    "usage_month": "2026-11-01"
  }
}
```

If a direct balance cannot cover one result, `detail` instead contains `error`, `remaining`, and `credits_per_record`. A backend rate-limit 429 contains a string `detail` and a `Retry-After` header. RapidAPI gateway errors can use RapidAPI's own response format.

---

## 4. TUTORIAL — Your first sync

Set the proxy credentials once:

```bash
export RAPIDAPI_KEY="your_key"
export HOST="nivult-job-postings-company-data.p.rapidapi.com"
```

### Step 1 — Query jobs

```bash
curl -H "X-RapidAPI-Key: $RAPIDAPI_KEY" -H "X-RapidAPI-Host: $HOST" \
  "https://$HOST/v1/jobs?technology=Snowflake&limit=100"
```

### Step 2 — Paginate

Copy a non-null `next_cursor` into `CURSOR` and request the next page:

```bash
CURSOR="paste_next_cursor_here"
curl -H "X-RapidAPI-Key: $RAPIDAPI_KEY" -H "X-RapidAPI-Host: $HOST" \
  --get "https://$HOST/v1/jobs" \
  --data-urlencode "technology=Snowflake" \
  --data-urlencode "limit=100" \
  --data-urlencode "cursor=$CURSOR"
```

Repeat with each new cursor until `next_cursor` is null. Do not invent offsets.

### Step 3 — Retrieve the employer record

Use both `ats` and `company_slug` from a posting:

```bash
ATS="greenhouse"
SLUG="employer_slug_from_the_posting"
curl -H "X-RapidAPI-Key: $RAPIDAPI_KEY" -H "X-RapidAPI-Host: $HOST" \
  "https://$HOST/v1/companies/$ATS/$SLUG"
```

Do not use `q=snowflake` as a substitute: company `q` searches company names, not technologies or job text.

### Step 4 — Check field coverage

```bash
curl -H "X-RapidAPI-Key: $RAPIDAPI_KEY" -H "X-RapidAPI-Host: $HOST" \
  "https://$HOST/v1/coverage"
```

Use the returned percentages to decide which documented fields your pipeline can rely on. For ongoing Delta synchronization or a full snapshot rebuild, use the direct API at [nivult.com](https://nivult.com); those feeds are not part of RapidAPI plans.
