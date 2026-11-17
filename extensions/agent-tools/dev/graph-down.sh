#!/usr/bin/env bash
# Stop and remove the development Neo4j (the graph goes with the container).
set -uo pipefail
docker rm -f mercury-graph 2>/dev/null || true
echo "removed mercury-graph"
