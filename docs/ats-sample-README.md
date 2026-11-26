# Sample Dataset: Normalized Job Postings from 65 ATS Platforms

This repository contains a free, static sample of job postings from enterprise Applicant Tracking Systems including Workday, Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, and SAP SuccessFactors.

Nivult maintains a normalized index and runs measured classifiers to extract job family, seniority, workplace, skills, and technologies. The full index also includes career sites, staffing sources, aggregators, and public employment portals.

## Scale of the full Nivult index

This repository is a static sample. The current public index contains at least:

- **6,300,000** active job postings
- **73,000** company records with active postings
- **240** countries and territories
- **150,000** new postings per day

These are conservative public minimums, not a live count for this sample. The exact current snapshot is available from `GET /v1/demo/stats`.

## About this sample

- **Format:** `.jsonl.gz` (gzipped JSON Lines — one JSON object per line)
- **Content:** job title, company, full description text where available, location, seniority, workplace, skills, and technologies extracted from the text
- **Source:** employer career systems and other source types included in the Nivult index

## What a record looks like

A real row from this sample, lightly trimmed for readability:

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

`posted_at` is the source posting date when available and can be estimated when the source does not expose an exact date. `first_seen` and `last_seen` are Nivult observation dates. A posting is marked closed when it leaves the source.

## Quickstart (Python)

```python
import gzip
import json

with gzip.open("Jobs Sample.gz", "rt") as f:
    for line in f:
        job = json.loads(line)
        print(f"{job['title']} at {job['company']} — {job['country']}")
```

## Need the live index?

This repository is a snapshot. The full index refreshes daily. Daily Delta feeds and Daily bulk JSONL exports are available through direct Nivult plans, not through the RapidAPI marketplace plans.

- **Live API on RapidAPI:** https://rapidapi.com/nivult-nivult-default/api/nivult-job-postings-company-data
- **Actors on Apify Store:** search for "Nivult"
- **Direct API, Delta, bulk exports, and portal trial:** https://nivult.com
