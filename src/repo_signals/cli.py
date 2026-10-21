"""Command line entry point: ``repo-signals``."""
import argparse

from . import __version__, doctor
from .collector import dry_run as collect


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="repo-signals",
        description="Repo Signals (early, dry run only). Nothing is ever sent.")
    ap.add_argument("--version", action="version", version=f"repo-signals {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check the configuration offline")
    d.add_argument("--online", action="store_true", help="online checks (not implemented yet)")
    c = sub.add_parser("collect", help="plan a GitHub signal collection (dry run only)")
    c.add_argument("--dry-run", action="store_true",
                   help="validate the signal file and print the plan; request nothing")
    args = ap.parse_args(argv)
    if args.command == "doctor":
        return doctor.run(online=args.online)
    if args.command == "collect":
        return collect.run(dry_run=args.dry_run)
    return 2
