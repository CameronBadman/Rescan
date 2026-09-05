CREATE TABLE users (
  id text PRIMARY KEY,
  subject text NOT NULL UNIQUE,
  created_at integer NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE jobs (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  idempotency_key varchar(128) NOT NULL,
  manifest_hash text NOT NULL,
  status text NOT NULL DEFAULT 'UPLOADING' CHECK (status IN ('UPLOADING','VERIFYING','QUEUED','PROCESSING','SUCCEEDED','PARTIAL_SUCCESS','FAILED','DELETING')),
  verification_generation integer NOT NULL DEFAULT 0,
  verification_token text,
  verification_until integer,
  total integer NOT NULL CHECK (total BETWEEN 1 AND 4000),
  created_at integer NOT NULL DEFAULT (unixepoch()),
  updated_at integer NOT NULL DEFAULT (unixepoch()),
  UNIQUE(user_id, idempotency_key)
);
CREATE INDEX jobs_owner_page ON jobs(user_id, id);
CREATE TABLE documents (
  id text PRIMARY KEY,
  job_id text NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  filename varchar(255) NOT NULL,
  file_index integer NOT NULL DEFAULT 0,
  size_bytes bigint NOT NULL CHECK(size_bytes > 0),
  source_key text NOT NULL UNIQUE,
  source_version text,
  verified_generation integer NOT NULL DEFAULT -1,
  result_key text,
  status text NOT NULL DEFAULT 'UPLOADING' CHECK (status IN ('UPLOADING','QUEUED','PROCESSING','RETRY_WAIT','SUCCEEDED','FAILED')),
  attempts integer NOT NULL DEFAULT 0,
  token text,
  lease_until integer,
  available_at integer NOT NULL DEFAULT (unixepoch()),
  last_enqueued_at integer,
  error_code text,
  updated_at integer NOT NULL DEFAULT (unixepoch())
);
CREATE UNIQUE INDEX documents_manifest_index ON documents(job_id,file_index);
CREATE INDEX documents_job ON documents(job_id, id);
CREATE INDEX documents_work ON documents(status, available_at);
CREATE TABLE outbox (
  document_id text PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  available_at integer NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE processing_attempts (
  token text PRIMARY KEY,
  document_id text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  attempt integer NOT NULL,
  started_at integer NOT NULL DEFAULT (unixepoch()),
  finished_at integer,
  error_code text
);
CREATE TABLE controller_state (
  id integer PRIMARY KEY CHECK(id = 1),
  warm_until integer,
  lock_token text,
  lock_until integer,
  empty_since integer
);
INSERT INTO controller_state(id) VALUES(1);

CREATE INDEX jobs_status ON jobs(status,id);
CREATE INDEX documents_leases ON documents(status,lease_until);
CREATE INDEX outbox_due ON outbox(available_at);
CREATE TABLE orphan_results (key text PRIMARY KEY, created_at integer NOT NULL DEFAULT (unixepoch()));
