CREATE TABLE traces (
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
);
CREATE TABLE experiments (
    experiment_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    use_case_key TEXT NOT NULL,
    candidate_model TEXT NOT NULL,
    split_pct INTEGER NOT NULL,
    status TEXT NOT NULL,
    max_calls_per_task INTEGER NOT NULL,
    max_cost_usd_per_task REAL NOT NULL
);
CREATE TABLE routes (
    use_case_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    previous_model TEXT,
    note TEXT,
    ts REAL NOT NULL
);
INSERT INTO traces (
    ts, method, path, query, status_code, latency_ms,
    request_headers, request_body, response_headers, response_body
) VALUES (
    2.0, 'POST', '/fixture-v1', '', 200, 3.0,
    '{}', X'7B7D', '{}', X'7B7D'
);
INSERT INTO experiments VALUES (
    'exp:fixture-v1', 2.0, 'tag:fixture', 'candidate-v1', 25,
    'running', 10, 1.0
);
INSERT INTO routes VALUES (
    'tag:route-v1', 'model-v1', 'baseline-v1', 'fixture', 2.0
);
