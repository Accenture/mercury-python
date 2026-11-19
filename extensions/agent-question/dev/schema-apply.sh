#!/usr/bin/env bash
# Apply the inbox schema to the development PostgreSQL the LLM helper's dev scripts start
# (extensions/llm-helper/dev/postgres-up.sh: the container mercury-transcripts, database mercury, user mercury).
# Safe to repeat: every statement is IF NOT EXISTS / ON CONFLICT DO NOTHING.
#
#   extensions/agent-question/dev/schema-apply.sh
#
# Then run the app on the same database, the password from the environment and never from a file:
#
#   mercury-serve extensions/agent-question/agent_question.py -Dquestion.inbox=postgres \
#     -Dquestion.inbox.postgres.ssl=false
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
container="${POSTGRES_CONTAINER:-mercury-transcripts}"
docker exec -i "$container" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-mercury}" -d "${POSTGRES_DB:-mercury}" \
  < "${here}/../schema/postgres.sql"
echo "inbox schema applied to ${container}"
