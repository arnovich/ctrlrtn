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
INSERT INTO traces (
    ts, method, path, query, status_code, latency_ms,
    request_headers, request_body, response_headers, response_body
) VALUES (
    1.0, 'POST', '/fixture-v0', '', 200, 2.0,
    '{}', X'7B7D', '{}', X'7B7D'
);
