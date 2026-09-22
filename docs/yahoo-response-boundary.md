# Bounded Yahoo response-boundary diagnosis

This diagnostic answers a narrower question than whether a financial value is
correct: **did the same HTTP chart response contain the value that the production
adapter later marked missing?** It does not repair or publish a price dataset.

## Why this is needed

Main run `35687508312` at `bb487d6e6d3690481ceb94603d626dee1123aa88`
retained 89,989 provider-frame observations for 19 issuers, including 144 rejected
issuer/date rows. All 19 had a missing 2026-09-21 Close. The saved DataFrames alone
cannot distinguish upstream omission from adapter conversion. Prior observations
and their immutable artifacts are not modified by this diagnostic.

## Implementation and limits

`python -m scripts.audit_yahoo_boundary --directory NEW_DIRECTORY --symbol RELIANCE.NS
--start 2026-09-18 --end 2026-09-22` invokes the unchanged production adapter once
for one issuer and at most ten calendar days. The committed workflow uses the
explicit four-day window containing September 18 and September 21, not the entire
historical request. A different request can return different source content;
this is not a reconstruction of an earlier HTTP response.

The observer wraps `yfinance.data.YfData.get` and the adapter's existing frame
audit, forwarding the same arguments and returning the identical response/audit.
It makes no extra HTTP request, sets no cookies, resets no session, does not
change user agents, and adds no retry. Existing yfinance internal behavior is
unchanged. Only a matching chart request with exact requested period boundaries
is eligible for capture; timezone bootstrap requests are not treated as evidence
for the target window. The implementation is pinned to yfinance 1.7.0 and refuses
another version until reviewed.

Only a successful, valid chart document with the requested symbol, INR currency,
Asia/Kolkata timezone, equity instrument type and daily granularity is saved as
`chart-response.json`. Its response bytes are preserved unchanged. No request
headers, cookies, crumbs, signed URLs or unsuccessful HTTP bodies are retained.
The DataFrame seen by the production validator is copied for comparison without
modification. Missing source values, adapter-only missing values, changed values
and unmatched dates are reported separately, rather than forced into agreement.
Array lengths and duplicate/out-of-range dates are rejected.

The manifest carries run/attempt/SHA, explicit request bounds and hashes/lengths
of captured files. `probe_status=complete` means the boundary observation completed,
NOT that the price frame is valid, the feed is reliable or history is complete.
A failed adapter contract keeps the CLI and workflow failed; artifact upload
never turns invalid prices green. Successful diagnostics are not connected to
Download Stock Market Data or Data Analysis Report. A metadata/shape failure
preserves a bounded diagnostic error, not a fabricated explanation.

## Reviewed upstream implementation

The pinned history implementation calls YfData.get/cache_get before response.json
and quote parsing:
https://github.com/ranaroussi/yfinance/blob/1.7.0/yfinance/scrapers/history.py

## Official-source route reviewed, not enabled

The existing `market_data/sources.py` on `abhijitsingh14-run-stock-market-app`
(blob `4c05454c576237cc116dee155d7dea92eea58a3d`) already contains NSE UDiFF URLs
and field mappings. It was inspected, not blindly imported: its parser assigns
the requested date and skips records lacking Close; that is insufficient for
an independent exact-session reconciliation contract.

NSE's report page identifies the legacy cash-market CSV formats as discontinued
from 2024-07-08 and points to the UDiFF final ZIP:
https://www.nseindia.com/all-reports

The current NSE website terms explicitly restrict systematic/automated collection.
No evidence of a specific overriding permission for this user was supplied. No
new exchange scraper or bulk-download schedule is introduced here. An approved
source arrangement or lawfully supplied existing bhavcopy can be reconciled
offline with its embedded dates and issuer identities validated first. Report
availability is not a blanket automation or redistribution licence:
https://www.nseindia.com/static/nse-terms-of-use

Existing exchange collectors are not repaired or altered by this diagnostic.
No source values, validation thresholds, scope denominators, HDFC archival work,
production workflow permissions, Mac databases or unrelated branches change.
