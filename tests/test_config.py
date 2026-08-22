"""AppConfig tests: resources/ convention, -D overrides, ${ENV:default} substitution."""

import os

from mercury_composable import AppConfig


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_resources_location_convention(tmp_path, monkeypatch):
    write(str(tmp_path / "resources" / "application.yml"),
          "application:\n  name: 'demo-app'\nrest:\n  server:\n    port: 8086\n")
    monkeypatch.chdir(tmp_path)
    config = AppConfig(argv=[])
    assert config.source == "resources/application.yml"
    assert config.get("application.name") == "demo-app"
    assert config.get("rest.server.port") == 8086


def test_properties_format(tmp_path):
    path = str(tmp_path / "application.properties")
    write(path, "# comment\nrest.server.port=8087\napplication.name=props-app\n")
    config = AppConfig(path=path, argv=[])
    assert config.get("rest.server.port") == "8087"
    assert config.get("application.name") == "props-app"


def test_d_argument_overrides_win(tmp_path):
    path = str(tmp_path / "application.yml")
    write(path, "rest:\n  server:\n    port: 8086\n")
    config = AppConfig(path=path, argv=["-Drest.server.port=9999", "-Dnew.key=live"])
    assert config.get("rest.server.port") == "9999"  # -D wins over the file
    assert config.get("new.key") == "live"


def test_set_is_runtime_override(tmp_path):
    path = str(tmp_path / "application.yml")
    write(path, "some:\n  key: 'original'\n")
    config = AppConfig(path=path, argv=[])
    assert config.get("some.key") == "original"
    config.set("some.key", "changed")
    assert config.get("some.key") == "changed"


def test_env_substitution_with_default(tmp_path, monkeypatch):
    path = str(tmp_path / "application.yml")
    write(path, "peer:\n  url: 'http://127.0.0.1:${PEER_PORT:8085}/api/event'\n"
                "missing: '${NOT_SET_ANYWHERE}'\n")
    config = AppConfig(path=path, argv=[])
    assert config.get("peer.url") == "http://127.0.0.1:8085/api/event"
    monkeypatch.setenv("PEER_PORT", "9090")
    assert config.get("peer.url") == "http://127.0.0.1:9090/api/event"
    assert config.get("missing") is None  # unresolved whole-value ref -> None
