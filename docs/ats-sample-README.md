# 📊 Sample Dataset: Normalized Job Postings from 65+ ATS Platforms

This repository contains a free, static sample of job postings read directly from enterprise Applicant Tracking Systems (Workday, Greenhouse, SAP SuccessFactors, iCIMS, Lever, and many more).

Scraping ATS platforms is notoriously painful. The data is messy, job titles don't reflect the actual tech stack, and aggregators are full of expired listings. We built Nivult to solve this: we index directly from the source — the employer's own career system — and run our own measured classifiers to extract and normalize what matters.

### 🚀 Scale of the full Nivult index
This repository is a static sample. The latest daily export contains at least:

*   **5,900,000** active job postings
*   **69,000** companies hiring now
*   **240** countries and territories
*   **85,000** new postings per day

These are conservative public minimums; the exact snapshot is available from `GET /v1/demo/stats`.

### 💡 About this sample
* **Format:** `.jsonl.gz` (gzipped JSON Lines — one JSON object per line)
* **Content:** job title, company, full description text, location, seniority, remote policy, skills and technologies extracted from the text
* **Source:** employer career systems and public employment portals — no commercial aggregators

### 💡 What a record looks like
A real row from this sample (lightly trimmed for readability):

```json
{
  "id": "00000557-0612-4acb-ac16-7cfe80692c33",
  "title": "Showroom Manager, Chicago at The Mart",
  "ats": "greenhouse",
  "company_slug": "lgelectronics",
  "company": "Lgelectronics",
  "url": "https://job-boards.greenhouse.io/lgelectronics/jobs/5194675008",
  "country": "US",
  "location": "Illinois",
  "language": "en",
  "seniority": "lead",
  "remote": "onsite",
  "skills": ["excel", "crm", "recruiting", "electronics"],
  "posted_at": "2026-04-21 12:54:09+00:00",
  "first_seen": "2026-09-09 13:44:15+00:00",
  "last_seen": "2026-10-08 04:35:00+00:00",
  "description": "Step into the innovative world of LG Electronics..."
}
```

`posted_at` is the employer's own date; `first_seen`/`last_seen` are our reading dates — a posting is marked closed only when it disappears at the source.

### 🐍 Quickstart (Python)

```python
import gzip, json

with gzip.open("Jobs Sample.gz", "rt") as f:        # the file is gzipped
    for line in f:
        job = json.loads(line)
        print(f"{job['title']} at {job['company']} — {job['country']}")
```

### ⚡ Need the live index?

This repository is a snapshot. The full index refreshes every morning and ships a delta feed (new, updated, closed postings) so your copy never goes stale.

👉 **Live API on RapidAPI:** https://rapidapi.com/nivult-nivult-default/api/nivult-job-postings-company-data
👉 **Actors on Apify Store:** search "Nivult"
👉 **Site & free portal trial:** https://nivult.com
