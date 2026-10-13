# October 12 collector failure: evidence, repair and unresolved coverage

Inspected run: https://github.com/ABHIJITSINGH14/indian-stock-market-data/actions/runs/38323511022

Commit: `4198c8b9b355d54fb79b3f36e8717024037bb6e0` (also main during inspection).
Job: `115056775059`. Setup, checkout and dependency installation succeeded;
collection failed with zero successful *tasks*. The analysis run `38323638378`
was correctly skipped. The September repairs (#2, #3, #5–#10) are already merged.
PR #1 concerns a different branch and is not part of this repair.

## Findings by task

| Task | Established cause in this run | Source versus implementation boundary |
| --- | --- | --- |
| Historical equities | 18 provider DataFrames failed whole-frame admission: 125 rows had missing Open/High/Low/Close. | Rejection is correct. No numerical normalization defect was reproduced in our validator. These artifacts contain post-yfinance frames, not the original chart bytes; upstream omission versus adapter loss remains unestablished. |
| BSE company list | `response.json()` raised a JSON decode error before company normalization. | No usable JSON reached the normalizer. Original HTTP status/type/body were not preserved. A fresh single request returned HTTP 403, `text/html`, 400 bytes; that is new availability evidence, not reconstruction of October's response. Separately, the old parser could accept an empty/wrong table or missing identities and retain stale data on reuse. |
| Fundamentals | RELIANCE failed the positive, finite, numeric `marketCap` gate; the remaining active issuers were not attempted. | Correct rejection. The original summary discarded `last_response_metadata`, so it cannot establish whether the field was absent, invalid, or lost in yfinance. No original quote response exists in these artifacts. No calculated cap or alternative field is a justified repair. |
| Bulk/block deals | The first bulk request, `bulkdeals_14Jul2026.csv`, failed with `TLSV1_ALERT_INTERNAL_ERROR`; block was not attempted after the shared source failure. | Transport failed before CSV parsing. It is not a no-deals day, not a pandas defect, and not proof that every provider URL is globally retired. |
| Corporate actions | The legacy `www1.nseindia.com/content/corporate/` request failed with the same TLS alert. | Transport failed before HTML parsing. Its positional table parser remains an unverified migration risk; changing table mappings cannot fix a TLS handshake. |

The repeated rejected dates are 2010-02-06, 2012-01-07, 2012-03-03,
2012-09-08, 2012-11-11, 2014-03-22 and 2015-02-28. Seventeen frames have
all seven; COALINDIA has six, excluding 2010-02-06. Weekend dates are not
automatically invalid: special sessions require independent evidence. No date
filter, fill, repair=True, keepna=False or partial-frame admission was introduced.

The 18 rejected frames contain 87,590 rows: 87,465 structurally usable diagnostic
rows plus 125 rejected rows. All stay quarantined. DMART independently passed
whole-frame admission: the partial task CSV has 2,362 rows, dated 2017-03-21
through 2026-10-09. That observation does not establish full exchange or session
coverage. HDFC remains in a separate outstanding archival worklist with no
successor substitution. The requested history was 2006-10-17 through exclusive
2026-10-12, not 60–70 years or a full exchange universe.

## Small reliable changes

- BSE requires a nonempty table, dictionary rows, six-digit scrip codes,
  nonblank names, Indian ISIN shape and unique code/ISIN identities. A malformed
  row rejects the entire response. This is structural admission, not independent
  certification of a listing, ISIN checksum or exchange-universe completeness.
  Clear previous rows before a new attempt, and distinguish source request,
  JSON decode and payload validation failures.
- Preserve fundamentals shape/type metadata, attempted/unattempted identities
  and the failing category in the existing download summary, including when an
  explicit provider exception stops the loop. Existing financial gates and
  stopping behavior are unchanged.
- API diagnostics record source URL without query/user information, HTTP
  status/content type, redirect status codes, byte count/hash, attempt count and
  transport failure category. They do not retain response bodies, cookies,
  credentials or provider exception text. A new request/attempt clears previous
  metadata. The existing orchestrator includes these records in the log artifact.

This PR fixes validation/state and evidence-loss defects. **It does not restore
unavailable source data or make the production download green.** Source URLs,
retry limits, TLS verification, schedules, requested universe, HDFC archival
obligations, price/market-cap checks and production success gates are unchanged.

## Verified artifacts

Downloaded all four original ZIPs and verified SHA256 against the run metadata:

| Artifact ID | Name | ZIP SHA256 |
| --- | --- | --- |
| 11779995823 | price-rejection-evidence-38323511022-1 | `7a58ef5c39275ce150b31c085ce5dc86c0263b0feb5ceb835b7ad0b203d0e1eb` |
| 11779985789 | price-archival-work-38323511022-1 | `86407f574a2caff85e9340809d87502bec0021e411b7b3656d8acce3f368490e` |
| 11780095570 | partial-download-data-38323511022-1 | `cf08dd8fb9c044b3ff218b6c9731adf78f02ad5cc2e114431c90b39d3f03ba6a` |
| 11780240379 | download-logs | `bf920366d39ac1a8051e209f21f340461b1ee3cf1d7e0ea510b993bc9e7310f3` |

Verified all 18 rejection manifests' run/SHA, file lengths and file hashes.
Replayed the saved provider frames through `audit_price_frame`: every accepted
position and rejected date/position/reason matched. Zero rows were admitted by
the replay. The original ZIPs expire on October 19 under existing retention;
this document records their identities/hashes, not a replacement archival copy.

## Validation and remaining gate

160 offline tests pass locally, including 12 new synthetic regression tests
and a strengthened real-orchestrator summary assertion. Tested in a clean
Python 3.12 environment with the actual requirements.txt pins; pip check passes.
Compilation and git diff --check pass. The actual analyzer consumes isolated,
explicitly synthetic artifact-smoke inputs successfully. Hosted PR CI must
independently verify Python 3.11 and the separate-runner artifact handoff.

A single bounded existing Yahoo chart-to-frame probe for RELIANCE, 2010-02-05
through exclusive 2010-02-09, failed under provider HTTP 429 and captured no
matching chart response. It did not close any historical gap; no repeated probe,
session reset or access bypass followed. A direct chart request also returned
429. Future successful short-window responses cannot reconstruct the original
20-year response or justify splicing values between observations.

Before source restoration can be claimed:

1. Capture an identity-checked response from the same price adapter at an affected
   window using `scripts.audit_yahoo_boundary`; distinguish raw nulls, adapter
   loss and changed values, then independently reconcile any proposed correction.
2. Obtain usable BSE responses with documented schema/identity fixtures, plus
   verified, authorized replacement NSE deal/action sources. Do not guess URLs
   or disable TLS. Prior source review is in `docs/yahoo-response-boundary.md`.
3. Inspect the newly preserved fundamentals metadata on a future attempt. If cap
   remains absent/invalid, leave the snapshot unadmitted; any secondary source
   needs issuer, currency, timestamp and value-basis evidence before integration.
4. Retrieve verified HDFC archives; reconcile missing historical sessions and
   listing lifecycles; expand the explicitly requested universe/date scope only
   as a separate change. Partial data must never unlock the production analyzer.

Reproducible local regression command:

```bash
python -m pip install -r requirements.txt
python -m pip check
python -m unittest discover -s tests -p 'test_*.py' -v
```
