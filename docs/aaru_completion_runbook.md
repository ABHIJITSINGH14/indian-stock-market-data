# AARU market-data completion runbook

This runbook turns the open-ended NSE/BSE/Screener acquisition into a finite,
auditable completion cycle.

## 1. Fast status without scanning SQLite

```bash
python scripts/aaru_status.py \
  --database data/databases/stock_market.db \
  --state-file data/databases/auto-backfill-state.json \
  --screener-summary "$HOME/Library/Mobile Documents/com~apple~CloudDocs/AARU_RawDataLake/CODEX_DIRECT_RUN/MANIFESTS/SCREENER_RESUME_V3/summary.json"
```

This reads file/state metadata only. It does not certify completeness.

## 2. Build a live-safe hard inventory

```bash
python scripts/aaru_inventory.py \
  --profile live \
  --integrity none \
  --query-budget-seconds 5 \
  --database data/databases/stock_market.db \
  --screener-root "$HOME/Library/Mobile Documents/com~apple~CloudDocs/AARU_RawDataLake/CODEX_DIRECT_RUN" \
  --output data/inventory/live
```

Live mode avoids a long pinned transaction, database hashing and integrity
scans. Large-table counts are explicitly approximate/upper-bound observations.

## 3. Generate the finite missing manifest

```bash
python scripts/aaru_missing_manifest.py \
  --inventory data/inventory/live/inventory.json \
  --output data/inventory/live/missing_manifest.json
```

Only `retryable` items are eligible for reacquisition. `not_published`,
`source_unavailable` and `not_applicable` are terminal until new source evidence
appears.

## 4. Compile non-overlapping download jobs

```bash
python scripts/aaru_plan_downloads.py \
  --manifest data/inventory/live/missing_manifest.json \
  --database-url sqlite:///data/databases/stock_market.db \
  --screener-command "$HOME/Downloads/AARU_Resume_Screener.command" \
  --reserve-gib 12 \
  --output data/inventory/live/download_plan.json
```

The generated `.sh` file is sequential by design. All SQLite writers share the
same logical mutex and must not run alongside `auto_backfill.py` or another
writer.

## 5. Execute only after checking the plan

Before running database-writer jobs:

1. stop/checkpoint the unattended auto-backfill service;
2. confirm free space remains above the plan reserve;
3. review unsupported dataset families;
4. execute the generated shell plan sequentially.

Never parallelize NSE/BSE SQLite writers merely to increase apparent speed.
Parallelize source downloads only when writes remain serialized.

## 6. Repeat until no retryable items remain

Re-run steps 2–4 after each targeted pass. Completion means:

- no `retryable` items remain;
- all remaining gaps are explicit terminal or review states;
- required unsupported families have a collector or an evidence-backed
  unavailable status;
- Screener pending/partial/unresolved identities are reconciled;
- prices are reconciled against expected exchange sessions;
- filing indexes and linked documents are separately accounted for.

## 7. Create a small AARU control database during low-space operation

```bash
python scripts/aaru_adapt_database.py \
  --mode metadata \
  --source data/databases/stock_market.db \
  --destination data/databases/aaru_market_control.sqlite \
  --inventory data/inventory/live/inventory.json \
  --missing-manifest data/inventory/live/missing_manifest.json \
  --source-commit-sha "$(git rev-parse HEAD)"
```

This does not duplicate the multi-gigabyte source database.

## 8. Seal the final snapshot

After writers stop and enough space exists:

1. create a consistent SQLite backup;
2. run snapshot inventory with full integrity and hash;
3. create the full AARU candidate database;
4. validate identity, point-in-time knowledge and lineage;
5. export admitted evidence to immutable AARU research-lake generations.

```bash
python scripts/aaru_inventory.py \
  --profile snapshot --integrity full --hash-database \
  --database data/databases/stock_market.snapshot.db \
  --output data/inventory/final

python scripts/aaru_adapt_database.py \
  --mode full \
  --reserve-gib 12 \
  --source data/databases/stock_market.snapshot.db \
  --destination data/databases/aaru_market_candidate.sqlite \
  --inventory data/inventory/final/inventory.json \
  --missing-manifest data/inventory/final/missing_manifest.json \
  --source-commit-sha "$(git rev-parse HEAD)"
```

The candidate remains outside Decide authority until AARU's validation and
promotion gates pass.
