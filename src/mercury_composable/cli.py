"""
Developer runner: serve a polyglot function module with one command.

    mercury-serve app.py --port 8086
    mercury-serve app.py --config application.yml

The configuration is the ``resources`` folder next to ``app.py`` - never the working
directory - unless ``--config`` names a file.
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
                        help="Configuration file (default: resources/application.yml|yaml|properties "
                             "next to the application file)")
    # -Dkey=value runtime overrides are consumed by AppConfig from sys.argv
    args, _unknown = parser.parse_known_args()
    # argparse Namespace attributes are untyped - pin the types at the boundary
    app_arg: str = args.app
    port_arg: int | None = args.port
    host_arg: str = args.host
    config_arg: str | None = args.config

    from .config import load_config
    app_path = os.path.abspath(app_arg)
    if not os.path.isfile(app_path):
        print(f"Application file not found - {app_path}", file=sys.stderr)
        return 1
    # the resources folder is the one next to the application file, wherever the command runs
    # from; --config names another file. Loaded before logging/server setup so their keys apply.
    load_config(config_arg, app_dir=os.path.dirname(app_path))
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
    platform.run(port=port_arg, host=host_arg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
