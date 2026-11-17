#!/usr/bin/env bash
# Stop and remove the development PostgreSQL and its SQL browser (the rows go with the container).
set -uo pipefail
docker rm -f mercury-transcripts-browser mercury-transcripts 2>/dev/null || true
echo "removed mercury-transcripts and mercury-transcripts-browser"
