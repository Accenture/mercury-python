#!/usr/bin/env bash
# The sample purge of the transcript audit store (RFC-0012, ruling R6): runs schema/purge.sql inside
# the development container with its psql, since none is installed on the host. An installation's
# operations automate the same two statements on their own schedule and policy; this script is the
# developer's by-hand version.
#
#   extensions/llm-helper/dev/audit-purge.sh                      the two statements of purge.sql
#   extensions/llm-helper/dev/audit-purge.sh --dry-run            count what would go, change nothing
#   extensions/llm-helper/dev/audit-purge.sh --before 2026-10-01  also the conversations started before
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dry_run=0
before=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --before) before="${2:?--before needs an ISO date}"; shift ;;
    *) echo "usage: $0 [--dry-run] [--before <ISO date>]" >&2; exit 2 ;;
  esac
  shift
done

# statements go in on stdin, where psql interpolates :'before' (it does not for -c)
run_psql() {
  docker exec -i mercury-transcripts psql -U mercury -d mercury -v ON_ERROR_STOP=1 -v before="${before}" "$@"
}

if [ "${dry_run}" = 1 ]; then
  run_psql <<'SQL'
SELECT count(*) AS conversations_past_retention FROM transcript WHERE expires_at < now();
SELECT count(*) AS open_conversations_to_mark_expired FROM transcript
  WHERE status = 'open' AND updated_at < now() - interval '7 days';
SQL
  if [ -n "${before}" ]; then
    run_psql <<'SQL'
SELECT count(*) AS conversations_started_before FROM transcript WHERE started_at < :'before';
SQL
  fi
  exit 0
fi

run_psql < "${here}/../schema/purge.sql"
if [ -n "${before}" ]; then
  run_psql <<'SQL'
DELETE FROM transcript WHERE started_at < :'before';
SQL
fi
echo "purge done"
