"""Command line entry point: ``repo-signals``."""
import argparse
import os
import sys

from . import __version__, doctor
from .collector import dry_run as collect


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="repo-signals",
        description="Repo Signals (early; dry run by default). Nothing is ever sent.")
    ap.add_argument("--version", action="version", version=f"repo-signals {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check the configuration offline")
    d.add_argument("--online", action="store_true", help="online checks (not implemented yet)")
    c = sub.add_parser("collect", help="collect GitHub signals (--dry-run or --live)")
    mode = c.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="validate the signal file and print the plan; request nothing")
    mode.add_argument("--live", action="store_true",
                      help="make the planned GitHub requests after a typed confirmation")
    p = sub.add_parser("probe-org", help="one GitHub request: look up one organisation")
    p.add_argument("login", help="the organisation login")
    args = ap.parse_args(argv)
    if args.command == "doctor":
        return doctor.run(online=args.online)
    if args.command == "collect":
        if args.dry_run:
            return collect.run(dry_run=True)
        if args.live:
            from .collector import live  # loaded only for the live path
            return live.run_live(os.environ)
        print("usage: repo-signals collect --dry-run | --live", file=sys.stderr)
        return 2
    if args.command == "probe-org":
        from .collector import live  # loaded only for the live path
        return live.run_probe(args.login, os.environ)
    return 2
