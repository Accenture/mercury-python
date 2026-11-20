-- The question record: the reference schema (PostgreSQL), RFC-0014 round 4 in mercury-composable (section 21.2) -
-- the questions, answers and advices of ask-a-person as part of the transcript audit log, beside the transcript
-- tables of the LLM helper (extensions/llm-helper/schema/postgres.sql; the published specification is
-- docs/guides/transcript-audit-schema.md in mercury-composable).
--
-- The main flow records and the channels deliver: the writer is the ask-person extension's question.audit function -
-- every wait (an approval or a question) and every advice person.ask sends, whatever channel delivers it, and every
-- answer POST /ask/reply brings back, with who decided, when, and the engine's outcome. A wait is upserted on
-- (graph_id, correlation_id, turn) - a re-advice for the same turn updates the row, an older one never overwrites a
-- newer - and an advice is appended. The answer applies while the row is open or when the engine accepted it: the
-- first accepted decision stands. Every write takes the next value of question_seq, the change sequence a feed reads
-- from (the agent-question app's GET /questions?since=<seq> and its stream). The writer inserts and updates this table
-- and nothing else; rows are never deleted by it - purge.sql is the installation's. An advice has no state of its own
-- (NULL): whether a person has read it is the device's, kept by the UI.
--
-- A question joins its transcript by correlation_id and graph_id, which the transcript table carries. A flow that asks a
-- person at a stage records the same shape: its id as graph_id, the stage as turn.
--
-- An installation that implements the contract on another database keeps the table (or its equivalent), the key
-- semantics, the state values and the indexes. Byte-identical in mercury-python and mercury-nodejs
-- (extensions/ask-person/schema/postgres.sql). Version 2 of the question schema: version 1 was the agent-question
-- app's own inbox (question_inbox), retired by round 4.

CREATE TABLE IF NOT EXISTS question_schema (
  version     INTEGER PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO question_schema (version) VALUES (2) ON CONFLICT DO NOTHING;

CREATE SEQUENCE IF NOT EXISTS question_seq;

CREATE TABLE IF NOT EXISTS question (
  id              BIGSERIAL    PRIMARY KEY,
  seq             BIGINT       NOT NULL,                 -- the change sequence a feed reads from
  ticket          TEXT         UNIQUE,                   -- null for an advice
  graph_id        TEXT         NOT NULL,                 -- a graph's id, or a flow's
  correlation_id  TEXT         NOT NULL,                 -- the transaction: joins the transcript tables
  turn            INTEGER,                               -- a graph's turn or a flow's stage; null for an advice that follows none
  kind            TEXT         NOT NULL CHECK (kind IN ('approval','question','advice')),
  description     TEXT         NOT NULL,
  summary         TEXT         NOT NULL,
  details         TEXT,                                  -- an advice's body
  message         JSONB        NOT NULL,                 -- the message event as sent: the audit copy
  link            TEXT,                                  -- null for an advice
  state           TEXT         CHECK (state IN ('open','answered','expired')),  -- null for an advice
  requested_at    TIMESTAMPTZ  NOT NULL,
  expires_at      TIMESTAMPTZ,                           -- null for an advice
  updated_at      TIMESTAMPTZ  NOT NULL,
  answered_at     TIMESTAMPTZ,
  answered_by     TEXT,
  decisions       JSONB,
  outcome         JSONB                                  -- the engine's answer to the resume: status and body
);
-- waits upsert, advices append
CREATE UNIQUE INDEX IF NOT EXISTS question_wait ON question (graph_id, correlation_id, turn)
  WHERE kind <> 'advice';
-- the feed, the open waits, a case's history
CREATE INDEX IF NOT EXISTS question_seq_idx ON question (seq);
CREATE INDEX IF NOT EXISTS question_open ON question (state, requested_at);
CREATE INDEX IF NOT EXISTS question_case ON question (correlation_id, requested_at);

-- Roles, for an installation (the development container runs as its one user): the main flow writes with the least
-- privilege, the agent-question app and people read.
--   GRANT SELECT, INSERT, UPDATE ON question TO transcript_writer;
--   GRANT USAGE ON SEQUENCE question_seq, question_id_seq TO transcript_writer;
--   GRANT SELECT ON question, question_schema TO transcript_reader;
