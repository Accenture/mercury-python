#!/usr/bin/env bash
# Run the inbox purge (schema/purge.sql) against the development container by hand; an installation's
# operations automate the same statements on their own schedule.
#
#   extensions/agent-question/dev/inbox-purge.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
container="${POSTGRES_CONTAINER:-mercury-transcripts}"
docker exec -i "$container" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-mercury}" -d "${POSTGRES_DB:-mercury}" \
  < "${here}/../schema/purge.sql"
echo "inbox purged in ${container}"
