# RapidAPI pricing proposal — not live

This is an internal proposal for RapidAPI Studio. No plan, quota, price, or live listing has been changed.

## Current state and constraints

- No RapidAPI plan or price configuration is versioned in this repository. Check the live tiers in RapidAPI Studio before applying this proposal.
- Direct API metering is pay per result: jobs 1 credit, companies 3 credits, changes 1 credit. Direct usage, coverage, export manifest, and Daily Bulk cost 0 credits and retain authentication, rate limits, and entitlements.
- The backend change prepared here emits `X-RapidAPI-Billing: Credits=N` only for requests authenticated through the RapidAPI proxy. Jobs bill per returned record, companies bill 3 per returned record, and empty results and application errors below 500 bill 0. Coverage and usage bill 0 custom credits. Server errors do not set the header; direct API calls never receive it.
- Delta feed, export manifest, and bulk files are direct-only. Missing-company onboarding, historical backfill, custom company lists, and redistribution remain direct/Enterprise offerings.

## Direct reference prices

| Direct plan | Monthly price | Included credits | Effective price / 1K credits |
| --- | ---: | ---: | ---: |
| One-time top-up | €40 | 10,000 | €4.00 |
| Starter | €49 | 15,000 | €3.27 |
| Growth | €149 | 100,000 | €1.49 |
| Scale | €449 | 500,000 | €0.90 |
| Business | €899 | 1,500,000 | €0.60 |
| Enterprise | Custom | 5M+ | Custom |

## Proposed RapidAPI tiers — USD, four fixed Studio slots

Use **hard limits with overage disabled**. Prices are USD and account for the **25% RapidAPI marketplace commission** shown separately below. MEGA is the marketplace ceiling at 100,000 credits/month.

| RapidAPI Studio slot | Monthly price | Custom `Credits` quota | Gross price / 1K | Net after 25% fee | Net / 1K |
| --- | ---: | ---: | ---: | ---: | ---: |
| BASIC | $0/mo | 500 | — | $0.00 | — |
| PRO | $75/mo | 15,000 | $5.00 | $56.25 | $3.75 |
| ULTRA | $119/mo | 50,000 | $2.38 | $89.25 | $1.79 |
| MEGA | $219/mo | 100,000 | $2.19 | $164.25 | $1.64 |

Recommended plan rules:

- All slots: jobs, company list/detail, coverage, and usage; no Delta, manifest, or bulk downloads.
- All slots: 15 requests/second, burst 30, matching the backend's current paying/RapidAPI profile.
- Jobs consume 1 custom credit per returned record; companies consume 3. Empty results and application errors below 500 consume 0 custom credits.
- Coverage and usage are excluded from the custom `Credits` quota and explicitly return `Credits=0`.
- Keep a separate request quota if desired for abuse protection, but document both quotas explicitly. Do not silently treat one API request as one credit: a request can return up to 500 records.

## RapidAPI Studio activation checklist

1. Deploy and test the backend billing middleware before changing the live plans.
2. Create the custom `Credits` quota and associate it **only** with `/v1/jobs`, `/v1/companies`, and `/v1/companies/{piattaforma}/{slug}`. Leave `/v1/coverage` and `/v1/usage` outside the custom quota.
3. With Test Endpoint, verify jobs and company billing, empty datasets, company 404, validation 400/422, and direct-only 403 responses.
4. Specifically verify that `X-RapidAPI-Billing: Credits=0` is transmitted as zero and **is not rounded to 1 by the proxy**. Test coverage and usage even though they are outside the custom quota.
5. Verify that 5xx responses do not emit the billing header and that direct `api.nivult.com` responses never emit it.
6. Verify quota enforcement with small test allocations before entering the four USD price points above.
7. Recheck existing live plans and subscriptions. Plan migrations, overage behavior, and publication are manual marketplace operations and are not authorized by this proposal.
