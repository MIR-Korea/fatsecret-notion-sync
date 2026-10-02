# FatSecret dual-output synchronization

The existing 30-minute GitHub Actions schedule is preserved (`7,37 * * * *`).
Each run reads the most recent seven Korean calendar dates once, validates/deduplicates each snapshot, then sends the same food values to:
1. Supabase, first: atomic daily totals + food upserts + deletions for successfully fetched dates only.
2. Existing Notion food database and daily-summary database, independently.

Notion failure cannot prevent Supabase writes. Failed outputs make the run fail with a sanitized service/date/error-type message; the next scheduled run retries the recent window. Runs are serialized without cancelling an in-flight request. No cross-service transaction is claimed: temporary lag is possible until a retry succeeds.

## Existing configuration (GitHub repository Actions Secrets)
- FATSECRET_CONSUMER_KEY
- FATSECRET_CONSUMER_SECRET
- FATSECRET_ACCESS_TOKEN
- FATSECRET_ACCESS_TOKEN_SECRET
- NOTION_TOKEN (read/update/insert access to both existing Notion databases)
- NOTION_DATABASE_ID (existing food database)

No new token or Supabase key is needed. GitHub OIDC authenticates to the existing nutrition-ingest Edge Function; its repository, branch, workflow allowlist and audience remain unchanged. Never put values in source or logs. The daily-summary database ID remains the existing constant.

## Notion food schema
| Property | API type |
|---|---|
| Name | title |
| Date | date |
| Meal | select (아침 / 점심 / 저녁 / 간식) |
| Food | rich_text |
| FatSecretID | rich_text |
| Calories / Carbs / Protein / Fat | number |

The existing Memo property is left untouched. Startup checks the raw database schema after Supabase writes, so a Notion configuration error cannot block Supabase.

FatSecretID is the logical key. Existing pages are patched; same-day duplicate pages are archived. An ID lookup across the database reuses a page whose date changed. Deleted FatSecret records are archived in Notion; pages without a FatSecretID are preserved. Full-day deletion produces a zero daily summary. Only dates successfully read from FatSecret are reconciled.

## Supabase deployment
`supabase_snapshot.sql` defines a SECURITY INVOKER function accessible only to service_role. The authenticated Edge Function calls it for `snapshot: true`, after validating dates, counts, IDs and nutrition totals. Upsert/deletion/summary are one transaction with an account-level advisory lock.
Legacy days/entries/checkpoint payloads used by the backfill remain supported without deletion.
Edge Function source: `supabase/functions/personal-os-nutrition-ingest/index.ts`.

Deletion reconciliation is intentionally limited to the seven-day polling window. Older corrections require an explicit historical reconciliation; the resumable historical backfill is unchanged.

## Tests
`python -m unittest -v test_sync`
Covers same food values, repeat-run deduplication, duplicate cleanup, all-food deletion, manual rows, date movement, invalid values, and independent output failures.
Database verification used transaction rollback: upsert, stable row ID, delete, zero summary and anonymous access denial.
