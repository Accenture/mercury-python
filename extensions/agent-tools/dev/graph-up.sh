#!/usr/bin/env bash
# The agent's knowledge graph for development (RFC-0013, ruling R11): Neo4j Community on Colima (or
# Docker Desktop), with its own Browser as the viewer. The password comes from the environment and
# is never written to a file.
#
#   GRAPH_PASSWORD=... extensions/agent-tools/dev/graph-up.sh
#
# Then run the tools with the graph on, in the same shell: resources/application.yml reads the user
# from GRAPH_USER (default neo4j) and the password from GRAPH_PASSWORD, and the other keys default to
# this container (bolt://127.0.0.1:7687, the default database):
#
#   mercury-serve extensions/agent-tools/agent_tools.py -Dtools.graph.uri=bolt://127.0.0.1:7687 \
#     -Dtools.graph.write=true
#
# and browse the graph at http://127.0.0.1:7474 (user neo4j, the same password). Neo4j needs a
# password of at least eight characters. The container keeps its data until graph-down.sh removes it.
set -euo pipefail
: "${GRAPH_PASSWORD:?set GRAPH_PASSWORD in the environment - never in a file}"

docker run -d --name mercury-graph -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH="neo4j/${GRAPH_PASSWORD}" \
  neo4j:5-community

echo "Neo4j:   bolt://127.0.0.1:7687, user neo4j (ready in about twenty seconds)"
echo "Browser: http://127.0.0.1:7474"
