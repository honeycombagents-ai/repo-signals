"""Command line entry point: ``repo-signals``."""
import argparse

from . import __version__, doctor


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="repo-signals",
        description="Repo Signals (early, dry run only). Nothing is ever sent.")
    ap.add_argument("--version", action="version", version=f"repo-signals {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check the configuration offline")
    d.add_argument("--online", action="store_true", help="online checks (not implemented yet)")
    args = ap.parse_args(argv)
    if args.command == "doctor":
        return doctor.run(online=args.online)
    return 2
