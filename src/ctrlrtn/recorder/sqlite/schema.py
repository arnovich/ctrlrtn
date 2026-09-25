"""SQLite schema creation and in-place upgrades for the trace store.

Separated from the store so the DDL that defines the database can be read and
changed without scrolling past the reporting queries that consume it. The store
owns queries; this module owns the shape those queries run against.
"""

from __future__ import annotations

import sqlite3

_CREATE = """
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    use_case_key TEXT,
    request_headers TEXT NOT NULL,
    request_body BLOB,
    response_headers TEXT NOT NULL,
    response_body BLOB
)
"""

_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("cache_read_tokens", "INTEGER"),
    ("cache_write_tokens", "INTEGER"),
    ("cost_usd", "REAL"),
    ("task_id", "TEXT"),
    # Live A/B: the arm this call was served under (None when no experiment
    # applied). Set once on the hot path; enrichment never rewrites them.
    ("experiment_id", "TEXT"),
    ("arm", "TEXT"),
    ("served_model", "TEXT"),
    # Marks a router-imposed terminal ("ceiling"/"send_failure") vs a genuine
    # upstream error, so divergence failures are countable apart from infra noise.
    ("terminal_reason", "TEXT"),
    # Immutable transport identity selected by the upstream resolver.
    ("provider", "TEXT"),
    # Pricing policy captured with the request so historical re-enrichment is
    # stable even if the provider's current configuration later changes.
    ("provider_free", "INTEGER NOT NULL DEFAULT 0"),
    ("session_id", "TEXT"),
    # True only when an evidence-approved budget downgrade served this call.
    ("budget_fallback", "INTEGER NOT NULL DEFAULT 0"),
    ("shadow_experiment_id", "TEXT"),
    ("shadow_pair_id", "TEXT"),
    ("shadow_role", "TEXT"),
    ("workflow", "TEXT"),
    ("workflow_version", "TEXT"),
    ("step", "TEXT"),
    ("step_run_id", "TEXT"),
    ("parent_step_run_id", "TEXT"),
    ("dependency_step_run_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ("step_attempt", "INTEGER"),
    ("workflow_identity_error", "TEXT"),
    ("route_rule_scope", "TEXT"),
    ("route_rule_key", "TEXT"),
    ("control_revision", "TEXT"),
)

_WORKFLOW_EVENTS_CREATE = """
CREATE TABLE IF NOT EXISTS workflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL,
    task_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    step TEXT NOT NULL,
    step_run_id TEXT NOT NULL,
    parent_step_run_id TEXT,
    dependency_step_run_ids TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    success INTEGER,
    score REAL,
    error_code TEXT
)
"""

_TOOL_EVENTS_CREATE = """
CREATE TABLE IF NOT EXISTS tool_operation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL,
    task_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    step TEXT NOT NULL,
    step_run_id TEXT NOT NULL,
    parent_step_run_id TEXT,
    dependency_step_run_ids TEXT NOT NULL,
    step_attempt INTEGER NOT NULL,
    operation TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    effect TEXT NOT NULL,
    status TEXT NOT NULL,
    success INTEGER,
    error_code TEXT,
    latency_ms REAL,
    cost_usd REAL
)
"""

_INFERRED_EDGES_CREATE = """
CREATE TABLE IF NOT EXISTS inferred_workflow_edges (
    edge_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    algorithm TEXT NOT NULL,
    task_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    source_step_run_id TEXT NOT NULL,
    target_step_run_id TEXT NOT NULL,
    source_trace_id INTEGER NOT NULL,
    target_trace_id INTEGER NOT NULL,
    evidence_hash TEXT NOT NULL,
    confidence REAL NOT NULL,
    confirmation TEXT NOT NULL
)
"""

_OUTCOMES_CREATE = """
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id TEXT NOT NULL,
    success INTEGER,
    score REAL
)
"""

_EXPERIMENTS_CREATE = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    use_case_key TEXT NOT NULL,
    candidate_model TEXT NOT NULL,
    split_pct INTEGER NOT NULL,
    status TEXT NOT NULL,
    max_calls_per_task INTEGER NOT NULL,
    candidate_provider TEXT,
    workflow TEXT,
    workflow_version TEXT,
    step TEXT
)
"""

_EXPERIMENTS_ONE_RUNNING = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_experiments_one_running
ON experiments (use_case_key) WHERE status = 'running'
"""

