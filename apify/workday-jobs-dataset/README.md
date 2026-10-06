![Every Workday career site, read live — Nivult](https://raw.githubusercontent.com/granno-dev/nivult-engine/main/apify/assets/copertina-workday.png)

## What does this Workday jobs dataset do?

Thousands of the world's largest employers run their hiring on **Workday** — this Actor reads every public Workday career site directly, every day, and hands you the postings as clean, structured rows. When a posting leaves the employer's Workday page, it closes here too: the dataset never lies about what is open.

Filter **Workday job postings** by what matters:

- 🌍 **Country** — 240+ countries and territories
- 🎯 **Seniority & workplace** — intern to head, remote / hybrid / on site
- 🔎 **Keyword** — free text in title and description
- 🗓️ **Posted-after date** — only what is fresh

Every field carries **field-level provenance**: the row says where each value came from, so you can audit what you buy.

### Not a scraper — a maintained index

Scrape-on-demand actors ask you for career-site URLs and minutes of your
patience, then hand you raw rows. **This dataset asks for nothing but
filters** — the reading already happened today, whether you run us or not.

| | Scrape-on-demand | **This dataset** |
|---|---|---|
| You supply | URLs you must know | nothing — just filters |
| Answer in | minutes | **seconds** |
| Closed postings | stay forever | **verified closed**, daily |
| Enrichment | raw title + link | seniority, workplace, language, skills |
| Provenance | none | **field-level, on every row** |

## How to use it

1. Open the Actor and set your filters — or none, for the full Workday slice.
2. Run it, then download JSON, CSV or Excel — or read the dataset through the Apify API.
3. For the whole Workday universe every morning, ask about the daily feed (link on this page).

## How much does a Workday jobs dataset cost?

**$6 per 1,000 rows received** — nothing for empty filters, nothing upfront. Apify's free plan covers your first hundreds of rows.

Why a row here costs more than a raw scrape: a raw row is a title and a link that may be dead by tonight. Ours is an **enriched, verified record** — classified, provenance-stamped, and closed out of the index the day it leaves the source. You are not paying for a scrape; you are paying for the maintenance.

## What does the output look like?

A real row, read the same morning from a live Workday board:

```json
{
  "title": "Manager - Finance & Strategy",
  "company": "Flextronics",
  "ats": "workday",
  "city": "Chennai, IN",
  "posted_at": "2026-10-06",
  "url": "https://flextronics.wd1.myworkdayjobs.com/careers/job/..."
}
```

## FAQ

**Is this the official Workday API?**
No — and it doesn't need your Workday credentials. We read the public career pages that employers publish for candidates, politely and within their terms.

**How fresh is the data?**
The Workday slice refreshes every day; new postings land within 24 hours of appearing on the board.

**Do you cover other ATS platforms?**
Yes — Greenhouse, Lever, SmartRecruiters, SuccessFactors and dozens more. The full index (all platforms, all filters) has its own Actor; niche datasets like this one exist for the platforms people ask for most.

**Is this legal?**
We read public postings on the employer's own site, respect robots directives and rate limits, and honor removal requests.
