"""Offline configuration checks.

Prints one line per check: PASS, WARN or FAIL. Lines name environment
variables, never their values. Reads the environment and, when
REPO_SIGNALS_GOOGLE_CREDENTIALS is set, that one file (to check that it parses
and has the expected key names; no value from it is ever printed). When
REPO_SIGNALS_SUPPRESSION_FILE is set, it parses that file and prints counts
only. It parses the built-in drafting templates and, when
REPO_SIGNALS_SIGNAL_FILE is set, the signal file (counts only). Looks for
openssl on PATH without running it. Makes no network call.
Exit status: 0 if nothing FAILs, 1 otherwise.
"""
import os
import shutil
import sys

from . import config
from .budget import HARD_MAX_CALLS
from .providers.registry import UnknownProviderError, default_registry, planned_note
from .collector.signal import SignalError, load_signal
from .drafting.base import DraftError
from .drafting.suppression import load_suppression
from .drafting.template import BUILTIN_TEMPLATES, load_template
from .providers.hunter import TITLES_WARNING
from .run_gates import check_gates
from .sheets import google_api
from .sheets.auth import inspect_credentials

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _is_set(environ, name):
    return environ.get(name, "").strip() != ""


def check_python(version_info):
    v = f"Python {version_info[0]}.{version_info[1]}.{version_info[2]}"
    if tuple(version_info[:2]) >= (3, 12):
        return [(PASS, f"{v} (3.12 or newer required)")]
    return [(FAIL, f"{v}: 3.12 or newer is required")]


def check_provider(environ, registry, settings=None):
    try:
        name = config.parse_provider(environ)
    except config.ConfigError as e:
        return [(FAIL, str(e))]
    try:
        provider = registry.get(name)
    except UnknownProviderError:
        return [(FAIL, f"{config.PROVIDER} does not name a registered provider "
                       f"(registered: {', '.join(registry.names())}{planned_note()})")]
    if _is_set(environ, config.PROVIDER):
        lines = [(PASS, f"{config.PROVIDER} names a registered provider")]
    else:
        lines = [(PASS, f"{config.PROVIDER} not set: using the built-in fake provider")]
    if not provider.required_env:
        lines.append((PASS, "the selected provider needs no environment variables"))
    for var in provider.required_env:
        if _is_set(environ, var):
            lines.append((PASS, f"{var} is set"))
        else:
            lines.append((FAIL, f"{var} is missing (required by the selected provider)"))
    extra = getattr(provider, "doctor_lines", None)
    if callable(extra):
        lines.extend(extra(settings if settings is not None else config.Settings()))
    return lines


def check_max_calls(environ):
    try:
        config.parse_max_calls(environ)
    except config.ConfigError as e:
        return [(FAIL, str(e))]
    if _is_set(environ, config.MAX_CALLS):
        return [(PASS, f"{config.MAX_CALLS} is within the hard bound of {HARD_MAX_CALLS}")]
    return [(PASS, f"{config.MAX_CALLS} not set: using the default of "
                   f"{config.DEFAULT_MAX_CALLS}")]


def check_budget(environ, var, parse, default):
    try:
        parse(environ)
    except config.ConfigError as e:
        return [(FAIL, str(e))]
    if _is_set(environ, var):
        return [(PASS, f"{var} is within the hard bound of {HARD_MAX_CALLS}")]
    return [(PASS, f"{var} not set: using the default of {default}")]


def check_run_gates(environ, registry, which=None):
    """Whether run --live would pass its offline gates, failing gates by NAME only.

    doctor knows only the built-in signer, so a missing openssl is a failing gate here.
    """
    failures = check_gates(environ, registry, which=which).failures
    if failures:
        return [(PASS, f"run --live is not armed; failing gates: {', '.join(failures)}")]
    return [(WARN, "run --live is ARMED: every offline gate passes (it still needs a terminal "
                   "and a typed yes)")]


def check_dry_run(settings, problem=None):
    if problem:
        return [(FAIL, problem)]
    if settings.dry_run:
        return [(PASS, "dry run is on: no paid calls, nothing written")]
    return [(WARN, "dry run is off: probe-org, collect --live and run --live can make GitHub "
                   "requests")]


def check_live_armed(environ, settings):
    """Armed means dry run is off and a signal file is set. Names only, never values."""
    if not settings.dry_run and _is_set(environ, config.SIGNAL_FILE):
        return [(WARN, f"live collection is ARMED (dry run is off and {config.SIGNAL_FILE} is "
                       "set); collect --live still asks for confirmation")]
    return [(PASS, f"live collection is not armed (needs dry run off and {config.SIGNAL_FILE} "
                   "set)")]


def check_optional(environ, var, parse, missing_hint):
    try:
        parse(environ)
    except config.ConfigError as e:
        return [(FAIL, str(e))]
    if _is_set(environ, var):
        return [(PASS, f"{var} is set")]
    return [(WARN, f"{var} is missing ({missing_hint})")]


