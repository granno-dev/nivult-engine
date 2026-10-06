![Every Greenhouse board, read live — Nivult](https://raw.githubusercontent.com/granno-dev/nivult-engine/main/apify/assets/copertina-greenhouse.png)

## What does this Greenhouse jobs dataset do?

**Greenhouse** is the ATS of the world's tech companies — startups, scale-ups, and the names you know. This Actor reads every public Greenhouse board directly, every day, and gives you the postings as clean, structured rows. When a posting leaves the board, it closes here too: the dataset never lies about what is open.

Filter **Greenhouse job postings** by what matters:

- 🌍 **Country** — 240+ countries and territories
- 🎯 **Seniority & workplace** — intern to head, remote / hybrid / on site
- 🔎 **Keyword** — free text in title and description
- 🗓️ **Posted-after date** — only what is fresh

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

![How it works: you set filters, the index refreshes daily, you get clean rows in seconds](https://raw.githubusercontent.com/granno-dev/nivult-engine/main/apify/assets/come-greenhouse.png)

## How to use it

1. Open the Actor and set your filters — or none, for the full Greenhouse slice.
2. Run it, then download JSON, CSV or Excel — or read the dataset through the Apify API.
3. For the whole Greenhouse universe every morning, ask about the daily feed (link on this page).

## How much does a Greenhouse jobs dataset cost?

**$6 per 1,000 rows received** — nothing for empty filters, nothing upfront. Apify's free plan covers your first hundreds of rows.

Why a row here costs more than a raw scrape: a raw row is a title and a link that may be dead by tonight. Ours is an **enriched, verified record** — classified, provenance-stamped, and closed out of the index the day it leaves the source. You are not paying for a scrape; you are paying for the maintenance.

## What does the output look like?

A real row, read the same day from a live Greenhouse board:

```json
{
  "title": "Summer Internship 2027",
  "company": "Hankooktireamericacorp",
  "ats": "greenhouse",
  "country": "US",
  "remote": "onsite",
  "seniority": "intern",
  "language": "en",
  "technologies": ["MS-Office"],
  "salary_estimate_median": 52300,
  "salary_estimate_currency": "USD",
  "salary_is_estimate": true,
  "posted_at": "2026-10-06",
  "url": "https://job-boards.greenhouse.io/hankooktireamericacorp/jobs/5445258008"
}
```

## Explore the Nivult family

- **[Job Postings Dataset — the full index](https://apify.com/nivult_developers/global-job-postings)** — every ATS, every filter, company mode
- **[Workday Jobs Dataset](https://apify.com/nivult_developers/workday-jobs-dataset)** — every Workday career site, daily
- **[Greenhouse Jobs Dataset](https://apify.com/nivult_developers/greenhouse-jobs-dataset)** — every Greenhouse board, daily
- **[Jobs by Technology](https://apify.com/nivult_developers/jobs-by-technology)** — postings matched by the stack in the text

## FAQ

**Is this the official Greenhouse API?**
No — and it doesn't need Greenhouse credentials. We read the public boards that companies publish for candidates, politely and within their terms.

**How fresh is the data?**
The Greenhouse slice refreshes every day; new postings land within 24 hours of appearing on the board.

**Do you cover other ATS platforms?**
Yes — Workday, Lever, SmartRecruiters, SuccessFactors and dozens more. The full index (all platforms, all filters) has its own Actor; niche datasets like this one exist for the platforms people ask for most.

**Is this legal?**
We read public postings on the employer's own board, respect robots directives and rate limits, and honor removal requests.
