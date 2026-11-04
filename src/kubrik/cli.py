import argparse
import sys

from kubrik import __version__

COMMANDS = ["serve", "join", "who", "mcp", "hook", "bridge"]


def _stub(args) -> int:
    print("not implemented")
    return 2


def entry() -> None:
    p = argparse.ArgumentParser(prog="kubrik", description="A team hub for coding agents")
    p.add_argument("--version", action="version", version=f"kubrik {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        sub.add_parser(name, help=f"{name} (not implemented yet)").set_defaults(run=_stub)
    args = p.parse_args()
    sys.exit(args.run(args))