_ROUTES_CREATE = """
CREATE TABLE IF NOT EXISTS routes (
    use_case_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    previous_model TEXT,
    note TEXT,
    ts REAL NOT NULL,
    provider TEXT
)
"""

_WORKFLOW_ROUTES_CREATE = """
CREATE TABLE IF NOT EXISTS workflow_routes (
    workflow TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    step TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    provider TEXT,
    note TEXT,
    ts REAL NOT NULL,
    PRIMARY KEY (workflow, workflow_version, step)
)
"""

_WORKFLOW_DEFINITIONS_CREATE = """
CREATE TABLE IF NOT EXISTS workflow_definitions (
    workflow TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    PRIMARY KEY (workflow, workflow_version)
)
"""

_CONTROL_CONFIG_CREATE = """
CREATE TABLE IF NOT EXISTS control_config_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision TEXT NOT NULL,
    source_path TEXT NOT NULL,
    document_sha256 TEXT NOT NULL,
    activated_at REAL NOT NULL
)
"""

_SHADOW_CREATE = """
CREATE TABLE IF NOT EXISTS shadow_experiments (
    shadow_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    use_case_key TEXT NOT NULL,
    candidate_model TEXT NOT NULL,
    sample_pct INTEGER NOT NULL,
    status TEXT NOT NULL,
    candidate_provider TEXT,
    workflow TEXT,
    workflow_version TEXT,
    step TEXT
)
"""

_SHADOW_ONE_RUNNING = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_shadow_one_running
ON shadow_experiments (use_case_key) WHERE status = 'running'
"""

_SHADOW_STATS_CREATE = """
CREATE TABLE IF NOT EXISTS shadow_stats (
    shadow_id TEXT PRIMARY KEY,
    submitted INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    dropped INTEGER NOT NULL DEFAULT 0
)
"""

_SHADOW_EXCLUSIVE_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS shadow_excludes_live_experiment
       BEFORE INSERT ON shadow_experiments
       WHEN NEW.status = 'running' AND EXISTS (
           SELECT 1 FROM experiments
           WHERE use_case_key = NEW.use_case_key AND status = 'running'
       )
       BEGIN SELECT RAISE(ABORT, 'live experiment already running'); END""",
    """CREATE TRIGGER IF NOT EXISTS live_experiment_excludes_shadow
       BEFORE INSERT ON experiments
       WHEN NEW.status = 'running' AND EXISTS (
           SELECT 1 FROM shadow_experiments
           WHERE use_case_key = NEW.use_case_key AND status = 'running'
       )
       BEGIN SELECT RAISE(ABORT, 'shadow experiment already running'); END""",
)

_FALLBACKS_CREATE = """
CREATE TABLE IF NOT EXISTS approved_fallbacks (
    use_case_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    baseline_model TEXT NOT NULL,
    evidence_created REAL NOT NULL,
    approved_at REAL NOT NULL,
    provider TEXT
)
"""

