PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS aaru_source_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    source_branch TEXT,
    source_commit_sha TEXT,
    created_at TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    snapshot_size_bytes INTEGER NOT NULL,
    integrity_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aaru_dataset_registry (
    dataset TEXT PRIMARY KEY,
    source_table TEXT,
    aaru_kind TEXT NOT NULL,
    authority TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS aaru_coverage_cells (
    snapshot_id TEXT NOT NULL,
    source TEXT NOT NULL,
    dataset TEXT NOT NULL,
    universe TEXT NOT NULL DEFAULT 'all',
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    expected_count INTEGER,
    present_count INTEGER,
    missing_count INTEGER,
    status TEXT NOT NULL,
    reason TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (
        snapshot_id, source, dataset, universe, period_start, period_end
    ),
    FOREIGN KEY(snapshot_id) REFERENCES aaru_source_snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS aaru_missing_manifest (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    dataset TEXT NOT NULL,
    identity TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_action TEXT,
    reason TEXT,
    UNIQUE(source, dataset, identity, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS aaru_quality_findings (
    id INTEGER PRIMARY KEY,
    severity TEXT NOT NULL,
    dataset TEXT NOT NULL,
    identity TEXT,
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS aaru_instruments AS
SELECT
    s.id AS security_id,
    s.canonical_id,
    s.isin,
    s.name,
    s.active,
    es.exchange,
    es.exchange_symbol,
    es.series,
    es.scrip_code,
    es.active AS listing_active,
    es.source
FROM securities s
JOIN exchange_symbols es ON es.security_id = s.id;

CREATE VIEW IF NOT EXISTS aaru_cash_eod AS
SELECT
    dp.security_id,
    dp.exchange,
    dp.trading_date AS event_time,
    dp.series,
    dp.open,
    dp.high,
    dp.low,
    dp.close,
    dp.last,
    dp.previous_close,
    dp.volume,
    dp.turnover,
    dp.trades,
    dp.deliverable_quantity,
    dp.source,
    dp.created_at AS retrieved_at,
    dp.updated_at
FROM daily_prices dp;

CREATE VIEW IF NOT EXISTS aaru_filing_index AS
SELECT
    f.id AS filing_id,
    f.security_id,
    f.symbol,
    f.exchange,
    f.dataset,
    f.external_id,
    f.filing_date AS knowledge_time,
    f.period_start AS event_period_start,
    f.period_end AS event_period_end,
    f.document_url,
    f.document_sha256,
    f.is_revision,
    f.created_at AS retrieved_at
FROM filings f;

CREATE VIEW IF NOT EXISTS aaru_financial_facts AS
SELECT
    ffi.filing_id,
    ffi.fact_index,
    ffi.security_id,
    ffi.symbol,
    ffi.namespace,
    ffi.concept,
    ffi.context_id,
    ffi.entity_identifier,
    ffi.entity_scheme,
    ffi.period_kind,
    ffi.period_start,
    ffi.period_end,
    ffi.instant,
    ffi.dimensions_json,
    ffi.unit,
    ffi.decimals,
    ffi.precision,
    ffi.nil,
    ffi.value_text,
    ffi.value_numeric
FROM financial_fact_instances ffi;

CREATE VIEW IF NOT EXISTS aaru_financial_metrics AS
SELECT
    filing_id,
    symbol,
    filing_date AS knowledge_time,
    period_end AS event_time,
    metric,
    value,
    source,
    scope,
    derivation
FROM financial_metrics;

CREATE VIEW IF NOT EXISTS aaru_ownership AS
SELECT
    filing_id,
    security_id,
    symbol,
    quarter_end AS event_time,
    promoter_percent,
    fii_percent,
    dii_percent,
    public_percent,
    non_institution_public_percent
FROM shareholding_patterns;
