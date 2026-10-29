"""Entry point: ``python3 -m second_scribe.app``: start the local server (Task 31), print the link, open the browser.

``--project PATH`` opens the project folder at PATH, or creates it there when it holds none (Task 32); a refusal
prints the reason and exits 2."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def kit_version() -> str:
    """Read the kit version from ``pyproject.toml`` (no ``tomllib``: it is 3.11+)."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "unknown"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="second_scribe.app",
        description="A local tool that shows what listening added when an AI turns interviews into maps.",
    )
    p.add_argument("--project", metavar="PATH", help="project folder (created if missing)")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default 0: a random free port)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(kit_version())
        return 0
    if not 0 <= args.port <= 65535:
        build_parser().error("--port must be between 0 and 65535")
    from . import strings
    from .api import AppState
    from . import server
    state = AppState()
    if args.project:
        from .project import Project, ProjectError, user_folder
        try:
            state.project = Project.open_or_create(user_folder(str(Path(args.project).expanduser().absolute())))
        except ProjectError as e:
            print(strings.t(f"project.error.{e.code}"), file=sys.stderr)
            return 2
    srv = server.serve(state, args.port, open_browser=not args.no_browser)
    try:
        while srv.thread.is_alive():
            srv.thread.join(0.5)
    except KeyboardInterrupt:
        print(strings.t("server.stopped"), flush=True)
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
