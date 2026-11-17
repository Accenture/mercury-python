#!/usr/bin/env bash
# The transcript audit store for development (RFC-0012, section 9): PostgreSQL 17 with the schema
# applied at first start, and pgweb as the SQL browser. Colima (or Docker Desktop) must be running;
# the password comes from the environment and is never written to a file.
#
#   POSTGRES_PASSWORD=... extensions/llm-helper/dev/postgres-up.sh
#
# Then run the helper with the audit on, in the same shell: resources/application.yml reads the user
# and the password from POSTGRES_USER (default mercury) and POSTGRES_PASSWORD, and the other
# connection keys default to this container (127.0.0.1, 5432, database mercury):
#
#   mercury-serve extensions/llm-helper/llm_helper.py -Dllm.transcript.store=redis \
#     -Dllm.transcript.audit=postgres -Dllm.transcript.audit.postgres.ssl=false
#
# and browse the rows at http://127.0.0.1:8081 - by correlation id, by trace id, by time range.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in the environment - never in a file}"

docker run -d --name mercury-transcripts -p 5432:5432 \
  -e POSTGRES_DB=mercury -e POSTGRES_USER=mercury -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  -v "${here}/../schema/postgres.sql:/docker-entrypoint-initdb.d/transcript.sql:ro" \
  postgres:17-alpine

docker run -d --name mercury-transcripts-browser -p 8081:8081 \
  sosedoff/pgweb --url "postgres://mercury:${POSTGRES_PASSWORD}@host.docker.internal:5432/mercury?sslmode=disable"

echo "PostgreSQL: 127.0.0.1:5432, database mercury, user mercury (the schema applies at first start)"
echo "pgweb:      http://127.0.0.1:8081"
