"""``repo-signals collect --dry-run``: validate the signal file and print the request plan.

It requests nothing and prints no URL. Live collection is in ``collector/live.py``; ``collect``
with neither flag prints a usage line and exits 2 (see ``cli.py``).
"""
import os
import sys

from .. import config
from ..drafting.base import DraftError
from ..drafting.suppression import suppression_from_env
from .plan import SEARCH_RATE_CEILING_PER_MINUTE, PlanError, build_plan
from .signal import SignalError, signal_from_env

PREVIEW_REQUESTS = 5


def run(environ=None, out=None, err=None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    environ = os.environ if environ is None else environ
    try:
        budget_cap = config.parse_max_calls(environ)
    except config.ConfigError as e:
        print(f"collect: {e}", file=err)
        return 1
    try:
        signal = signal_from_env(environ)
    except SignalError as e:
        print(f"collect: the signal file is not usable ({e.code})", file=err)
        return 1
    if signal is None:
        print(f"collect: {config.SIGNAL_FILE} is not set", file=err)
        return 1
    try:
        suppression = suppression_from_env(environ)
    except DraftError as e:
        print(f"collect: the suppression list is not usable ({e.code})", file=err)
        return 1
    try:
        plan = build_plan(signal, suppression, budget_cap)
    except PlanError as e:
        print(f"collect: refused: {e}; nothing requested", file=err)
        return 1
    print("collect: dry run, nothing requested", file=out)
    print(f"signal: {plan.signal_name}", file=out)
    print(f"targets: {len(signal.targets)} ({plan.targets_planned} planned, "
          f"{len(plan.skipped)} skipped as suppressed)", file=out)
    print(f"terms: {len(signal.terms)}", file=out)
    print(f"planned requests: {plan.total} (core {plan.core_count}, search {plan.search_count}); "
          f"budget {budget_cap}; search ceiling {SEARCH_RATE_CEILING_PER_MINUTE} per minute "
          "(unverified)", file=out)
    if plan.skipped:
        print(f"skipped (suppressed): {', '.join(plan.skipped)}", file=out)
    if plan.requests:
        shown = min(PREVIEW_REQUESTS, plan.total)
        print(f"first {shown} planned request(s):", file=out)
        for i, request in enumerate(plan.requests[:PREVIEW_REQUESTS], 1):
            print(f"  {i}. {request.describe()}", file=out)
    return 0
