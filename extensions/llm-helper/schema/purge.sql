-- The transcript audit store's purge (RFC-0012, ruling R6): the two statements a purge is, safe to
-- repeat; the turns cascade with their conversation. The helper runs no purge: dev/audit-purge.sh
-- runs this file against the development container by hand, and an installation's operations
-- automate the same statements on their own schedule and policy (a job on expires_at, or
-- transcript_turn partitioned by month with partitions dropped).
-- Byte-identical in mercury-python and mercury-nodejs (extensions/llm-helper/schema/purge.sql).

-- a conversation past the audit retention it was written with (llm.transcript.audit.retention)
DELETE FROM transcript WHERE expires_at < now();

-- a run the loop stopped or abandoned: still open, untouched for longer than the working store's
-- time to live, so its working record is gone and no turn can follow
UPDATE transcript SET status = 'expired' WHERE status = 'open' AND updated_at < now() - interval '7 days';
