# Nivult — Job Postings & Company Data (RapidAPI listing)

Customer-facing overview and technical documentation for the RapidAPI listing. All examples use the RapidAPI proxy host and headers.

---

## 1. LONG DESCRIPTION

Not a scraper. A maintained index.

Nivult reads job postings from employer career systems and public employment portals: Workday, Greenhouse, SAP SuccessFactors, iCIMS, SmartRecruiters and 60+ ATS platforms, across 240+ countries and territories. The index also includes public national employment sources; it is not limited to employer ATS records.

What you get in one subscription:

- **Live job postings** — title, full text where available, location, seniority, remote policy, employment type, observed and modelled salary in separate fields, and technologies extracted by measured classifiers.
- **Company records** — keyed by `ats + company_slug`, with technology aggregate, industry and registry source where available, size band, locations and 30-day hiring pace.
- **A delta feed** — `new`, `updated` and `closed` events since an ISO timestamp. Delta history is retained for seven days.
- **Declared coverage** — percentages for the documented set of fields measured in the latest export. Coverage does not claim to describe every response field.
- **Selected provenance metadata** — source fields are exposed for selected enriched values, not for every field.

Typical uses: labor-market analytics, talent intelligence, sales prospecting, academic research, HR tech products and competitive intelligence.

---

## 2. SPOTLIGHTS

**Spotlight 1 — Employer and public sources**

Records come from employer career systems and public employment portals, not recycled commercial aggregators. Closure timestamps reflect Nivult's source-checking pipeline.

**Spotlight 2 — A delta feed, not just snapshots**

`GET /v1/changes?since=` returns new, updated and closed events. Paginate every response with `next_cursor`; use the timestamp of the previous sync for the next `since` value.

**Spotlight 3 — Declared coverage**

`GET /v1/coverage` returns fill rates for the documented set of posting and company fields. Observed and modelled salary fields are separate.

**Spotlight 4 — Measured enrichment**

Job family, seniority, remote policy and technologies come from classifiers evaluated against hand-labeled benchmarks. Exported records contain the resulting classifications, not benchmark-score fields.

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

List calls return 100 records by default and accept `limit=1` through `limit=500`.

## Quickstart

```bash
export RAPIDAPI_KEY="your_key"
export HOST="nivult-job-postings-company-data.p.rapidapi.com"

curl -H "X-RapidAPI-Key: $RAPIDAPI_KEY" \
     -H "X-RapidAPI-Host: $HOST" \
     "https://$HOST/v1/jobs?technology=SAP&country=DE&limit=100"
```

The response has `data`, `next_cursor` and `count_page`. When `next_cursor` is non-null, pass it back as `cursor`. The keyset cursor avoids offset pagination, but pages are separate live queries, not one transactionally isolated snapshot.

## GET /v1/jobs

Active postings, 100 by default and up to 500 per call.

Parameters:

- `q` — PostgreSQL full-text search over title and description. Multiple terms follow PostgreSQL `plainto_tsquery` semantics.
- `country` — ISO 3166-1 alpha-2, such as `DE`, `US` or `IT`.
- `technology` — case-insensitive exact match against extracted, normalized technologies; not arbitrary text search.
- `category`, `ats`, `seniority`, `remote`, `language` — corresponding exported values.
- `posted_after` — posted on/after `YYYY-MM-DD`.
- `limit` — 1–500, default 100.
- `cursor` — previous response's `next_cursor`.

Records include available fields such as title, company, `company_slug`, `ats`, URL, geography, `posted_at`, seniority, remote, employment type, skills, technologies and full description. Observed salary uses `salary_min`, `salary_max` and `salary_currency`. Modelled salary uses `salary_is_estimate`, `salary_estimate_min`, `salary_estimate_median`, `salary_estimate_max`, `salary_estimate_currency` and `salary_estimate_basis`.

## GET /v1/companies

Company records are keyed by **`ats + company_slug`**, not by slug alone.

Parameters:

- `q` — case-insensitive company-name search.
- `country` — country on the company record, not necessarily every posting location.
- `industry` — exact industry value.
- `technology` — company-level exported technology aggregate; this is not a distinct-employers query over the filtered job slice.
- `employees_min`, `employees_max` — employee-count bounds.
- `limit`, `cursor` — as above.

## GET /v1/companies/{piattaforma}/{slug}

Returns one company record. Supply both the ATS/source and the employer slug from the job or company record.

## GET /v1/changes

Events are `new`, `updated` and `closed`. Updates carry a full posting snapshot and `updated_at`; closures carry `closed_at`.

- `since` is required and must be an ISO 8601 timestamp within the seven-day retention window.
- `limit` and `cursor` paginate the current response.
- `next_cursor` is not the `since` value for the next synchronization. Store an ISO timestamp from the previous sync separately.

If `since` predates `retention_since`, the response is HTTP 400 with `code: delta_retention_exceeded` and `resync_required: true`. Rebuild from the active snapshot or bulk export before resuming.

## GET /v1/exports/latest

Returns the latest available export manifest. This endpoint is authenticated and metered but does not require the direct bulk-file tier.

## GET /v1/exports/latest/{nome}

`nome` is `offerte` or `aziende`; the response is JSONL gzip. Direct accounts require a monthly tier of at least 50,000 credits. One-time top-ups do not unlock this tier. RapidAPI does not include bulk-file downloads.

## GET /v1/coverage

Returns percentages from 0 to 100 for the documented set of fields measured in the latest export. It does not claim coverage for every possible response field.

## GET /v1/usage

Direct usage is account-level across keys, including revoked keys. The monthly allowance is spent before purchased credits. This endpoint itself consumes one credit and returns 429 at a zero balance. RapidAPI returns a channel note because RapidAPI manages its own quotas.

## Errors

- **400** — invalid date/cursor, missing `since`, or an expired delta-retention window.
- **401** — missing, invalid or revoked credentials.
- **403** — the channel or monthly tier does not include the requested resource.
- **404** — company, export name or file not found.
- **422** — FastAPI parameter validation failed, such as `limit=0`, `limit=501` or a non-integer limit. Its `detail` is an array.
- **429** — plan quota/rate limit on RapidAPI, or exhausted monthly and purchased credits on direct keys.
- **503** — the direct credit counter is unavailable; the request fails closed.

Unknown query parameters are ignored. Unknown filter values generally produce an empty page rather than a 422.

A direct-key credit-exhausted response has an object in `detail`:

```json
{
  "detail": {
    "errore": "monthly and purchased credits exhausted",
    "crediti_mensili": 1000,
    "crediti_extra": 0,
    "mese_uso": "2026-11-01"
  }
}
```

A delta-retention error has a structured `detail` containing `code`, `error`, `message`, `since`, `retention_since`, `action` and `resync_required`.

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

### Step 4 — Request recent changes

Generate a timestamp within the seven-day retention window:

```bash
SINCE=$(python -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())')
curl -H "X-RapidAPI-Key: $RAPIDAPI_KEY" -H "X-RapidAPI-Host: $HOST" \
  --get "https://$HOST/v1/changes" \
  --data-urlencode "since=$SINCE" \
  --data-urlencode "limit=500"
```

Paginate until `next_cursor` is null. For the next run use a separately stored sync timestamp, not the pagination cursor.

### Step 5 — Check field coverage

```bash
curl -H "X-RapidAPI-Key: $RAPIDAPI_KEY" -H "X-RapidAPI-Host: $HOST" \
  "https://$HOST/v1/coverage"
```

Use the returned percentages to decide which documented fields your pipeline can rely on.
