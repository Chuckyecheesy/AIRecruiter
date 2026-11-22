-- AIRecruiter application schema (specs/04-data-model.md). Idempotent.
-- IDs are ULIDs (text); timestamps ISO-8601 UTC text; JSON columns are text
-- validated by Pydantic in repo.py. repo.py must run `PRAGMA foreign_keys=ON`
-- on every connection (SQLite does not persist it).
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
  id                TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  form              TEXT NOT NULL DEFAULT '{}',
  rubric            TEXT NOT NULL DEFAULT '{}',
  state             TEXT NOT NULL DEFAULT 'DRAFT',
  shortlist_target  INTEGER NOT NULL DEFAULT 3,
  score_threshold   INTEGER NOT NULL DEFAULT 75,
  max_candidates    INTEGER NOT NULL DEFAULT 5,
  permission_policy TEXT NOT NULL DEFAULT '{}',
  agent_thread_id   TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS candidates (
  id               TEXT PRIMARY KEY,
  job_id           TEXT NOT NULL REFERENCES jobs(id),
  ploid_id         TEXT NOT NULL,
  name             TEXT NOT NULL,
  name_key         TEXT NOT NULL,
  state            TEXT NOT NULL DEFAULT 'DISCOVERED',
  prev_state       TEXT,
  score            INTEGER,
  demo_disposition TEXT,
  locked_by        TEXT,
  locked_at        TEXT,
  trace_url        TEXT,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (job_id, ploid_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_job_state ON candidates(job_id, state);

CREATE TABLE IF NOT EXISTS candidate_memory (
  candidate_id   TEXT PRIMARY KEY REFERENCES candidates(id),
  profile        TEXT NOT NULL DEFAULT '{}',
  research_notes TEXT NOT NULL DEFAULT '{}',
  contact        TEXT NOT NULL DEFAULT '{}',
  contact_log    TEXT NOT NULL DEFAULT '[]',
  decisions      TEXT NOT NULL DEFAULT '[]',
  summary        TEXT NOT NULL DEFAULT '',
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS messages (
  id           TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(id),
  job_id       TEXT NOT NULL REFERENCES jobs(id),
  channel      TEXT NOT NULL,           -- email | voice
  direction    TEXT NOT NULL,           -- out | in
  subject      TEXT,
  body         TEXT,
  thread_id    TEXT,
  simulated    INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS interviews (
  id              TEXT PRIMARY KEY,
  candidate_id    TEXT NOT NULL REFERENCES candidates(id),
  job_id          TEXT NOT NULL REFERENCES jobs(id),
  conversation_id TEXT,
  join_url        TEXT,
  expires_at      TEXT,
  questions       TEXT NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | in_progress | completed | expired
  transcript      TEXT,
  duration_s      INTEGER,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS evaluations (
  id             TEXT PRIMARY KEY,
  candidate_id   TEXT NOT NULL REFERENCES candidates(id),
  job_id         TEXT NOT NULL REFERENCES jobs(id),
  score          INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
  criteria       TEXT NOT NULL DEFAULT '[]',
  recommendation TEXT NOT NULL,         -- shortlist | reject | borderline
  summary        TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS approvals (
  id           TEXT PRIMARY KEY,
  job_id       TEXT NOT NULL REFERENCES jobs(id),
  candidate_id TEXT NOT NULL REFERENCES candidates(id),
  action       TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected | expired | executed
  reasoning    TEXT,
  decided_by   TEXT,                            -- ceo | auto
  feedback     TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at   TEXT,
  executed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_job_status ON approvals(job_id, status);

CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id       TEXT NOT NULL REFERENCES jobs(id),
  candidate_id TEXT REFERENCES candidates(id),  -- NULL for job-level events
  type         TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'pending', -- pending | processing | done | failed
  not_before   TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_status_not_before ON events(status, not_before);

-- run_id is deliberately not a foreign key: a tool call may be recorded
-- before/independent of its agent_runs row.
CREATE TABLE IF NOT EXISTS tool_calls (
  id               TEXT PRIMARY KEY,
  run_id           TEXT,
  candidate_id     TEXT REFERENCES candidates(id),
  tool             TEXT NOT NULL,
  action           TEXT NOT NULL,
  idempotency_key  TEXT NOT NULL UNIQUE,
  input            TEXT,
  output           TEXT,
  status           TEXT NOT NULL,               -- ok | error
  error_kind       TEXT,
  attempts         INTEGER NOT NULL DEFAULT 1,
  latency_ms       INTEGER,
  langfuse_span_id TEXT,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id                TEXT PRIMARY KEY,
  job_id            TEXT NOT NULL REFERENCES jobs(id),
  candidate_id      TEXT REFERENCES candidates(id),
  event_id          INTEGER,
  langfuse_trace_id TEXT,
  trace_url         TEXT,
  status            TEXT NOT NULL DEFAULT 'running', -- running | ok | error
  chosen_action     TEXT,
  error             TEXT,
  started_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at          TEXT
);
