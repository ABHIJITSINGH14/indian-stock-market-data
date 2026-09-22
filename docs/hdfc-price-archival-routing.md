# HDFC historical-price routing: outstanding work, not recovered data

## Why this exception exists

HDFC Bank's issuer announcement makes the HDFC Ltd merger effective 2023-07-01:
https://www.hdfc.bank.in/press-release/2023/q2/hdfc-ltd-to-merge-into-hdfc-bank-effective-july-1-2023

The existing Yahoo price collector returned an empty HDFC.NS frame in main run
35685081036 on 2026-09-22, after it had examined four other issuers:
https://github.com/ABHIJITSINGH14/indian-stock-market-data/actions/runs/35685081036

That is observed adapter unavailability, not proof that HDFC has no history or
that Yahoo will never return it. The merger alone is NOT a last-trading-date
record. A broker's linked BSE circular (20230704-36) could not be retrieved in
this review; no exchange suspension boundary is derived from inaccessible text.
No NSE or BSE OHLCV archive was retrieved as part of this routing change.

## Execution and coverage contract

The orchestrator enables `defer_archival_prices=True`. Direct collector/CLI
usage remains unchanged unless this explicit option is enabled. The reviewed
policy matches only HDFC.NS and only execution dates on/after 2026-09-22. This is
execution knowledge, not a feature for historical backtesting.

Before any provider request, the collector atomically finalizes a separate JSON
worklist under `data/raw/price-archive-requests/plan-*/manifest.json`. It records
the full original requested universe, the unchanged HDFC request interval,
reviewed policy and source evidence, run/attempt/commit and the remaining
provider candidates. A byte-count/SHA256 reference is included in coverage.
A persistence failure stops the task before network calls or deferral.

HDFC stays in `requested`, `not_returned` and `not_attempted`, and is explicitly
listed in `deferred_archival`. `pending_provider` counts candidates not yet
attempted; it excludes planned archival work. Therefore these are distinct:

- All 19 provider candidates examined: iteration progress only.
- HDFC archival work outstanding: original 20-issuer scope is still incomplete.
- A structurally valid observed row: not independent exchange-price verification.

The original requested interval is never clipped. `last_trading_date`,
`replacement_symbol` and `archive_source` remain null. The work item says
`archive_fetch_status=awaiting_verified_source`. No archival downloader or
validated archive endpoint is supplied by this change. No post-merger HDFC
prices or HDFC Bank replacement history are synthesized.

A known planned archival request does not authorize ignoring unknown empty
responses, denials, rate limits or malformed results from another issuer.
Existing strict failures, numerical-row isolation, source requests, adjustment
settings and evidence preservation are unchanged. An outstanding HDFC request
keeps the original task unsuccessful even when every other frame passes.

## Artifact separation and verification

The original `downloaded-data` success condition and production analysis guard
are unchanged. The worklist uses its own `price-archival-work-<run>-<attempt>`
artifact; it cannot qualify as market data or satisfy the CSV upload condition.
Only finalized manifests are uploaded, never unfinished `.tmp` files. Retention
is seven days, like diagnostics: this is NOT permanent archival storage.

The existing historical diagnostic now exercises HDFC.NS followed by
ICICIBANK.NS, with both explicit routing and numerical-isolation options. Its
expected result is still failure while HDFC is outstanding, even if the second
issuer returns a valid frame. Verify that HDFC was not requested, the next
issuer was attempted, original bounds and scope remain, the plan hash matches,
and every returned frame's complete/usable/rejected accounting is preserved.

Actual HDFC recovery needs a permitted, identifiable archival input, full
source/issuer/session validation, original bytes and documented price basis.
The worklist is the handoff to that unresolved task, not a claim it has run.
