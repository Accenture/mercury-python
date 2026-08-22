"""
Developer runner: serve a polyglot function module with one command.

    mercury-serve app.py --port 8086
    mercury-serve app.py --config application.yml
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="mercury-serve",
        description="Serve Mercury Composable polyglot functions over Event over HTTP")
    parser.add_argument("app", help="Python file registering functions with @preload")
    parser.add_argument("--port", type=int, default=None,
                        help="Event API port (default: rest.server.port or 8085)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--config", default=None,
                        help="Configuration file (default: resources/application.yml|properties)")
    # -Dkey=value runtime overrides are consumed by AppConfig from sys.argv
    args, _unknown = parser.parse_known_args()

    from .config import DEFAULT_CANDIDATES, load_config
    app_path = os.path.abspath(args.app)
    config_path = args.config
    if config_path is None and not any(os.path.isfile(c) for c in DEFAULT_CANDIDATES):
        # fall back to a resources folder next to the application file
        app_dir = os.path.dirname(app_path)
        for candidate in DEFAULT_CANDIDATES:
            probe = os.path.join(app_dir, candidate)
            if os.path.isfile(probe):
                config_path = probe
                break
    load_config(config_path)  # before logging/server setup so their keys apply

    if not os.path.isfile(app_path):
        print(f"Application file not found - {app_path}", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("mercury_user_app", app_path)
    if spec is None or spec.loader is None:
        print(f"Unable to load application - {app_path}", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    sys.modules["mercury_user_app"] = module
    spec.loader.exec_module(module)

    from .registry import default_registry
    from .server import platform
    if not default_registry.routes():
        print("No functions registered - use @preload(route=..., instances=...)",
              file=sys.stderr)
        return 1
    platform.run(port=args.port, host=args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
