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
    integrity_status TEXT NOT NULL,
    copy_mode TEXT NOT NULL
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
    PRIMARY KEY(snapshot_id,source,dataset,universe,period_start,period_end),
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
    locator TEXT,
    affected_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source,dataset,identity,period_start,period_end)
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
