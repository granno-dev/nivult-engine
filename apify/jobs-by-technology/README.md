![Nivult — live job postings, read at the source](https://raw.githubusercontent.com/granno-dev/nivult-engine/main/apify/assets/copertina.png)

## What does this dataset do?

This Actor finds job postings by the technology stack they actually mention in the text — not just the title. SAP, Snowflake, Kubernetes, Salesforce: if the employer's posting says it, we index it. Refreshed daily from the career systems of tens of thousands of employers.

Use it to extract **job postings** or **enriched company records** with the filters that matter:

- 🌍 **Country** — 240+ countries and territories, ISO-2 filtered
- 🧩 **Technology** — postings that mention SAP, Snowflake, Kubernetes… in the text
- 🎯 **Seniority & workplace** — intern to head, remote / hybrid / on site
- 🗓️ **Posted-after date** — only what is fresh
- 🏢 **Company mode** — industry, size band, legal identity from public registries, measured technology stack

Every field carries **field-level provenance**: the record says where each value came from, so you can audit what you buy.

## How to use it

1. Choose **Job postings** or **Companies**.
2. Set your filters — country, technology, keyword, seniority, workplace, posted-after.
3. Run, and download the dataset as JSON, CSV, or Excel — or read it through the Apify API.

Every result set is a slice of the same index we sell as a full daily feed.

## How much does it cost to extract job postings data?

**You pay per result: $6 per 1,000 rows received** — nothing for empty filters, nothing upfront. On Apify's free plan, your monthly free usage covers your first hundreds of rows, so you can evaluate the data before paying anything.

For the full daily export or a custom feed (millions of rows, JSONL, daily deltas), write to us — the Actor page links to the contact.

## What does the output look like?

A real row from the index (posting published the same morning):

```json
{
  "id": "a19ff499-a360-4d87-a37d-5332a5a28804",
  "title": "SAP EAM Consultant",
  "company": "NTT DATA",
  "country": "IT",
  "city": "Milano",
  "remote": "hybrid",
  "seniority": "mid",
  "language": "it",
  "skills": ["sap", "eam", "asset management"],
  "posted_at": "2026-10-06T09:53:33Z",
  "url": "https://careers.example.com/jobs/12345"
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
