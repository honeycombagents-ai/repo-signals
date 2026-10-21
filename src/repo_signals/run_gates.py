"""The gates ``repo-signals run --live`` must pass, checked offline.

Shared by ``run --live`` (which also needs a terminal and a typed "yes") and ``doctor``.
Failures are reported by NAME only (a variable name or a named condition), never a value.
Nothing here makes a network call or imports the GitHub client.
"""
from typing import NamedTuple

from . import config
from .collector.plan import PlanError, build_plan
from .collector.signal import SignalError, signal_from_env
from .drafting.base import SENDER_FIELD_VARS, DraftError, sender_from_env
from .drafting.suppression import suppression_from_env
from .providers.registry import UnknownProviderError, default_registry

GITHUB_PLAN_GATE = f"github plan within {config.MAX_CALLS}"
SEARCH_CEILING_GATE = "github plan within the search-rate ceiling"


class Gates(NamedTuple):
    failures: tuple[str, ...]
    settings: object = None
    signal: object = None
    sender: object = None
    suppression: object = None
    plan: object = None


def check_gates(environ, registry=None) -> Gates:
    """Every failing gate by name, in a fixed order, plus what passed (for the run)."""
    registry = registry if registry is not None else default_registry()
    try:
        settings = config.load_settings(environ)
    except config.ConfigError as e:
        return Gates((e.var,))
    failures = []
    if settings.dry_run:
        failures.append(config.DRY_RUN)

    signal = None
    try:
        signal = signal_from_env(environ)
    except SignalError:
        pass
    if signal is None:
        failures.append(config.SIGNAL_FILE)

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

    suppression, plan = None, None
    try:
        suppression = suppression_from_env(environ)
    except DraftError:
        failures.append(config.SUPPRESSION_FILE)
    else:
        if signal is not None:
            try:
                plan = build_plan(signal, suppression, settings.max_calls)
            except PlanError as e:
                failures.append(GITHUB_PLAN_GATE if e.code == "plan_over_budget"
                                else SEARCH_CEILING_GATE)
    return Gates(tuple(failures), settings, signal, sender, suppression, plan)
