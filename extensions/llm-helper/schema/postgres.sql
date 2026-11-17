-- The transcript audit store: the reference schema (PostgreSQL), RFC-0012 in mercury-composable.
--
-- Two kinds of row, so that the natural questions each hit one index: a conversation (one row per
-- reference llm.chat returned) and a turn (one row per model turn). The writer is the LLM helper's
-- transcript.audit function; its statements are idempotent (INSERT ... ON CONFLICT), so a repeated
-- write or a check that re-sends a row cannot duplicate it. The JSON columns carry the provider's
-- format unchanged: request is the array of messages a turn sent, response the array of assistant
-- content blocks it got back, calls the normalized tool calls of a tool_use turn.
--
-- An installation that implements the contract on another database keeps the two tables (or their
-- equivalent), the key semantics, the status values and the three indexes (correlation id, trace id,
-- time). The published specification: docs/guides/transcript-audit-schema.md in mercury-composable.
-- Byte-identical in mercury-python and mercury-nodejs (extensions/llm-helper/schema/postgres.sql).

CREATE TABLE IF NOT EXISTS transcript_schema (
  version     INTEGER PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO transcript_schema (version) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS transcript (
  conversation                CHAR(32)     PRIMARY KEY,
  correlation_id              TEXT,
  graph_id                    TEXT,
  node                        TEXT,
  model                       TEXT         NOT NULL,
  status                      TEXT         NOT NULL CHECK (status IN ('open','done','refusal','cap','expired')),
  turns                       INTEGER      NOT NULL,
  started_at                  TIMESTAMPTZ  NOT NULL,
  updated_at                  TIMESTAMPTZ  NOT NULL,
  ended_at                    TIMESTAMPTZ,
  expires_at                  TIMESTAMPTZ,
  input_tokens                BIGINT       NOT NULL DEFAULT 0,
  output_tokens               BIGINT       NOT NULL DEFAULT 0,
  cache_creation_input_tokens BIGINT       NOT NULL DEFAULT 0,
  cache_read_input_tokens     BIGINT       NOT NULL DEFAULT 0,
  system                      TEXT,
  tools                       JSONB
);
CREATE INDEX IF NOT EXISTS transcript_correlation_id ON transcript (correlation_id);
CREATE INDEX IF NOT EXISTS transcript_started_at     ON transcript (started_at);
CREATE INDEX IF NOT EXISTS transcript_expires_at     ON transcript (expires_at);

CREATE TABLE IF NOT EXISTS transcript_turn (
  conversation                CHAR(32)     NOT NULL REFERENCES transcript (conversation) ON DELETE CASCADE,
  turn                        INTEGER      NOT NULL CHECK (turn >= 1),
  trace_id                    TEXT,
  trace_path                  TEXT,
  correlation_id              TEXT,
  started_at                  TIMESTAMPTZ  NOT NULL,
  ended_at                    TIMESTAMPTZ  NOT NULL,
  stop_reason                 TEXT,
  status                      TEXT         NOT NULL CHECK (status IN ('done','tool_use','refusal','cap')),
  request_id                  TEXT,
  input_tokens                INTEGER      NOT NULL DEFAULT 0,
  output_tokens               INTEGER      NOT NULL DEFAULT 0,
  cache_creation_input_tokens INTEGER      NOT NULL DEFAULT 0,
  cache_read_input_tokens     INTEGER      NOT NULL DEFAULT 0,
  request                     JSONB        NOT NULL,
  response                    JSONB        NOT NULL,
  calls                       JSONB,
  PRIMARY KEY (conversation, turn)
);
CREATE INDEX IF NOT EXISTS transcript_turn_trace_id       ON transcript_turn (trace_id);
CREATE INDEX IF NOT EXISTS transcript_turn_correlation_id ON transcript_turn (correlation_id);
CREATE INDEX IF NOT EXISTS transcript_turn_started_at     ON transcript_turn (started_at);

-- Roles, for an installation (the development container runs as its one user): the helper writes
-- with the least privilege, people and the SQL browser read.
--   CREATE ROLE transcript_writer LOGIN PASSWORD '...';
--   GRANT SELECT, INSERT, UPDATE ON transcript, transcript_turn TO transcript_writer;
--   GRANT SELECT ON transcript_schema TO transcript_writer;
--   CREATE ROLE transcript_reader LOGIN PASSWORD '...';
--   GRANT SELECT ON transcript, transcript_turn, transcript_schema TO transcript_reader;
