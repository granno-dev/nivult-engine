![Nivult — live job postings, read at the source](https://raw.githubusercontent.com/granno-dev/nivult-engine/main/apify/assets/copertina.png)

## Not another scraper. A maintained index.

Most job actors ask you for career-site URLs, then spend minutes scraping them, then hand you raw rows. **This Actor asks for nothing but filters** — and answers in seconds, because the work is already done: we read the career systems of tens of thousands of employers **every day**, whether you run us or not.

| | Scrape-on-demand actors | **Nivult Job Index** |
|---|---|---|
| You supply | career-site URLs you must know | nothing — just filters |
| Answer in | minutes of scraping | **seconds** |
| Closed postings | stay in your data forever | **verified closed**, daily |
| Enrichment | raw title + link | seniority, workplace, language, skills, salary signal |
| Company layer | a name string | **spine record: industry, size, legal identity, tech stack** |
| Provenance | none | **field-level, on every row** |

## What does the Nivult Job Index do?

We read job postings **directly from the career systems of tens of thousands of employers** — Workday, Greenhouse, Lever, SmartRecruiters, SuccessFactors and dozens more — across 240+ countries and territories. Refreshed daily; closed when the posting leaves the employer's own page.

Filter by what matters:

- 🌍 **Country** — 240+ countries and territories, ISO-2
- 🧩 **Technology** — postings that mention SAP, Snowflake, Kubernetes… in the text
- 🎯 **Seniority & workplace** — intern to head, remote / hybrid / on site
- 🗓️ **Posted-after date** — only what is fresh
- 🏢 **Company mode** — industry, size band, legal identity from public registries, measured technology stack

![How it works: you set filters, the index refreshes daily, you get clean rows in seconds](https://raw.githubusercontent.com/granno-dev/nivult-engine/main/apify/assets/come-funziona.png)

## How to use it

1. Choose **Job postings** or **Companies**.
2. Set your filters — country, technology, keyword, seniority, workplace, posted-after.
3. Run, and download the dataset as JSON, CSV, or Excel — or read it through the Apify API.

Every result set is a slice of the same index we sell as a full daily feed.

## How much does it cost — and why is a row worth more here?

**$6 per 1,000 rows received** — nothing for empty filters, nothing upfront. On Apify's free plan, your monthly free usage covers your first hundreds of rows.

A fair question: raw-scrape actors charge cents per thousand. A raw row is a title and a link that may be dead by tonight. Our row is an **enriched, verified record**: classified seniority and workplace, measured skills and language, salary signal, a link to the company's spine record, and the source of every field — closed out of the index the day it leaves the source. You are not paying for a scrape; you are paying for the maintenance.

For the full daily export or a custom feed (millions of rows, JSONL, daily deltas), write to us — the Actor page links to the contact.

## What does the output look like?

A real row from the index (read today, verbatim — fields with no source stay empty; we never invent):

```json
{
  "id": "945ba2d5-2250-4c76-bac6-e7ae2040dcfc",
  "title": "Junior Internal Auditor",
  "company": "Leonardo",
  "ats": "workday",
  "country": "IT",
  "city": "IT - Roma - Via Tiburtina KM12",
  "seniority": "junior",
  "language": "it",
  "posted_at": "2026-10-06T11:00:02Z",
  "url": "https://leonardocompany.wd3.myworkdayjobs.com/leonardocareersite/job/IT---Roma---Via-Tiburtina--KM12400/Junior-Internal-Auditor_R0028587"
}
```

Company mode returns one record per employer: name, domain, industry, size band, legal form from public registries, technology stack, and the count of live postings.

## FAQ

**Where does the data come from?**
From the employers' own career systems — the page where the company itself publishes the opening. A closure is verified: the posting left the source.

**How fresh is it?**
The index refreshes every day; new postings land within 24 hours of appearing at the source.

**Is this legal?**
We read public pages that employers publish for candidates, respect robots directives and rate limits, and honor removal requests. The company records enrich public postings with public registries.

**Can I get the data through an API instead of the UI?**
Yes — the dataset of every run is readable through the Apify API, and the full index has its own REST API with daily JSONL exports. Ask us through the store page.

**Something looks off — who do I tell?**
Write to us from the store page. We read everything, and we fix fast.
