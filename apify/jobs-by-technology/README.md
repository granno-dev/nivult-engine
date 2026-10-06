![Who is hiring for Snowflake today — Nivult](https://raw.githubusercontent.com/granno-dev/nivult-engine/main/apify/assets/copertina-tecnologie.png)

## What does "Jobs by Technology" do?

Type a technology — **SAP, Snowflake, Kubernetes, Salesforce, anything** — and get every live job posting that mentions it **in the text of the ad**, not just in the title. Read daily from the career systems of tens of thousands of employers, worldwide.

For sales teams it is a lead list ("who is implementing Snowflake right now?"), for recruiters a talent map, for analysts a demand signal — measured from what employers actually write.

- 🧩 **Technology matching in the full text** — the stack the posting really asks for
- 🌍 **Country** — 240+ countries and territories
- 🎯 **Seniority & workplace** — intern to head, remote / hybrid / on site
- 🗓️ **Posted-after date** — only what is fresh
- 🏢 **Company mode** — the employers behind the postings, with industry and size

Every field carries **field-level provenance**: the row says where each value came from, so you can audit what you buy.

### Not a scraper — a maintained index

Scrape-on-demand actors ask you for career-site URLs and minutes of your
patience, then hand you raw rows. **This dataset asks for nothing but
filters** — the reading already happened today, whether you run us or not.

| | Scrape-on-demand | **Nivult Dataset API** |
|---|---|---|
| You supply | URLs you must know | nothing — just filters |
| Answer in | minutes | **seconds** |
| Closed postings | stay forever | **verified closed**, daily |
| Enrichment | raw title + link | seniority, workplace, language, skills |
| Provenance | none | **field-level, on every row** |

![How it works: you set filters, the index refreshes daily, you get clean rows in seconds](https://raw.githubusercontent.com/granno-dev/nivult-engine/main/apify/assets/come-tecnologie.png)

## How to use it

1. Type the technology (e.g. `Snowflake`) and set filters if you want.
2. Run it, then download JSON, CSV or Excel — or read the dataset through the Apify API.
3. For a standing feed of a technology every morning, ask about the daily feed (link on this page).

## How much does it cost?

**$3 per 1,000 rows received** — nothing for empty filters, nothing upfront. Apify's free plan covers your first hundreds of rows.

Why a row here costs more than a raw scrape: a raw row is a title and a link that may be dead by tonight. Ours is an **enriched, verified record** — classified, provenance-stamped, and closed out of the index the day it leaves the source. You are not paying for a scrape; you are paying for the maintenance.

## What does the output look like?

A real row from a Snowflake-mentioning posting, read the same morning (verbatim — the skills list is long because it is measured from the whole text):

```json
{
  "title": "Delivery Manager - Data Analytics Spread & Commissions",
  "ats": "icims",
  "country": "US",
  "city": "Hanover",
  "posted_at": "2026-10-06",
  "skills": ["python", "sql", "aws", "azure", "gcp", "etl", "scrum", "agile", "project management", "warehouse", "snowflake", "unstructured data", "use logical reasoning"]
}
```

## Explore the Nivult family

- **[Job Postings Dataset — the full index](https://apify.com/nivult_developers/global-job-postings)** — every ATS, every filter, company mode
- **[Workday Jobs Dataset](https://apify.com/nivult_developers/workday-jobs-dataset)** — every Workday career site, daily
- **[Greenhouse Jobs Dataset](https://apify.com/nivult_developers/greenhouse-jobs-dataset)** — every Greenhouse board, daily
- **[Jobs by Technology](https://apify.com/nivult_developers/jobs-by-technology)** — postings matched by the stack in the text

## FAQ

**How do you know which technologies a posting asks for?**
We read the full text of the ad at the source and measure the stack it mentions — a posting that says "SAP" in the body counts even if the title doesn't say it.

**How fresh is the data?**
Daily. New postings land within 24 hours of appearing on the employer's career system.

**Which technologies are covered?**
Anything employers write — mainstream clouds and databases, ERP, CRM, data tools, languages. If it appears in postings, it is filterable.

**Is this legal?**
We read public postings on the employer's own site, respect robots directives and rate limits, and honor removal requests.
