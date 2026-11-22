-- DEV FALLBACK ONLY (lane C). Transcribed from specs/04-data-model.md so lane C can
-- seed and test before lane A merges api/db/schema.sql. When api/db/schema.sql
-- exists, reset_demo.py and the tests use it instead. Delete this file then.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  form TEXT NOT NULL DEFAULT '{}',
  rubric TEXT,
  state TEXT NOT NULL DEFAULT 'DRAFT',
  shortlist_target INTEGER NOT NULL DEFAULT 3,
  score_threshold INTEGER NOT NULL DEFAULT 75,
  max_candidates INTEGER NOT NULL DEFAULT 5,
  permission_policy TEXT NOT NULL DEFAULT '{}',
  agent_thread_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id),
  ploid_id TEXT NOT NULL,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,
  state TEXT NOT NULL,
  prev_state TEXT,
  score INTEGER,
  demo_disposition TEXT,
  locked_by TEXT,
  locked_at TEXT,
  trace_url TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (job_id, ploid_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_job_state ON candidates(job_id, state);

CREATE TABLE IF NOT EXISTS candidate_memory (
  candidate_id TEXT PRIMARY KEY REFERENCES candidates(id),
  profile TEXT NOT NULL DEFAULT '{}',
  research_notes TEXT NOT NULL DEFAULT '{}',
  contact TEXT NOT NULL DEFAULT '{}',
  contact_log TEXT NOT NULL DEFAULT '[]',
  decisions TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  direction TEXT NOT NULL,
  subject TEXT,
  body TEXT,
  thread_id TEXT,
  simulated INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interviews (
  id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  conversation_id TEXT,
  join_url TEXT,
  expires_at TEXT,
  questions TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  transcript TEXT NOT NULL DEFAULT '[]',
  duration_s INTEGER,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS evaluations (
  id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  score INTEGER NOT NULL,
  criteria TEXT NOT NULL DEFAULT '[]',
  recommendation TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  action TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  reasoning TEXT NOT NULL DEFAULT '',
  decided_by TEXT,
  feedback TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  executed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_job_status ON approvals(job_id, status);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  candidate_id TEXT,
  type TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  not_before TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_status_nb ON events(status, not_before);

CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  candidate_id TEXT,
  tool TEXT,
  action TEXT,
  idempotency_key TEXT UNIQUE,
  input TEXT,
  output TEXT,
  status TEXT,
  error_kind TEXT,
  attempts INTEGER,
  latency_ms INTEGER,
  langfuse_span_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  job_id TEXT,
  candidate_id TEXT,
  event_id INTEGER,
  langfuse_trace_id TEXT,
  trace_url TEXT,
  status TEXT,
  chosen_action TEXT,
  error TEXT,
  started_at TEXT,
  ended_at TEXT
);
