"""
Minimalist configuration management, consistent with the Mercury engines.

Same conventions as the Java/Rust AppConfigReader so a polyglot installation
uses one configuration style everywhere:

- Configuration lives in the ``resources`` folder, mirroring the engines:
  ``resources/application.yml`` (or ``.yaml`` / ``.properties``), or an
  explicit path via ``AppConfig(path=...)`` / ``mercury-serve --config``.
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
- ``log.format`` — ``text`` (default) or ``json``.
- ``log.level`` — default INFO; the ``LOG_LEVEL`` environment variable wins,
  mirroring the engines' log4j2 setting.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from typing import Any, List, Optional

import yaml

_REF = re.compile(r"\$\{([^}]+)\}")

DEFAULT_CANDIDATES = [
    "resources/application.yml",
    "resources/application.yaml",
    "resources/application.properties",
]


def _flatten(prefix: str, node: Any, out: dict) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten(key, v, out)
    else:
        out[prefix] = node


def _parse_properties(text: str) -> dict:
    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def parse_d_args(argv: List[str]) -> dict:
    """Extract -Dkey=value runtime overrides (Java/Rust engine syntax)."""
    overrides: dict = {}
    for arg in argv:
        if arg.startswith("-D") and "=" in arg:
            key, _, value = arg[2:].partition("=")
            if key.strip():
                overrides[key.strip()] = value
    return overrides


class AppConfig:
    """Flat, dot-addressed application configuration."""

    def __init__(self, path: Optional[str] = None, argv: Optional[List[str]] = None):
        self._store: dict = {}
        self._overrides: dict = parse_d_args(sys.argv[1:] if argv is None else argv)
        self._source = "none"
        candidates = [path] if path else DEFAULT_CANDIDATES
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                self._load(candidate)
                self._source = candidate
                break
        else:
            if path:
                raise FileNotFoundError(f"Configuration file not found - {path}")

    def _load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if path.endswith((".yml", ".yaml")):
            data = yaml.safe_load(text) or {}
            flat: dict = {}
            _flatten("", data, flat)
            self._store = flat
        else:
            self._store = _parse_properties(text)

    @property
    def source(self) -> str:
        return self._source

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

    def get_property(self, key: str, default: Optional[str] = None) -> Optional[str]:
        value = self.get(key, default)
        return None if value is None else str(value)

    def exists(self, key: str) -> bool:
        return key in self._overrides or key in self._store

    def _substitute(self, value: str, default: Any = None) -> Any:
        match = _REF.fullmatch(value.strip())
        if match:
            resolved = self._resolve_ref(match.group(1))
            return resolved if resolved is not None else default

        def repl(m: "re.Match[str]") -> str:
            resolved = self._resolve_ref(m.group(1))
            return "" if resolved is None else str(resolved)

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
_instance: Optional[AppConfig] = None


def app_config() -> AppConfig:
    """The shared AppConfig singleton (created on first use)."""
    global _instance
    with _lock:
        if _instance is None:
            _instance = AppConfig()
        return _instance


def load_config(path: Optional[str] = None) -> AppConfig:
    """Replace the shared AppConfig (used by the CLI before startup)."""
    global _instance
    with _lock:
        _instance = AppConfig(path)
        return _instance
