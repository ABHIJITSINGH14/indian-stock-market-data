# Issuer and session reconciliation — 2026-09-21

## Two distinct identity defects

Both default Yahoo lists used `INFOSY.NS`. Infosys' current issuer FAQ identifies its NSE exchange code as `INFY` and Indian equity ISIN as `INE009A01021`. Its 2016–17 annual report also identifies the NSE code as `INFY`. The corrected configured Yahoo request is `INFY.NS`; hosted validation must still check that the returned issuer is Infosys. This is a repository configuration correction, not evidence of a historical exchange-symbol change.

Primary evidence, reviewed 2026-09-21:
- https://www.infosys.com/investors/shareholder-services/faqs.html (FAQs 12–13)
- https://www.infosys.com/content/dam/infosys-web/en/investors/reports-filings/annual-report/annual/Documents/AR-2017/shareholder-information.html (listing codes and ISIN)

The lists keep their original sizes and ordering (20 prices, 15 fundamentals), correcting only that spelling. Explicit `INFOSY.NS` input is rejected with an explanation; no fuzzy alias or silent input rewrite is performed. Existing files are not renamed or relabelled by this change.

HDFC Ltd merged into HDFC Bank effective 2023-07-01. It is not a separate company with a current standalone fundamentals snapshot. This is not the same event date as withdrawal of its exchange trading instruments. The NSE F&O circular separately excludes its derivatives from 2023-07-13; that circular alone is NOT evidence of an equity last-trading date.

Primary evidence:
- https://www.hdfc.bank.in/press-release/2023/q2/hdfc-ltd-to-merge-into-hdfc-bank-effective-july-1-2023 (issuer merger announcement, dated 2023-06-30)
- https://archives.nseindia.com/content/circulars/FAOP57430.pdf (NSE derivatives circular, dated 2023-07-04)

The current-snapshot collector records `HDFC.NS` as `archival_required` from the merger effective date, avoids a pointless provider lookup, and continues to the remaining requested companies. It does not fetch HDFC Bank as a replacement. HDFC remains in the historical price list and the original requested/not_returned fundamentals accounting. Even when all other snapshots succeed, this mixed-scope request remains incomplete and returns failure. A future explicitly approved current-only universe can have its own completion contract; this repair does not silently narrow the denominator to get green CI.

The policy file covers only these reviewed exceptions. Other symbols are not certified current, complete or independently verified by their absence from it. Current snapshot dates use Asia/Kolkata; the policy helper does not fetch historical fundamentals.

## Seven historical price rejections: no invented holiday filter

PR #6 preserved a RELIANCE.NS provider frame from 2006-09-26 inclusive to 2026-09-21 exclusive: 4,939 rows, including 4,932 structurally usable rows and seven missing-OHLC/zero-volume rows. Those figures describe that archived response, not an authoritative exchange calendar or complete history.

| Date | Evidence available in this review | Treatment |
|---|---|---|
| 2010-02-06 | No issuer-level exchange-price reconciliation established | Keep unresolved rejection |
| 2012-01-07 | CSE 2012 index lists a special live-session notice dated January 5 | Keep unresolved; session title is not issuer OHLCV |
| 2012-03-03 | CSE index lists BSE/NSE special-session settlement notice dated March 5 | Keep unresolved; session title is not issuer OHLCV |
| 2012-09-08 | No issuer-level exchange-price reconciliation established | Keep unresolved rejection |
| 2012-11-11 | CSE index labels the special session for GOLD ETF | Keep unresolved; do not turn index title into automated exclusion |
| 2014-03-22 | No issuer-level exchange-price reconciliation established | Keep unresolved rejection |
| 2015-02-28 | No issuer-level exchange-price reconciliation established | Keep unresolved rejection |

Index evidence: https://www.cse-india.com/Cse_notice/notice2012 . Full linked CSE notices could not be retrieved in this review. Index titles are weaker evidence than complete notices plus segment/instrument eligibility. No price, volume, dividend or market-cap value was taken from a search excerpt and inserted into collected data.

No date is automatically excluded; keepna, strict row validation, provider arguments, the original frame and rejected-row evidence are unchanged. No field fill, backfill, price-series splicing, session-calendar override or complete-history claim is introduced.

## Verification scope

The original offline contracts remain unchanged. Added identity tests cover corrected lists, explicit-typo rejection, merger boundary, no successor substitution, no network for a retired standalone snapshot, continued collection of later issuers, preserved incomplete accounting, provider-denial stopping and cleared stale diagnostics.

The existing isolated Yahoo sample accepts only RELIANCE.NS or INFY.NS. Its original manual default stays RELIANCE.NS; repair-branch pushes use INFY.NS. Both the producer and separate-runner consumer use the same explicit identity, five fixed price sessions and a current fundamentals snapshot. A failed live request stays failed: no repeated requests until green. The ordinary production workflow also triggers after changes to these adapters or the reviewed identity policy.

Passing this scoped sample is not full-production or historical coverage acceptance. Legacy exchange endpoints, intermittent provider field omissions, required HDFC archival financials, session reconciliation and production cross-workflow analysis remain tracked in issue #4.
