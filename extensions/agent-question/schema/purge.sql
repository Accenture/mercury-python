-- The agent-question inbox's purge: what an installation runs on its own schedule and policy. The app never
-- deletes a row - a person who was offline wants to see what happened without them - so a decided, expired or seen
-- card stays until this runs. The interval is the installation's retention (30 days here).
-- Byte-identical in mercury-python and mercury-nodejs (extensions/agent-question/schema/purge.sql).

-- a card that is no longer open and has not changed for the retention
DELETE FROM question_inbox WHERE state <> 'open' AND updated_at < now() - interval '30 days';

-- a wait still marked open long after its expiry: the sweeper did not run (the app was down) - expire it
UPDATE question_inbox SET state = 'expired', seq = nextval('question_inbox_seq'), updated_at = now()
  WHERE state = 'open' AND kind <> 'advice' AND expires_at < now() - interval '1 day';