_JOBS_CREATE = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    worker_id TEXT,
    heartbeat_at REAL,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER,
    progress_message TEXT,
    result_json TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0
)
"""

_JOBS_CLAIM_INDEX = """
CREATE INDEX IF NOT EXISTS ix_jobs_claim
ON jobs (status, created_at)
"""

_WORKFLOW_DISCOVERY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_traces_discovery_task "
    "ON traces (workflow, task_id, id)",
    "CREATE INDEX IF NOT EXISTS ix_traces_discovery_last "
    "ON traces (task_id, ts, id)",
    "CREATE INDEX IF NOT EXISTS ix_traces_discovery_provider "
    "ON traces (provider, task_id) WHERE workflow IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_traces_discovery_model "
    "ON traces (served_model, model, task_id) WHERE workflow IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_traces_discovery_experiment "
    "ON traces (experiment_id, arm, task_id) WHERE workflow IS NULL",
)

_TRACES_TIME_INDEX = "CREATE INDEX IF NOT EXISTS ix_traces_ts ON traces (ts)"

_WORKFLOW_DISCOVERY_INVALIDATIONS_CREATE = """
CREATE TABLE IF NOT EXISTS workflow_discovery_invalidations (
    job_id TEXT PRIMARY KEY,
    invalidated_at REAL NOT NULL,
    pruned_traces INTEGER NOT NULL
)
"""


# Order matters only in that tables precede the indexes and triggers over them.
_CREATE_STATEMENTS: tuple[str, ...] = (
    _CREATE,
    _OUTCOMES_CREATE,
    _WORKFLOW_EVENTS_CREATE,
    _TOOL_EVENTS_CREATE,
    _INFERRED_EDGES_CREATE,
    _EXPERIMENTS_CREATE,
    _EXPERIMENTS_ONE_RUNNING,
    _ROUTES_CREATE,
    _WORKFLOW_ROUTES_CREATE,
    _WORKFLOW_DEFINITIONS_CREATE,
    _CONTROL_CONFIG_CREATE,
    _SHADOW_CREATE,
    _SHADOW_ONE_RUNNING,
    _SHADOW_STATS_CREATE,
    *_SHADOW_EXCLUSIVE_TRIGGERS,
    _FALLBACKS_CREATE,
    _JOBS_CREATE,
    _JOBS_CLAIM_INDEX,
    _WORKFLOW_DISCOVERY_INVALIDATIONS_CREATE,
)

# Columns added to tables other than traces after their first release.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("experiments", "candidate_provider", "TEXT"),
    ("experiments", "workflow", "TEXT"),
    ("experiments", "workflow_version", "TEXT"),
    ("experiments", "step", "TEXT"),
    ("routes", "provider", "TEXT"),
    ("shadow_experiments", "workflow", "TEXT"),
    ("shadow_experiments", "workflow_version", "TEXT"),
    ("shadow_experiments", "step", "TEXT"),
)

# Indexes created after the migration pass so they cover added columns too.
_POST_MIGRATION_INDEXES: tuple[str, ...] = (
    _TRACES_TIME_INDEX,
    *_WORKFLOW_DISCOVERY_INDEXES,
)


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """The column names ``table`` currently carries."""
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def has_column(connection: sqlite3.Connection, table: str, name: str) -> bool:
    """Report whether ``table`` already carries ``name``."""
    return name in columns(connection, table)


def add_column(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    decl: str,
    *,
    existing: set[str] | None = None,
) -> None:
    """Add ``name`` to ``table`` when an older database predates it.

    ``existing`` lets a caller upgrading many columns of one table read
    ``PRAGMA table_info`` once instead of once per column.
    """
    present = existing if existing is not None else columns(connection, table)
    if name in present:
        return
    try:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    except sqlite3.OperationalError as exc:
        # Another process upgrading the same file can land the column between
        # our check and our ALTER. Losing that race is success, not failure:
        # the column we wanted now exists. Any other error still propagates.
        if "duplicate column name" not in str(exc).lower():
            raise


# Columns older databases carry that the code no longer reads or writes.
# Dropping them keeps INSERTs that name every column valid on old files.
_DROPPED_COLUMNS: tuple[tuple[str, str], ...] = (
    # Recorded with each experiment and never enforced; removed in 0.2.
    ("experiments", "max_cost_usd_per_task"),
)


def drop_column(connection: sqlite3.Connection, table: str, name: str) -> None:
    """Drop ``name`` from ``table`` when an older database still carries it."""
    if not has_column(connection, table, name):
        return
    try:
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    except sqlite3.OperationalError as exc:
        # Another process upgrading the same file can drop it first.
        if "no such column" not in str(exc).lower():
            raise


def migrate(connection: sqlite3.Connection) -> None:
    """Bring an existing database up to the current column set."""
    trace_columns = columns(connection, "traces")
    for name, decl in _MIGRATIONS:
        add_column(connection, "traces", name, decl, existing=trace_columns)
    for table, name, decl in _ADDED_COLUMNS:
        add_column(connection, table, name, decl)
    for table, name in _DROPPED_COLUMNS:
        drop_column(connection, table, name)


def initialize(connection: sqlite3.Connection) -> None:
    """Create every table, index, and trigger, then upgrade older databases.

    Safe against a fresh, an existing, or a CONCURRENTLY-OPENED database.
    BEGIN IMMEDIATE takes the write lock up front, so two processes starting
    against the same file serialize (honouring busy_timeout) rather than
    interleaving a column check with the other's ALTER. It also makes the
    schema atomic: a failure part-way rolls back instead of leaving half the
    tables durable for the next open to find.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _CREATE_STATEMENTS:
            connection.execute(statement)
        migrate(connection)
        for statement in _POST_MIGRATION_INDEXES:
            connection.execute(statement)
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
