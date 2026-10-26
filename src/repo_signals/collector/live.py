"""The live switch: ``repo-signals probe-org <login>`` and ``repo-signals collect --live``.

probe-org makes exactly ONE GitHub request (an org lookup) so the unverified API details can be
checked before any collection. It prints only code-only fields, never the website text.

collect --live refuses, with no request, unless dry run is off, the signal file is set and
parses, the plan fits the budget and the search-rate ceiling, and stdin and stdout are a
terminal. It then prints the plan and asks the user to type exactly "yes". There is no flag to
skip the prompt. Only after "yes" is the real client built. Results go to stdout only: nothing
is written to disk and nothing is sent anywhere else.
"""
import re
import sys

from .. import config
from ..clock import utc_today
from ..budget import CallBudget
from ..drafting.base import DraftError
from ..drafting.suppression import suppression_from_env
from ..providers.base import ProviderError
from .github_api import GitHubClient
from .orchestrate import COMPLETE, PARTIAL, REFUSED, collect, summarize
from .plan import SEARCH_RATE_CEILING_PER_MINUTE, PlanError, build_plan
from .signal import HUGGINGFACE, SignalError, _LOGIN_RE, signal_from_env

CONFIRM_WORD = "yes"
HF_NOT_IMPLEMENTED = "huggingface_not_implemented"   # no Hugging Face client exists yet
EXIT_CODES = {COMPLETE: 0, PARTIAL: 3, REFUSED: 4}
EXIT_NOT_CONFIRMED = 1
EXIT_REFUSED_BEFORE_START = 2
_TYPE_RE = re.compile(r"[A-Za-z]{1,32}")


def _settings_and_token(environ, prefix, err):
    try:
        settings = config.load_settings(environ)
    except config.ConfigError as e:
        print(f"{prefix}: refused: {e}", file=err)
        return None, None
    if settings.dry_run:
        print(f"{prefix}: refused: dry run is on (set {config.DRY_RUN}=false to allow live "
              "requests); nothing requested", file=err)
        return None, None
    return settings, settings.github_token


def run_probe(login, environ, out=None, err=None, http=None) -> int:
    """One org lookup, code-only output. Exit 0 ok, 1 on a request error, 2 if refused."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if not isinstance(login, str) or not _LOGIN_RE.fullmatch(login):
        print("probe-org: refused: the login is not a valid organisation login shape; nothing "
              "requested", file=err)
        return EXIT_REFUSED_BEFORE_START
    settings, token = _settings_and_token(environ, "probe-org", err)
    if settings is None:
        return EXIT_REFUSED_BEFORE_START
    client = GitHubClient(http, CallBudget(1), token, dry_run=False)
    outcome, org = "ok", None
    try:
        org = client.get_org(login)
    except ProviderError as e:
        outcome = e.code
    print("probe-org: login shape ok", file=out)
    if org is not None:
        kind = org.get("type")
        kind = kind if isinstance(kind, str) and _TYPE_RE.fullmatch(kind) else "unrecognised"
        blog = org.get("blog")
        listed = "yes" if isinstance(blog, str) and blog.strip() else "no"
        verified = {True: "yes", False: "no"}.get(org.get("is_verified"), "unknown")
        print(f"type: {kind}", file=out)
        print(f"website listed: {listed}", file=out)   # never the website text itself
        print(f"is_verified: {verified}", file=out)
    remaining, reset = client.rate_limit
    print(f"rate limit remaining: {remaining if remaining is not None else 'unknown'}", file=out)
    print(f"rate limit reset: {reset if reset is not None else 'unknown'}", file=out)
    print(f"outcome: {outcome}", file=out)
    return 0 if outcome == "ok" else 1


def _is_terminal(stream):
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except (OSError, ValueError):
        return False


def run_live(environ, stdin=None, out=None, err=None, http=None, today=None) -> int:
    """Gated live collection. See the module docstring for the checks and exit codes."""
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    settings, token = _settings_and_token(environ, "collect", err)
    if settings is None:
        return EXIT_REFUSED_BEFORE_START
    try:
        signal = signal_from_env(environ)
    except SignalError as e:
        print(f"collect: refused: the signal file is not usable ({e.code}); nothing requested",
              file=err)
        return EXIT_REFUSED_BEFORE_START
    if signal is None:
        print(f"collect: refused: {config.SIGNAL_FILE} is not set; nothing requested", file=err)
        return EXIT_REFUSED_BEFORE_START
    if signal.source == HUGGINGFACE:
        print(f"collect: refused: {HF_NOT_IMPLEMENTED}; nothing requested", file=err)
        return EXIT_REFUSED_BEFORE_START
    try:
        suppression = suppression_from_env(environ)
    except DraftError as e:
        print(f"collect: refused: the suppression list is not usable ({e.code}); nothing "
              "requested", file=err)
        return EXIT_REFUSED_BEFORE_START
    try:
        plan = build_plan(signal, suppression, settings.max_calls)
    except PlanError as e:
        print(f"collect: refused: {e}; nothing requested", file=err)
        return EXIT_REFUSED_BEFORE_START
    if not (_is_terminal(stdin) and _is_terminal(out)):
        print("collect: refused: stdin and stdout must be a terminal (live collection needs a "
              "typed confirmation); nothing requested", file=err)
        return EXIT_REFUSED_BEFORE_START

    print(f"signal: {plan.signal_name}", file=out)
    print(f"targets: {len(signal.targets)} ({plan.targets_planned} planned, "
          f"{len(plan.skipped)} skipped as suppressed)", file=out)
    if plan.skipped:
        print(f"skipped (suppressed): {', '.join(plan.skipped)}", file=out)
    print(f"terms: {len(signal.terms)}", file=out)
    print(f"planned requests: at most {plan.total} (core {plan.core_count}, search "
          f"{plan.search_count}); budget {settings.max_calls}; search ceiling "
          f"{SEARCH_RATE_CEILING_PER_MINUTE} per minute (unverified)", file=out)
    print(f'Type "{CONFIRM_WORD}" to make these GitHub requests: ', end="", file=out)
    out.flush()
    try:
        answer = stdin.readline()
    except (OSError, ValueError, KeyboardInterrupt):
        answer = ""
    if answer.rstrip("\n") != CONFIRM_WORD:
        print("collect: not confirmed; nothing requested", file=err)
        return EXIT_NOT_CONFIRMED

    client = GitHubClient(http, CallBudget(settings.max_calls), token, dry_run=False)
    result = collect(signal, client, suppression, settings.max_calls,
                     today if today is not None else utc_today())
    for line in summarize(result):
        print(line, file=out)
    for domain, items in result.evidence_by_domain.items():
        for evidence in items:
            print(f"evidence for {domain}:", file=out)
            print(f"  summary: {evidence.summary}", file=out)
            print(f"  url: {evidence.url}", file=out)
    return EXIT_CODES[result.run_report.status]