def check_google_credentials(environ):
    var = config.GOOGLE_CREDENTIALS
    try:
        path = config.parse_google_credentials(environ)
    except config.ConfigError as e:
        return [(FAIL, str(e))]
    if path is None:
        return [(WARN, f"{var} is missing (needed before results can be written to a Sheet)")]
    lines = [(PASS, f"{var} is set")]
    check = inspect_credentials(path)   # the same check the run uses to load the key file
    if check.problem == "unreadable":
        return lines + [(FAIL, f"{var} does not point to a readable file ({check.code})")]
    if check.problem == "too_large":
        lines.append((FAIL, f"{var} file is too large to be a service-account key "
                            f"({check.code})"))
    elif check.problem == "not_json":
        lines.append((FAIL, f"{var} file does not parse as a JSON object ({check.code})"))
    elif check.problem == "missing_keys":
        lines.append((FAIL, f"{var} file lacks the expected keys: "
                            f"{', '.join(check.fields)} ({check.code})"))
    elif check.problem == "wrong_type":
        lines.append((FAIL, f"{var} file is not a service-account key ({google_api.CRED_TYPE} "
                            f"is not {google_api.CRED_TYPE_SERVICE_ACCOUNT}; {check.code})"))
    elif check.problem == "malformed_field":
        lines.append((FAIL, f"{var} file has a malformed field: {', '.join(check.fields)} "
                            f"({check.code})"))
    else:
        lines.append((PASS, f"{var} file parses as JSON with the expected keys "
                            f"({', '.join(google_api.REQUIRED_CRED_KEYS)})"))
    if check.loose_permissions:
        lines.append((WARN, f"{var} file is readable by other users (chmod 600 recommended)"))
    return lines


def check_openssl(which):
    if which("openssl"):
        return [(PASS, "openssl found on PATH (used by the built-in Google signer)")]
    return [(WARN, "openssl not found on PATH (the built-in Google signer needs it; "
                   "you can supply your own Signer)")]


def check_sender(environ):
    lines = []
    for var in config.SENDER_VARS:
        try:
            value = config.parse_sender_var(environ, var)
        except config.ConfigError as e:
            lines.append((FAIL, str(e)))
            continue
        if value is None:
            lines.append((WARN, f"{var} is missing (drafting refuses without it)"))
        else:
            lines.append((PASS, f"{var} is set"))
    return lines


def check_suppression(environ):
    var = config.SUPPRESSION_FILE
    try:
        path = config.parse_suppression_file(environ)
    except config.ConfigError as e:
        return [(FAIL, str(e))]
    if path is None:
        return [(WARN, f"{var} is missing (no suppression list: nobody is excluded from "
                       "drafting)")]
    try:
        listed = load_suppression(path)
    except DraftError as e:
        return [(PASS, f"{var} is set"), (FAIL, f"{var} file is not usable ({e.code})")]
    return [(PASS, f"{var} is set"),
            (PASS, f"{var} file parses: {len(listed.emails)} email(s), "
                   f"{len(listed.domains)} domain(s)")]


def check_templates():
    bad = []
    for name in BUILTIN_TEMPLATES:
        try:
            load_template(name)
        except DraftError:
            bad.append(name)
    if bad:
        return [(FAIL, f"drafting templates missing or invalid: {', '.join(bad)}")]
    return [(PASS, f"drafting templates present and valid ({', '.join(BUILTIN_TEMPLATES)})")]


def check_hunter_titles(environ):
    """WARN when the provider is hunter and the signal file sets titles. Offline; never FAILs:
    a malformed provider or signal file is reported by its own check."""
    try:
        if config.parse_provider(environ) != "hunter":
            return []
        path = config.parse_signal_file(environ)
        titles = load_signal(path).titles if path is not None else ()
    except (config.ConfigError, SignalError):
        return []
    return [(WARN, TITLES_WARNING)] if titles else []


def check_signal_file(environ):
    var = config.SIGNAL_FILE
    try:
        path = config.parse_signal_file(environ)
    except config.ConfigError as e:
        return [(FAIL, str(e))]
    if path is None:
        return [(WARN, f"{var} is missing (needed before signals can be collected)")]
    try:
        signal = load_signal(path)
    except SignalError as e:
        return [(PASS, f"{var} is set"), (FAIL, f"{var} file is not usable ({e.code})")]
    return [(PASS, f"{var} is set"),
            (PASS, f"{var} file parses: {len(signal.terms)} term(s), "
                   f"{len(signal.targets)} target(s)")]


def run_checks(environ, registry, settings=None, version_info=None, which=None):
    dry_run_problem = None
    if settings is None:
        try:
            settings = config.Settings(dry_run=config.parse_dry_run(environ))
        except config.ConfigError as e:
            settings, dry_run_problem = config.Settings(), str(e)
    version_info = version_info if version_info is not None else sys.version_info
    which = which if which is not None else shutil.which
    return [
        *check_python(version_info),
        *check_provider(environ, registry, settings),
        *check_max_calls(environ),
        *check_budget(environ, config.PROVIDER_MAX_CALLS, config.parse_provider_max_calls,
                      config.DEFAULT_PROVIDER_MAX_CALLS),
        *check_budget(environ, config.SHEETS_MAX_CALLS, config.parse_sheets_max_calls,
                      config.DEFAULT_SHEETS_MAX_CALLS),
        *check_dry_run(settings, dry_run_problem),
        *check_live_armed(environ, settings),
        *check_optional(environ, config.SHEET_ID, config.parse_sheet_id,
                        "needed before results can be written to a Sheet"),
        *check_optional(environ, config.GITHUB_TOKEN, config.parse_github_token,
                        "needed before GitHub signals can be collected"),
        *check_google_credentials(environ),
        *check_openssl(which),
        *check_sender(environ),
        *check_suppression(environ),
        *check_templates(),
        *check_signal_file(environ),
        *check_hunter_titles(environ),
        *check_run_gates(environ, registry, which),
    ]


def run(environ=None, registry=None, online=False, out=None, settings=None,
        version_info=None, which=None) -> int:
    environ = os.environ if environ is None else environ
    registry = registry if registry is not None else default_registry()
    out = out if out is not None else sys.stdout
    results = run_checks(environ, registry, settings, version_info, which)
    for status, message in results:
        print(f"{status} {message}", file=out)
    if online:
        print("--online: not implemented yet", file=out)
    counts = {s: sum(1 for r, _ in results if r == s) for s in (PASS, WARN, FAIL)}
    print(f"doctor: {counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail", file=out)
    return 1 if counts[FAIL] else 0
