# AARU Full Market Database — Integration and Completion Design

## Purpose

The `abhijitsingh14-run-stock-market-app` branch is a strong official-source
NSE/BSE acquisition engine. It should become AARU's **exchange-intake and
operational normalization layer**, not AARU's final research authority.

The AARU boundary remains:

```text
raw source custody
    → normalized operational facts
    → point-in-time research evidence
    → Workbench / Decide / Learning
```

A downloaded row is not automatically research-grade evidence. Identity,
knowledge time, source lineage, completeness and quality gates still apply.

## What the branch already provides

- Official NSE/BSE security masters.
- Historical NSE/BSE cash-market bhavcopies.
- Checkpointed, resumable price ingestion.
- NSE and BSE financial-result discovery.
- Linked XML/XBRL document custody.
- Lossless XBRL fact instances and canonical metrics.
- Shareholding, corporate actions, meetings, PIT/SAST and FII/DII activity.
- Source-specific retries, rate controls and unattended macOS orchestration.
- SQLite integrity, transaction and single-writer safeguards.

## What is still outside the completed AARU database

1. Screener standalone/consolidated raw-page reconciliation.
2. Historical delisted, suspended, renamed, merged and demerged universe.
3. NSE/BSE SME membership and listing-lifecycle intervals.
4. Explicit rights-entitlement, partly-paid, ETF/MF, REIT and InvIT classes.
5. Complete historical bulk/block-deal coverage.
6. Complete annual-report and original filing attachment custody.
7. BSE ownership history where the public API is unavailable.
8. Point-in-time knowledge timestamps for every normalized observation.
9. A final coverage matrix proving complete/partial/unavailable/N/A states.
10. AARU-native immutable research-lake generation export.

## Three storage layers

### Bronze — immutable source custody

Preserve original ZIP/CSV/JSON/XML/HTML/PDF bytes, retrieval time, source URL,
content hash, MIME type and permission. Never silently correct source bytes.

### Silver — operational SQLite database

Normalize issuer/security/listing identity and typed observations while
retaining source, event time, filing/knowledge time, retrieval time and revision
lineage.

### Gold — AARU research evidence

Only lineage-valid, point-in-time records that pass the applicable method and
quality gates may enter immutable AARU lake generations. Derived values remain
labelled derived. Incomplete evidence widens uncertainty or causes abstention.

## Canonical identity model

AARU must distinguish:

- issuer
- security
- listing
- exchange alias
- ISIN
- series
- instrument variant
- listing/lifecycle interval

Required instrument classes:

- NSE/BSE mainboard operating equity
- NSE/BSE SME operating equity
- NSE-only / BSE-only / cross-listed
- ETF / mutual fund / REIT / InvIT
- rights entitlement
- partly-paid security
- suspended / delisted / historical

## Completeness contract

Every `source × dataset × universe × period` cell receives one explicit state:

- `complete`
- `partial`
- `source_unavailable`
- `not_published`
- `not_applicable`
- `retryable`
- `blocked`
- `unknown`

Only `retryable` and `blocked` cells are eligible for automatic reacquisition.
Terminal unavailable/N/A states stop endless retries unless new source evidence
appears.

## Safe integration workflow

1. Let active writers checkpoint or stop at a transaction boundary.
2. Create a consistent SQLite backup with the backup API.
3. Run `scripts/aaru_inventory.py` against the live read-only database or backup.
4. Run `scripts/aaru_missing_manifest.py` to produce a finite work queue.
5. Assign non-overlapping collectors only to manifest items marked retryable.
6. Re-run inventory until no retryable cells remain.
7. Run `scripts/aaru_adapt_database.py` to create a separate AARU candidate DB.
8. Validate identity, timing, lineage and coverage.
9. Export admitted evidence into AARU immutable generations.
10. Keep source databases and AARU recommendation authority separate.

## Commands

```bash
python scripts/aaru_inventory.py \
  --database data/databases/stock_market.db \
  --screener-root "$HOME/Library/Mobile Documents/com~apple~CloudDocs/AARU_RawDataLake/CODEX_DIRECT_RUN" \
  --output data/inventory

python scripts/aaru_missing_manifest.py \
  --inventory data/inventory/inventory.json \
  --output data/inventory/missing_manifest.json

python scripts/aaru_adapt_database.py \
  --source data/databases/stock_market.db \
  --destination data/databases/aaru_market_candidate.sqlite \
  --inventory data/inventory/inventory.json \
  --missing-manifest data/inventory/missing_manifest.json \
  --source-commit-sha "$(git rev-parse HEAD)"
```

Use `--retain-source-snapshot` only when enough storage exists to retain both a
sealed source snapshot and the adapted candidate database.

## Source-table mapping

| Source table | AARU dataset |
|---|---|
| `securities` + `exchange_symbols` | `instrumentMaster` |
| `daily_prices` | `cashEOD` |
| `corporate_actions` | `corporateActions` |
| `filings` + `raw_documents` | `exchangeFilings` / `webDocuments` |
| `financial_fact_instances` | `financialStatements` |
| `financial_metrics` | deterministic derived/reported evidence |
| `shareholding_patterns` | `shareholding` |
| `pit_disclosures` | `insiderDisclosures` |
| `sast_disclosures` | ownership/corporate-deal event |
| `institutional_activity` | `institutionalFlows` |
| checkpoint tables | completeness ledger only |

## Promotion gate

The candidate database must not enter Decide until:

- SQLite integrity and foreign keys pass.
- A consistent snapshot hash and source commit are sealed.
- Identity conflicts are resolved or quarantined.
- Event time, knowledge time and retrieval time are valid.
- The completeness matrix is generated.
- Unsupported gaps are explicit.
- AARU package tests, Xcode builds and repository validators pass.
