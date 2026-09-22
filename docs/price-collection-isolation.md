# Isolate numerical row defects without weakening admission

## Why this change exists

The scheduled production run on September 22 stopped historical price collection
at the first RELIANCE frame. Earlier verified artifacts contained thousands of
structurally usable rows and seven missing-price rows. A known numerical defect
in a preserved frame need not prevent examination of the next explicitly requested
issuer. It is not equivalent to an empty, denied or ambiguous provider response.

## Native execution policy

`NSEDataDownloader` keeps its strict default and whole-frame admission contract.
The existing `run_all_downloads` orchestrator explicitly enables
`continue_after_row_rejection=True`. This changes iteration, not data admission.

Continuation requires all of the following: an unambiguous numeric schema and
datetime index; at least one usable row; at least one rejected row; only numerical
row-rejection reasons; and successful finalization of the existing checksum-linked
evidence manifest. Schema or date defects, an entirely unusable response, missing
source data, unknown exceptions and failed evidence writes still stop the task.
Explicit 401/403/429 and yfinance rate-limit exceptions still propagate without
new retries. The library request arguments, request spacing, issuer order, date
range, source URLs and TLS behavior are unchanged.

A rejected issuer stays in `not_returned`; its usable subset stays diagnostic.
Only independently passing whole frames enter the task CSV. The overall boolean
result stays false while any requested issuer is unreturned. Existing workflow
rules therefore publish these CSVs only as failed-run partial artifacts, never
as `downloaded-data`, and production financial analysis remains success-gated.
No calendar exclusions, imputation, successor-issuer substitution or fabricated
market-cap values are introduced.

## Inspectable accounting

Coverage now distinguishes `attempted`, `not_attempted`, `returned` (admitted
frames), `not_returned`, `row_quality_rejected`, per-issuer `observations`, and a
specific `stop_reason`. Every historical-completeness field stays `not_verified`.
The full requested denominator is retained. The absence of a stop reason means
iteration exhausted its input, not that collection or historical coverage passed.

## Verification

The existing one-issuer historical audit command remains compatible. The bounded
hosted audit is configured for RELIANCE.NS followed by TCS.NS with continuation
enabled. If either response is rejected, the audit stays failed and uploads its
run-linked evidence. Inspect the artifact to prove the second request actually
occurred, verify the full/usable/rejected partition and hashes, and check that no
rejected frame entered the task CSV. This is not a successful full production run.

Original offline tests remain unchanged. Added tests cover mixed/valid sequences,
strict opt-out, row conservation, entirely missing responses, schema/date faults,
write failures, unknown errors, explicit denials and stale state across issuers
and reused collector instances.

The seven earlier session gaps still require issuer/segment-specific evidence.
This change neither repairs nor classifies them. It also does not restore legacy
exchange endpoints, fill HDFC archival financials or guarantee Yahoo field
availability. The existing restoration issue remains open.
