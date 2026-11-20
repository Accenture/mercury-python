#!/usr/bin/env bash
# Apply the question record's schema to the development PostgreSQL the LLM helper's dev scripts start
# (extensions/llm-helper/dev/postgres-up.sh: the container mercury-transcripts, database mercury, user mercury).
# Safe to repeat: every statement is IF NOT EXISTS / ON CONFLICT DO NOTHING.
#
#   extensions/ask-person/dev/schema-apply.sh
#
# Then run the main flow on the same database, the password from the environment and never from a file:
#
#   mercury-serve extensions/ask-person/ask_person.py -Dask.ticket.key='${ASK_TICKET_KEY}' \
#     -Dask.audit=postgres -Dask.audit.postgres.ssl=false
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
container="${POSTGRES_CONTAINER:-mercury-transcripts}"
docker exec -i "$container" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-mercury}" -d "${POSTGRES_DB:-mercury}" \
  < "${here}/../schema/postgres.sql"
echo "question record schema applied to ${container}"
