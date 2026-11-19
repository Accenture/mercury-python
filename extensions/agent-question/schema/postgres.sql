-- The agent-question app's inbox: the reference schema (PostgreSQL), RFC-0014 in mercury-composable (Part II,
-- the first store-and-forward channel). One table: a row per wait - an approval or a question the main flow sent
-- with its ticket, resume link and expiry - and a row per advice (a status update, an acknowledgement, an outcome).
-- A wait is upserted on (graph_id, correlation_id, turn), so a re-advice for the same turn updates the card; an
-- advice is appended. Every write takes the next value of question_inbox_seq, the change sequence a reconnecting
-- device replays from (GET /questions?since=<seq>; the stream's Last-Event-ID is the same cursor). The app inserts,
-- selects and updates this table and nothing else; rows are never deleted by it - purge.sql is the installation's.
-- Byte-identical in mercury-python and mercury-nodejs (extensions/agent-question/schema/postgres.sql).

CREATE TABLE IF NOT EXISTS question_schema (
  version     INTEGER PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO question_schema (version) VALUES (1) ON CONFLICT DO NOTHING;

CREATE SEQUENCE IF NOT EXISTS question_inbox_seq;

CREATE TABLE IF NOT EXISTS question_inbox (
  id              BIGSERIAL    PRIMARY KEY,
  seq             BIGINT       NOT NULL,                 -- the change sequence a replay reads from
  ticket          TEXT         UNIQUE,                   -- null for an advice
  graph_id        TEXT         NOT NULL,
  correlation_id  TEXT         NOT NULL,
  turn            INTEGER,                               -- null for an advice that follows no turn
  kind            TEXT         NOT NULL CHECK (kind IN ('approval','question','advice')),
  description     TEXT         NOT NULL,
  summary         TEXT         NOT NULL,
  details         TEXT,                                  -- an advice's body
  message         JSONB        NOT NULL,                 -- the message event as received
  link            TEXT,                                  -- null for an advice
  state           TEXT         NOT NULL CHECK (state IN ('open','answered','expired','seen')),
  requested_at    TIMESTAMPTZ  NOT NULL,
  expires_at      TIMESTAMPTZ,                           -- null for an advice
  updated_at      TIMESTAMPTZ  NOT NULL,
  answered_at     TIMESTAMPTZ,
  answered_by     TEXT,
  decisions       JSONB,
  outcome         JSONB                                  -- the reply endpoint's answer: status and body
);
-- waits upsert, advices append
CREATE UNIQUE INDEX IF NOT EXISTS question_inbox_wait ON question_inbox (graph_id, correlation_id, turn)
  WHERE kind <> 'advice';
-- the replay, the open cards, a transaction's history
CREATE INDEX IF NOT EXISTS question_inbox_seq_idx ON question_inbox (seq);
CREATE INDEX IF NOT EXISTS question_inbox_open ON question_inbox (state, requested_at);
CREATE INDEX IF NOT EXISTS question_inbox_case ON question_inbox (correlation_id, requested_at);
