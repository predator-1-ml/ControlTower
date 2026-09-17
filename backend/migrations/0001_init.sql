-- Domain schema for the Control Tower.
--
-- Kept deliberately small: five tables, only the columns the three workflows
-- actually read or write. Anything speculative is a column you have to justify.

-- pgvector is a TRUSTED extension: no shared_preload_libraries, no reboot, no
-- superuser. The widespread belief that it needs a parameter-group change and an
-- RDS restart is wrong, which is why this lives in a migration rather than in
-- Terraform. There is no Terraform resource for it either.
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- customers
CREATE TABLE customers (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    external_ref text        NOT NULL UNIQUE,   -- what an operator types, e.g. CUST-1001
    full_name    text        NOT NULL,
    email        text        NOT NULL,
    date_of_birth date,
    -- Drives the onboarding workflow's verify_identity branch.
    kyc_status   text        NOT NULL DEFAULT 'unverified'
                 CHECK (kyc_status IN ('unverified', 'pending', 'verified', 'failed')),
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------- applications
CREATE TABLE applications (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id  uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    product      text        NOT NULL,
    status       text        NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'submitted', 'approved', 'rejected', 'manual_review')),
    decision_reason text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX applications_customer_idx ON applications (customer_id);

-- ------------------------------------------------------------------- claims
CREATE TABLE claims (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_ref     text        NOT NULL UNIQUE,
    customer_id   uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    claim_type    text        NOT NULL,
    status        text        NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'awaiting_information', 'under_review', 'settled', 'rejected')),
    amount        numeric(12, 2),
    incident_date date,
    -- Which required fields are absent. The claims workflow reads this to decide
    -- between generate_summary and request_information, so it is domain state
    -- rather than something the graph recomputes each run.
    missing_fields jsonb      NOT NULL DEFAULT '[]'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX claims_customer_idx ON claims (customer_id);
-- Supports "does this customer have an ACTIVE claim?", the exact question in the
-- assignment's worked example.
CREATE INDEX claims_active_idx ON claims (customer_id)
    WHERE status IN ('open', 'awaiting_information', 'under_review');

-- --------------------------------------------------------- knowledge_chunks
CREATE TABLE knowledge_chunks (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source     text NOT NULL,        -- document name, cited back to the user
    section    text,
    content    text NOT NULL,
    -- 1024 dimensions = cohere.embed-english-v3.
    --
    -- Chosen after checking what Bedrock actually offers in ap-southeast-1:
    -- Amazon Titan embeddings are NOT available there, only Cohere. Titan V2 is
    -- also 1024, so the column type is unchanged, but the reason is different
    -- and region-dependent.
    --
    -- Changing embedding model means changing this number AND re-embedding
    -- everything. The dimension is part of the column type, so a mismatch is a
    -- hard insert error rather than silent drift, which is the good outcome.
    embedding  vector(1024),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- HNSW, not IVFFlat. IVFFlat trains on populated data, so its centroids go stale
-- as the corpus grows and recall degrades silently; HNSW builds on an empty table
-- and accepts live inserts.
--
-- vector_cosine_ops pairs with the <=> operator. If the opclass and the query
-- operator disagree the planner silently falls back to a sequential scan —
-- correct results, terrible latency, and every test still passes.
--
-- Honest note: below ~50k vectors exact search is 100% recall in single-digit
-- milliseconds, so at demo scale this index is not load-bearing. It is here to
-- show the production shape.
CREATE INDEX knowledge_chunks_embedding_idx
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

-- ------------------------------------------------------------ audit_events
-- One row per node execution. This is what makes a run explainable after the
-- fact, and it is the same trace_id carried by the API and the graph.
CREATE TABLE audit_events (
    id         bigserial PRIMARY KEY,
    trace_id   text        NOT NULL,
    session_id text        NOT NULL,
    workflow   text,
    node       text        NOT NULL,
    tool       text,
    status     text        NOT NULL,
    latency_ms integer,
    detail     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_session_idx ON audit_events (session_id, created_at);
CREATE INDEX audit_events_trace_idx ON audit_events (trace_id);
