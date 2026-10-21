"""Offline configuration checks.

Prints one line per check: PASS, WARN or FAIL. Lines name environment
variables, never their values. Reads the environment and, when
REPO_SIGNALS_GOOGLE_CREDENTIALS is set, that one file (to check that it parses
and has the expected key names; no value from it is ever printed). Looks for
openssl on PATH without running it. Makes no network call.
Exit status: 0 if nothing FAILs, 1 otherwise.
"""
import os
import shutil
import sys

from . import config
from .budget import HARD_MAX_CALLS
from .providers.registry import UnknownProviderError, default_registry, planned_note
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


def check_dry_run(settings):
    if settings.dry_run:
        return [(PASS, "dry run is on: no paid calls, nothing written")]
    return [(FAIL, "dry run is off, and live mode is not implemented yet")]


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
    check = inspect_credentials(path)
    if check.problem == "unreadable":
        return lines + [(FAIL, f"{var} does not point to a readable file")]
    if check.problem == "too_large":
        lines.append((FAIL, f"{var} file is too large to be a service-account key"))
    elif check.problem == "not_json":
        lines.append((FAIL, f"{var} file does not parse as a JSON object"))
    elif check.problem == "missing_keys":
        lines.append((FAIL, f"{var} file lacks the expected keys: "
                            f"{', '.join(check.missing_keys)}"))
    elif check.problem == "wrong_type":
        lines.append((FAIL, f"{var} file is not a service-account key "
                            f"({google_api.CRED_TYPE} is not {google_api.CRED_TYPE_SERVICE_ACCOUNT})"))
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


def run_checks(environ, registry, settings=None, version_info=None, which=None):
    settings = settings if settings is not None else config.Settings()
    version_info = version_info if version_info is not None else sys.version_info
    which = which if which is not None else shutil.which
    return [
        *check_python(version_info),
        *check_provider(environ, registry, settings),
        *check_max_calls(environ),
        *check_dry_run(settings),
        *check_optional(environ, config.SHEET_ID, config.parse_sheet_id,
                        "needed before results can be written to a Sheet"),
        *check_optional(environ, config.GITHUB_TOKEN, config.parse_github_token,
                        "needed before GitHub signals can be collected"),
        *check_google_credentials(environ),
        *check_openssl(which),
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
