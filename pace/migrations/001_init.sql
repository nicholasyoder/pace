CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    origin TEXT NOT NULL,
    base_branch TEXT NOT NULL DEFAULT 'main',
    merge_mode TEXT NOT NULL DEFAULT 'local' CHECK (merge_mode IN ('local', 'pr')),
    merge_strategy TEXT NOT NULL DEFAULT 'squash' CHECK (merge_strategy IN ('squash', 'merge')),
    plan_ahead INTEGER NOT NULL DEFAULT 3,
    test_ahead INTEGER NOT NULL DEFAULT 2,
    max_concurrent_implementations INTEGER NOT NULL DEFAULT 1,
    model TEXT NOT NULL DEFAULT '',
    extra_instructions TEXT NOT NULL DEFAULT '',
    next_id INTEGER NOT NULL DEFAULT 1,
    paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE items (
    id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    depends_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'backlog',
    state_since TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    branch TEXT,
    plan_base_commit TEXT,
    impl_base_commit TEXT,
    pr_url TEXT,
    stale_json TEXT,
    reverify INTEGER NOT NULL DEFAULT 0,   -- conflicts were resolved after the last test
    summary TEXT NOT NULL DEFAULT '',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX items_project ON items(project_id, position);
CREATE INDEX items_state ON items(state);

CREATE TABLE plans (
    item_id TEXT NOT NULL REFERENCES items(id),
    version INTEGER NOT NULL,
    markdown TEXT NOT NULL,
    affected_files_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, version)
);

CREATE TABLE qa_rounds (
    item_id TEXT NOT NULL REFERENCES items(id),
    round INTEGER NOT NULL,
    questions_json TEXT NOT NULL,
    answers_json TEXT,
    answered_at TEXT,
    PRIMARY KEY (item_id, round)
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    item_id TEXT REFERENCES items(id),
    kind TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',   -- what the prompt was built from; Retry reuses it
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted', 'rate_limited')),
    pid INTEGER,
    session_id TEXT,
    started_at TEXT,
    ended_at TEXT,
    exit_code INTEGER,
    cost_usd REAL,          -- this run's share; the result event's total is per session
    usage_json TEXT,
    output_json TEXT,
    log_path TEXT,
    error TEXT
);
CREATE INDEX runs_item ON runs(item_id, id);

CREATE TABLE proposals (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source_item_id TEXT REFERENCES items(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'dismissed'))
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    item_id TEXT REFERENCES items(id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (actor IN ('system', 'human', 'claude')),
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX events_item ON events(item_id, id);

-- small global state, e.g. the latest rate_limit_info
CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
