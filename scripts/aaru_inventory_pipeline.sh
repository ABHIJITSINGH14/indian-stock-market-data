#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DB="${AARU_MARKET_DB:-$ROOT/data/databases/stock_market.db}"
STATE="${AARU_BACKFILL_STATE:-$ROOT/data/databases/auto-backfill-state.json}"
SCREENER_ROOT="${AARU_SCREENER_ROOT:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/AARU_RawDataLake/CODEX_DIRECT_RUN}"
OUT="${AARU_INVENTORY_OUT:-$ROOT/data/inventory/live}"
DATABASE_URL="${AARU_DATABASE_URL:-sqlite:///$DB}"
SCREENER_COMMAND="${AARU_SCREENER_COMMAND:-$HOME/Downloads/AARU_Resume_Screener.command}"

if [[ ! -f "$DB" ]]; then
  echo "Database not found: $DB" >&2
  exit 2
fi

mkdir -p "$OUT"

python3 scripts/aaru_status.py \
  --database "$DB" \
  --state-file "$STATE" \
  --screener-summary "$SCREENER_ROOT/MANIFESTS/SCREENER_RESUME_V3/summary.json" \
  | tee "$OUT/status.json"

python3 scripts/aaru_inventory.py \
  --profile live \
  --integrity none \
  --query-budget-seconds 5 \
  --database "$DB" \
  --screener-root "$SCREENER_ROOT" \
  --output "$OUT"

python3 scripts/aaru_missing_manifest.py \
  --inventory "$OUT/inventory.json" \
  --output "$OUT/missing_manifest.json"

PLAN_ARGS=(
  --manifest "$OUT/missing_manifest.json"
  --database-url "$DATABASE_URL"
  --reserve-gib 12
  --output "$OUT/download_plan.json"
)
if [[ -x "$SCREENER_COMMAND" ]]; then
  PLAN_ARGS+=(--screener-command "$SCREENER_COMMAND")
fi
python3 scripts/aaru_plan_downloads.py "${PLAN_ARGS[@]}"

python3 - "$OUT" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
inv=json.loads((root/'inventory.json').read_text())
missing=json.loads((root/'missing_manifest.json').read_text())
plan=json.loads((root/'download_plan.json').read_text())
print('\nAARU inventory pipeline complete')
print('inventory:', root/'inventory.json')
print('missing manifest:', root/'missing_manifest.json')
print('download plan:', root/'download_plan.json')
print('status counts:', missing.get('summary', {}).get('item_counts', {}))
print('download jobs:', len(plan.get('jobs', [])))
print('unsupported families:', len(plan.get('unsupported', [])))
print('profile:', inv.get('profile'))
PY
