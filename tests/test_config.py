"""AppConfig tests: resources/ convention, -D overrides, ${ENV:default} substitution."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mercury_composable import AppConfig


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_resources_next_to_the_application_file(tmp_path: Path):
    app_dir = tmp_path / "app"
    write(str(app_dir / "resources" / "application.yml"),
          "application:\n  name: 'demo-app'\nrest:\n  server:\n    port: 8086\n")
    config = AppConfig(argv=[], app_dir=str(app_dir))
    assert config.source == str(app_dir / "resources" / "application.yml")
    assert config.resources_dir == str(app_dir / "resources")
    assert config.get("application.name") == "demo-app"
    assert config.get("rest.server.port") == 8086


def test_the_working_directory_is_never_searched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # a decoy in the working directory, nothing next to the application file
    write(str(tmp_path / "cwd" / "resources" / "application.yml"), "application:\n  name: 'decoy'\n")
    monkeypatch.chdir(tmp_path / "cwd")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    config = AppConfig(argv=[], app_dir=str(app_dir))
    assert config.source == "none"
    assert config.get("application.name") is None
    assert config.resources_dir == str(app_dir / "resources")  # where a companion file would sit


def test_a_direct_run_anchors_on_the_script_python_ran(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch):
    from mercury_composable import config as config_module
    write(str(tmp_path / "resources" / "application.yml"), "application:\n  name: 'from-script'\n")
    monkeypatch.setattr(config_module, "main_script_dir", lambda: str(tmp_path))
    config = AppConfig(argv=[])
    assert config.source == str(tmp_path / "resources" / "application.yml")
    assert config.get("application.name") == "from-script"


def test_an_interactive_session_has_no_script_and_loads_nothing(monkeypatch: pytest.MonkeyPatch):
    from mercury_composable import config as config_module
    monkeypatch.setattr(config_module, "main_script_dir", lambda: None)
    config = AppConfig(argv=[])
    assert config.source == "none"
    assert config.resources_dir is None


def test_an_explicit_file_keeps_its_companions_beside_it(tmp_path: Path):
    path = str(tmp_path / "conf" / "application.yml")
    write(path, "application:\n  name: 'explicit'\n")
    config = AppConfig(path=path, argv=[])
    assert config.source == path
    assert config.resources_dir == str(tmp_path / "conf")


APP_SCRIPT = """\
from mercury_composable import app_config
print(app_config().get("application.name"), app_config().source)
raise SystemExit(0)  # before any function registers - the configuration is the point
"""


def _app_in_a_decoy_cwd(tmp_path: Path) -> tuple[Path, Path, str]:
    """An application file with its own resources folder, and a working directory holding a decoy."""
    app = tmp_path / "app" / "app.py"
    write(str(app), APP_SCRIPT)
    write(str(tmp_path / "app" / "resources" / "application.yml"), "application:\n  name: 'from-app'\n")
    write(str(tmp_path / "cwd" / "resources" / "application.yml"), "application:\n  name: 'decoy'\n")
    return app, tmp_path / "cwd", str(tmp_path / "app" / "resources" / "application.yml")


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run([sys.executable, *args], cwd=str(cwd), capture_output=True,
                            text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_python_app_py_resolves_next_to_the_script(tmp_path: Path):
    # `python app.py` from a decoy working directory finds the resources folder next to app.py
    app, cwd, expected = _app_in_a_decoy_cwd(tmp_path)
    assert _run([str(app)], cwd) == f"from-app {expected}"


def test_mercury_serve_resolves_next_to_the_application_file(tmp_path: Path):
    # the CLI, run from a decoy working directory, loads the resources folder next to the app
    app, cwd, expected = _app_in_a_decoy_cwd(tmp_path)
    assert _run(["-m", "mercury_composable.cli", str(app)], cwd) == f"from-app {expected}"
    # --config names another file
    other = str(tmp_path / "other.yml")
    write(other, "application:\n  name: 'other'\n")
    assert _run(["-m", "mercury_composable.cli", str(app), "--config", other], cwd) == f"other {other}"


def test_properties_format(tmp_path: Path):
    path = str(tmp_path / "application.properties")
    write(path, "# comment\nrest.server.port=8087\napplication.name=props-app\n")
    config = AppConfig(path=path, argv=[])
    assert config.get("rest.server.port") == "8087"
    assert config.get("application.name") == "props-app"


def test_d_argument_overrides_win(tmp_path: Path):
    path = str(tmp_path / "application.yml")
    write(path, "rest:\n  server:\n    port: 8086\n")
    config = AppConfig(path=path, argv=["-Drest.server.port=9999", "-Dnew.key=live"])
    assert config.get("rest.server.port") == "9999"  # -D wins over the file
    assert config.get("new.key") == "live"


def test_set_is_runtime_override(tmp_path: Path):
    path = str(tmp_path / "application.yml")
    write(path, "some:\n  key: 'original'\n")
    config = AppConfig(path=path, argv=[])
    assert config.get("some.key") == "original"
    config.set("some.key", "changed")
    assert config.get("some.key") == "changed"


def test_env_substitution_with_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = str(tmp_path / "application.yml")
    write(path, "peer:\n  url: 'http://127.0.0.1:${PEER_PORT:8085}/api/event'\n"
                "missing: '${NOT_SET_ANYWHERE}'\n")
    config = AppConfig(path=path, argv=[])
    assert config.get("peer.url") == "http://127.0.0.1:8085/api/event"
    monkeypatch.setenv("PEER_PORT", "9090")
    assert config.get("peer.url") == "http://127.0.0.1:9090/api/event"
    assert config.get("missing") is None  # unresolved whole-value ref -> None
