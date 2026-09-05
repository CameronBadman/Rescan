CREATE TABLE users (
  id uuid PRIMARY KEY,
  subject text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE jobs (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id),
  idempotency_key varchar(128) NOT NULL,
  manifest_hash text NOT NULL,
  status text NOT NULL DEFAULT 'UPLOADING' CHECK (status IN ('UPLOADING','QUEUED','PROCESSING','SUCCEEDED','PARTIAL_SUCCESS','FAILED','DELETING')),
  total integer NOT NULL CHECK (total BETWEEN 1 AND 4000),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(user_id, idempotency_key)
);
CREATE INDEX jobs_owner_page ON jobs(user_id, id);
CREATE TABLE documents (
  id uuid PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  filename varchar(255) NOT NULL,
  size_bytes bigint NOT NULL CHECK(size_bytes > 0),
  source_key text NOT NULL UNIQUE,
  source_version text,
  result_key text,
  status text NOT NULL DEFAULT 'UPLOADING' CHECK (status IN ('UPLOADING','QUEUED','PROCESSING','RETRY_WAIT','SUCCEEDED','FAILED')),
  attempts integer NOT NULL DEFAULT 0,
  token uuid,
  lease_until timestamptz,
  available_at timestamptz NOT NULL DEFAULT now(),
  last_enqueued_at timestamptz,
  error_code text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX documents_job ON documents(job_id, id);
CREATE INDEX documents_work ON documents(status, available_at);
CREATE TABLE outbox (
  document_id uuid PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  available_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE processing_attempts (
  token uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  attempt integer NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error_code text
);
CREATE TABLE controller_state (
  id integer PRIMARY KEY CHECK(id = 1),
  empty_since timestamptz
);
INSERT INTO controller_state(id) VALUES(1);
