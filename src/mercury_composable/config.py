"""
Minimalist configuration management, consistent with the Mercury engines.

Same conventions as the Java/Rust AppConfigReader so a polyglot installation
uses one configuration style everywhere:

- Configuration lives in the ``resources`` folder NEXT TO THE APPLICATION
  FILE, mirroring the engines' classpath resources: ``resources/application.yml``
  (or ``.yaml`` / ``.properties``) beside the file ``mercury-serve`` is given,
  or beside the script Python ran (``python app.py``); or an explicit path via
  ``AppConfig(path=...)`` / ``mercury-serve --config``. The working directory
  is never searched, so the configuration an application starts with does not
  depend on where it is launched from.
- Dotted keys over nested YAML (``rest.server.port``).
- ``-Dkey=value`` command-line arguments are runtime parameter overrides,
  checked first on every read — the same syntax as the Java engine's JVM
  system properties and the Rust port's ``-D`` arguments (and the mechanism
  behind the Event Script ``f:setConfig`` plugin). :meth:`AppConfig.set` does
  the same programmatically.
- ``${ENV_VAR:default}`` substitution inside values — the environment variable
  wins, then a base configuration key of that name, then the default. An
  unresolved reference without a default resolves to ``None`` when it is the
  whole value (empty string when embedded), matching the engines.

Well-known keys shared with the engines:

- ``application.name`` — application identity used in logs.
- ``rest.server.port`` — the Event API port (default 8085).
- ``log.format`` — ``text`` (default), ``json`` (pretty-printed) or
  ``compact`` (single-line JSONL), the engines' three presentations.
- ``log.level`` — default INFO; the ``LOG_LEVEL`` environment variable wins,
  mirroring the engines' log4j2 setting.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from typing import Any

import yaml

_REF = re.compile(r"\$\{([^}]+)}")

DEFAULT_CANDIDATES = [
    "resources/application.yml",
    "resources/application.yaml",
    "resources/application.properties",
]

RESOURCES_FOLDER = "resources"


def main_script_dir() -> str | None:
    """The folder of the script Python ran (``python app.py``) - where a direct run
    finds its ``resources`` folder; None in an interactive session, which has no script."""
    main = sys.modules.get("__main__")
    file = getattr(main, "__file__", None)
    return os.path.dirname(os.path.abspath(file)) if file else None


def _flatten(prefix: str, node: Any, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten(key, v, out)
    else:
        out[prefix] = node


def _parse_properties(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def parse_d_args(argv: list[str]) -> dict[str, Any]:
    """Extract -Dkey=value runtime overrides (Java/Rust engine syntax)."""
    overrides: dict[str, Any] = {}
    for arg in argv:
        if arg.startswith("-D") and "=" in arg:
            key, _, value = arg[2:].partition("=")
            if key.strip():
                overrides[key.strip()] = value
    return overrides


class AppConfig:
    """Flat, dot-addressed application configuration."""

    def __init__(self, path: str | None = None, argv: list[str] | None = None,
                 app_dir: str | None = None):
        """``path`` names the configuration file explicitly; otherwise the file is one of
        DEFAULT_CANDIDATES under ``app_dir`` - the application file's folder, defaulting to the
        folder of the script Python ran. The working directory is never searched."""
        self._store: dict[str, Any] = {}
        self._overrides: dict[str, Any] = parse_d_args(sys.argv[1:] if argv is None else argv)
        self._source = "none"
        self._resources_dir: str | None = None
        if path:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"Configuration file not found - {path}")
            self._load(path)
            self._source = path
            self._resources_dir = os.path.dirname(os.path.abspath(path))
            return
        folder = app_dir if app_dir is not None else main_script_dir()
        if folder is None:
            return
        self._resources_dir = os.path.join(folder, RESOURCES_FOLDER)
        for candidate in DEFAULT_CANDIDATES:
            probe = os.path.join(folder, candidate)
            if os.path.isfile(probe):
                self._load(probe)
                self._source = probe
                break

    def _load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if path.endswith((".yml", ".yaml")):
            data = yaml.safe_load(text) or {}
            flat: dict[str, Any] = {}
            _flatten("", data, flat)
            self._store = flat
        else:
            self._store = _parse_properties(text)

    @property
    def source(self) -> str:
        """The configuration file loaded (an explicit path as given, else the absolute path of
        the file found next to the application file), or ``none``."""
        return self._source

    @property
    def resources_dir(self) -> str | None:
        """The ``resources`` folder companion files (``app-log-context.yaml``) share with the
        configuration file - beside an explicit file, else next to the application file;
        None when there is neither."""
        return self._resources_dir

    def set(self, key: str, value: Any) -> None:
        """Runtime override, checked first on every read (f:setConfig analog)."""
        if not key or not str(key).strip():
            raise ValueError("Config key must not be empty")
        self._overrides[str(key)] = value

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        if key in self._store:
            value = self._store[key]
            if isinstance(value, str):
                return self._substitute(value, default)
            return value
        return default

    def get_property(self, key: str, default: str | None = None) -> str | None:
        value = self.get(key, default)
        return None if value is None else str(value)

    def resolve_text(self, value: str) -> Any:
        """Resolve ``${ENV:default}`` substitution in a text value - the same
        rules as configuration values (used by companion config files such as
        app-log-context.yaml)."""
        return self._substitute(value)

    def exists(self, key: str) -> bool:
        return key in self._overrides or key in self._store

    def _substitute(self, value: str, default: Any = None) -> Any:
        match = _REF.fullmatch(value.strip())
        if match:
            resolved = self._resolve_ref(match.group(1))
            return resolved if resolved is not None else default

        def repl(m: re.Match[str]) -> str:
            replacement = self._resolve_ref(m.group(1))
            return "" if replacement is None else str(replacement)

        return _REF.sub(repl, value)

    def _resolve_ref(self, ref: str) -> Any:
        name, sep, fallback = ref.partition(":")
        name = name.strip()
        if name in os.environ:
            return os.environ[name]
        if name in self._store and name not in (None, ""):
            base = self._store[name]
            if isinstance(base, str) and _REF.search(base):
                return self._substitute(base)
            return base
        return fallback if sep else None


_lock = threading.Lock()
_instance: AppConfig | None = None


def app_config() -> AppConfig:
    """The shared AppConfig singleton (created on first use - a direct ``python app.py`` run
    resolves its ``resources`` folder next to the script)."""
    global _instance
    with _lock:
        instance = _instance
        if instance is None:
            instance = AppConfig()
            _instance = instance
        return instance


def load_config(path: str | None = None, app_dir: str | None = None) -> AppConfig:
    """Replace the shared AppConfig (used by the CLI before startup, with the application
    file's folder as ``app_dir``)."""
    global _instance
    with _lock:
        instance = AppConfig(path, app_dir=app_dir)
        _instance = instance
        return instance
