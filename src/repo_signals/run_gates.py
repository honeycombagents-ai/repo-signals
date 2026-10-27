"""The gates ``repo-signals run --live`` must pass, checked offline.

Shared by ``run --live`` (which also needs a terminal and a typed "yes") and ``doctor``.
Failures are reported by NAME only (a variable name, a named condition or a code), never a
value. Nothing here makes a network call, runs openssl or imports the GitHub client. The key file
is checked with the same function the run uses to load it (``sheets.auth``), and openssl is
looked for on PATH only when the built-in signer will be used.
"""
import shutil
from typing import NamedTuple

from . import config
from .collector.plan import PlanError, build_plan
from .collector.signal import HUGGINGFACE, SignalError, signal_from_env
from .drafting.base import SENDER_FIELD_VARS, DraftError, sender_from_env
from .drafting.suppression import suppression_from_env
from .providers.registry import UnknownProviderError, default_registry
from .sheets.auth import inspect_credentials

GITHUB_PLAN_GATE = f"github plan within {config.MAX_CALLS}"
SEARCH_CEILING_GATE = "github plan within the search-rate ceiling"
# The preflight header read (with the token), then the header row, existing rows, header write
# and one append: the fewest sheet calls a live run can need (run.SHEETS_CALLS_PER_WRITE; a test
# keeps the two equal).
MIN_SHEETS_CALLS = 6
SHEETS_BUDGET_GATE = f"{config.SHEETS_MAX_CALLS} of at least {MIN_SHEETS_CALLS}"
OPENSSL_GATE = "openssl_not_found"
HF_NOT_IMPLEMENTED_GATE = "huggingface_not_implemented"
HF_PLAN_GATE = f"huggingface plan within {config.HF_MAX_CALLS}"
HF_RATE_GATE = "huggingface plan within the rate ceiling"


class Gates(NamedTuple):
    failures: tuple[str, ...]
    settings: object = None
    signal: object = None
    sender: object = None
    suppression: object = None
    plan: object = None


def check_gates(environ, registry=None, *, signer=None, which=None) -> Gates:
    """Every failing gate by name, in a fixed order, plus what passed (for the run).

    ``signer`` is the custom Signer the run will use, if any: with one, openssl is not needed and
    not looked for. ``which`` finds a program on PATH (``shutil.which`` by default).
    """
    registry = registry if registry is not None else default_registry()
    which = which if which is not None else shutil.which
    try:
        settings = config.load_settings(environ)
    except config.ConfigError as e:
        return Gates((e.var,))
    failures = []
    if settings.dry_run:
        failures.append(config.DRY_RUN)

    signal, signal_code = None, None
    try:
        signal = signal_from_env(environ)
    except SignalError as e:
        signal_code = e.code   # a code-only string, never a path or file content
    if signal is None:
        failures.append(config.SIGNAL_FILE if signal_code is None
                        else f"{config.SIGNAL_FILE} ({signal_code})")
    elif signal.source == HUGGINGFACE:
        failures.append(HF_NOT_IMPLEMENTED_GATE)   # run --live for Hugging Face: step 6b

    sender = None
    try:
        sender = sender_from_env(environ)
    except DraftError as e:
        failures.append(e.var or "sender")
    if sender is not None:
        failures += [var for field, var in SENDER_FIELD_VARS.items()
                     if not getattr(sender, field)]

    try:
        provider = registry.get(settings.provider)
        failures += [var for var in provider.required_env
                     if not str(environ.get(var, "")).strip()]
    except UnknownProviderError:
        failures.append(config.PROVIDER)

    if not settings.sheet_id:
        failures.append(config.SHEET_ID)
    if not settings.google_credentials:
        failures.append(config.GOOGLE_CREDENTIALS)
    else:
        code = inspect_credentials(settings.google_credentials).code
        if code is not None:
            failures.append(f"{config.GOOGLE_CREDENTIALS} file ({code})")
    if signer is None and not which("openssl"):
        failures.append(OPENSSL_GATE)
    if settings.sheets_max_calls < MIN_SHEETS_CALLS:
        failures.append(SHEETS_BUDGET_GATE)

    suppression, plan = None, None
    try:
        suppression = suppression_from_env(environ)
    except DraftError:
        failures.append(config.SUPPRESSION_FILE)
    else:
        if signal is not None and signal.source == HUGGINGFACE:
            try:
                plan = build_plan(signal, suppression, settings.hf_max_calls)
            except PlanError as e:
                failures.append(HF_PLAN_GATE if e.code == "plan_over_budget" else HF_RATE_GATE)
        elif signal is not None:
            try:
                plan = build_plan(signal, suppression, settings.max_calls)
            except PlanError as e:
                failures.append(GITHUB_PLAN_GATE if e.code == "plan_over_budget"
                                else SEARCH_CEILING_GATE)
    return Gates(tuple(failures), settings, signal, sender, suppression, plan)
