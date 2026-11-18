"""The MCP recipes (extensions/mcp-binding/resources/mcp-recipes.yml) and the digests they pin.

The engines' page "Tools from MCP servers" (RFC-0013, step 5) gives a recipe for each of seven MCP
servers - the server-file entry, the ``bind`` lines and the catalog rows. The entries live in
``mcp-recipes.yml``, which the binding does not load by default, and the input-schema digests of
the six servers that list their tools without a credential are pinned in
``tests/vectors/mcp-recipes.json``, shared byte for byte with the Node.js twin. The tests here load
the recipes file on every run and, when ``MCP_RECIPES_LIVE`` is set, start each server with ``uvx``
or ``npx`` through the binding's own probe and compare the digests, so a recipe that rots is caught
at the digest, as the binding's boot would catch it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_TESTS = Path(__file__).resolve().parent
_BINDING = _TESTS.parent / "extensions" / "mcp-binding" / "mcp_binding.py"
_RECIPES = _BINDING.parent / "resources" / "mcp-recipes.yml"
_DIGESTS = _TESTS / "vectors" / "mcp-recipes.json"
# the Node.js twin pins the same digest: change the file in both packs or in neither
_DIGESTS_SHA256 = "10c70fe179d315cb0932d56cf0bc1e11290eb7c922ff439e5b9e7944df437d53"

# the module name tests/test_mcp_binding.py loads the binding under: one process executes the
# module once, because a second execution would register mermaid.render again with a second function
# object and break that test's identity check (the preload registry is shared)
_MODULE = "mcp_binding_under_test"


def _load_binding() -> Any:
    # typed Any: the module is loaded dynamically from a file path, so its attributes are
    # unknowable statically - Any tells every analyzer to trust the runtime
    loaded = sys.modules.get(_MODULE)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_MODULE, _BINDING)
    assert spec is not None
    assert spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = module
    spec.loader.exec_module(module)
    return module


binding: Any = _load_binding()

DIGESTS: dict[str, Any] = json.loads(_DIGESTS.read_text(encoding="utf-8"))
LIVE = bool(os.environ.get("MCP_RECIPES_LIVE"))
RECIPE_SERVERS = ["fetch", "filesystem", "git", "github", "neo4j", "playwright", "time"]
# the servers that list their tools without a credential - the ones the page pins
CREDENTIAL_FREE = ["git", "time", "fetch", "filesystem", "playwright"]
# a credential the file references but these tests never send gets a placeholder, so the file
# loads; a real value already in the environment (the Neo4j run) is kept
PLACEHOLDERS = {
    "GITHUB_MCP_TOKEN": "not-used-by-this-test",
    "NEO4J_PASSWORD": "not-used-by-this-test",
}


@pytest.fixture
def recipes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    for name, placeholder in PLACEHOLDERS.items():
        monkeypatch.setenv(name, os.environ.get(name) or placeholder)
    monkeypatch.setenv("MCP_GIT_REPOSITORY", str(_TESTS.parent))
    monkeypatch.setenv("MCP_FILESYSTEM_ROOT", str(tmp_path))
    return {spec.name: spec for spec in binding.load_servers(str(_RECIPES))}


def rot(actual: dict[str, str], pinned: dict[str, str]) -> tuple[list[str], list[str]]:
    """The pinned tools a server no longer offers, and the ones whose schema changed."""
    missing = sorted(set(pinned) - set(actual))
    changed = sorted(
        name for name, digest in pinned.items() if name in actual and actual[name] != digest
    )
    return missing, changed


# --- the files, on every run ----------------------------------------------------------------


def test_the_digest_file_is_the_one_the_node_twin_runs() -> None:
    digest = hashlib.sha256(_DIGESTS.read_bytes()).hexdigest()
    assert digest == _DIGESTS_SHA256, "update both packs' pins when the shared file changes"


def test_the_recipes_file_names_the_seven_servers(recipes: dict[str, Any]) -> None:
    assert sorted(recipes) == RECIPE_SERVERS
    for name in ("git", "time", "fetch", "neo4j"):
        assert recipes[name].transport == "stdio" and recipes[name].command == "uvx", name
    for name in ("filesystem", "playwright"):
        assert recipes[name].transport == "stdio" and recipes[name].command == "npx", name
    github = recipes["github"]
    assert github.transport == "http" and github.url.startswith("https://")
    assert github.headers["Authorization"].startswith("Bearer ")
    assert github.headers["X-MCP-Readonly"] == "true"


def test_the_references_resolve_from_the_environment(recipes: dict[str, Any]) -> None:
    # the repository and the root this test set; the defaults where it set nothing
    assert str(_TESTS.parent) in recipes["git"].args
    assert "--read-only" in recipes["filesystem"].args
    assert recipes["time"].args[-1] == "UTC"
    assert recipes["neo4j"].env["NEO4J_URI"] == os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
    assert recipes["neo4j"].env["NEO4J_USERNAME"] == os.environ.get("NEO4J_USERNAME", "neo4j")


def test_every_pinned_server_has_a_recipe_and_tools(recipes: dict[str, Any]) -> None:
    pinned = DIGESTS["servers"]
    assert sorted(pinned) == sorted(CREDENTIAL_FREE + ["neo4j"])
    assert set(pinned) <= set(recipes)
    for name, server in pinned.items():
        assert server["tools"], name
        for tool, digest in server["tools"].items():
            assert len(digest) == 64 and int(digest, 16) >= 0, f"{name}.{tool}"


def test_rot_names_what_is_missing_and_what_changed() -> None:
    pinned = {"a": "1", "b": "2", "c": "3"}
    assert rot({"a": "1", "b": "9", "d": "4"}, pinned) == (["c"], ["b"])
    assert rot(dict(pinned), pinned) == ([], [])


# --- the servers, live (gated) ---------------------------------------------------------------


async def offered_digests(spec: Any) -> dict[str, str]:
    offered = await binding.offered_tools(spec)
    return {tool["name"]: binding.schema_digest(tool["input_schema"]) for tool in offered}


@pytest.mark.skipif(
    not LIVE,
    reason="set MCP_RECIPES_LIVE=1 to start the recipe servers with uvx and npx and check their digests",
)
@pytest.mark.parametrize("name", CREDENTIAL_FREE)
@pytest.mark.asyncio
async def test_live_recipe_digests(name: str, recipes: dict[str, Any]) -> None:
    actual = await offered_digests(recipes[name])
    missing, changed = rot(actual, DIGESTS["servers"][name]["tools"])
    assert not missing and not changed, (
        f"the {name} recipe rotted - missing {missing}, changed {changed}: run describe on it, review "
        "the schema, then re-pin the digest in both packs' vector file and on the page"
    )


@pytest.mark.skipif(
    not (LIVE and os.environ.get("NEO4J_PASSWORD")),
    reason="set MCP_RECIPES_LIVE=1 and NEO4J_PASSWORD, with a database at NEO4J_URI "
    "(e.g. the one extensions/agent-tools/dev/graph-up.sh starts)",
)
@pytest.mark.asyncio
async def test_live_neo4j_recipe_digests(recipes: dict[str, Any]) -> None:
    actual = await offered_digests(recipes["neo4j"])
    missing, changed = rot(actual, DIGESTS["servers"]["neo4j"]["tools"])
    assert not missing and not changed, (
        f"the neo4j recipe rotted - missing {missing}, changed {changed}"
    )
