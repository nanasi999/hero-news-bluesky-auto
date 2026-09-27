# Facebook automatic posting

Design reviewed before implementation, 2026-09-28.

## Scope
- Page identity verified by user Graph Explorer: 1366592206538225.
- Secret: FACEBOOK_PAGE_ACCESS_TOKEN, stored only in Actions secrets.
- Observed expiry: 1795733588 (2026-11-27). No promise of perpetual validity.
- New facebook/ files and a dedicated workflow only. Do not edit existing SNS
  jobs, Telegram, WordPress, secrets, or the shared social ledger.
- Reuse the read-only article extraction from telegram/relay.mjs; no translation.
- Japanese article title and article link, using Meta's Page /feed endpoint.

## Delivery
- Read confirmed source rows only, canonical article identity, deduplicate SNS.
- Initial baseline excludes historical posts. Publish only the latest article
  with a verified Bluesky createdAt timestamp, then future source articles.
- Own facebook-state.json on the existing state branch, checked by file SHA.
- Workflow concurrency plus durable reservation BEFORE POST. POST is never retried.
- Unknown response stays sending; human reconciliation is required, never resend.
- Verify returned post ID with a GET and exact message before confirming delivery.
- Crash after POST or before final checkpoint leaves a reservation, not a retry.
- Read/write failures are fail-closed. Never publish before a confirmed checkpoint.

## Failure and notification policy
- Safe reads have bounded retries and timeouts. Do not log HTTP bodies or secrets.
- Store incident code and cooldown in own state before reporting first incident.
- Same incident produces a paused/degraded summary, not another failed job/email.
- Clear incident only after successful processing, not after authentication alone.
- Ambiguous publication continues to show degraded state until reconciled.
- An explicit Meta rejection is retained as pending, safe to try after cooldown.
- A state-store outage cannot durably deduplicate alerts; report it honestly.
- Workflow failures before code starts (GitHub outage, checkout, tests) cannot be
  suppressed by application logic. Do not globally mute account notifications.
- Warn in job summary starting 14 days before known token expiry; never auto-renew
  credentials or hide an expired/revoked token as successful publication.

## Design review A: publication and recovery
- Baseline, source duplicates, two overlapping runs, reserve conflict, timeout
  after remote acceptance, bad response, final checkpoint loss, process restart.
- No ambiguous path may send twice. Known failed attempts may be retried only
  when Meta returned a structured non-transient rejection before acceptance.

## Design review B: isolation and operations
- Only own file is writable, source is read-only, fixed authorized Page ID.
- No token in URL/log or artifact; redirects disabled; fixed API hosts.
- No article-site fetches; one activation post, max 10 normal posts per run.
- Failure persistence prevents notification loops; summaries distinguish paused
  and confirmed. Expiry remains a future credential-maintenance requirement.
- Tests and live identity verification precede publication.

## Primary contract
https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/page.py
create_feed accepts message/link and POSTs to /feed. Graph version v25.0 was
also verified in the user's successful Explorer identity request.
