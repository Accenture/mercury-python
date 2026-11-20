-- The question record's purge: what an installation runs on its own schedule and policy. The main flow never deletes
-- a row - the record is an audit log, and a person who was offline wants to see what happened without them - so an
-- answered or expired wait and every advice stay until this runs. The interval is the installation's retention
-- (30 days here, as the agent-question inbox's purge had it; the transcript tables keep their own).
-- Byte-identical in mercury-python and mercury-nodejs (extensions/ask-person/schema/purge.sql).

-- a wait no longer open, or an advice, that has not changed for the retention
DELETE FROM question WHERE (state IS NULL OR state <> 'open') AND updated_at < now() - interval '30 days';

-- a wait still marked open long after its expiry: the sweeper did not run (the main flow was down) - expire it
UPDATE question SET state = 'expired', seq = nextval('question_seq'), updated_at = now()
  WHERE state = 'open' AND kind <> 'advice' AND expires_at < now() - interval '1 day';
