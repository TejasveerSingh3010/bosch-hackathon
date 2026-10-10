"""Build the dependency map: `python -m mapper`

    python -m mapper                          write results/graph.json + docs/dependency_graph.png
    python -m mapper --compose path/to/file   use a different compose file
    python -m mapper --print                  print the ranking without writing files
    python -m mapper --no-png                 skip the picture (no matplotlib needed)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compose import parse_compose
from .graph import DependencyGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMPOSE = REPO_ROOT / "app" / "docker-compose.yml"
DEFAULT_JSON = REPO_ROOT / "results" / "graph.json"
DEFAULT_PNG = REPO_ROOT / "docs" / "dependency_graph.png"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mapper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="where to write graph.json")
    parser.add_argument("--png", type=Path, default=DEFAULT_PNG, help="where to write the picture")
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print the ranking and write nothing")
    parser.add_argument("--no-png", action="store_true", help="do not render the picture")
    args = parser.parse_args(argv)

    try:
        parsed = parse_compose(args.compose)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    graph = DependencyGraph(parsed)
    print(graph.to_text())

    if args.print_only:
        return 0

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(graph.to_json(), indent=2), encoding="utf-8")
    print(f"\ngraph written to {args.json}")

    if not args.no_png:
        try:
            from .render import render_png

            path = render_png(graph, args.png)
            print(f"picture written to {path}")
        except ImportError:
            print("matplotlib is not installed, so no picture was written (use --no-png to silence this)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
