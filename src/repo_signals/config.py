"""Settings from environment variables only, parsed strictly.

    REPO_SIGNALS_PROVIDER      provider name (default: fake)
    REPO_SIGNALS_MAX_CALLS     GitHub call cap: 1 to the hard bound (default: 25)
    REPO_SIGNALS_PROVIDER_MAX_CALLS
                               people-provider call cap for "run": 1 to the hard bound (default: 25)
    REPO_SIGNALS_SHEETS_MAX_CALLS
                               Google Sheets call cap for "run": 1 to the hard bound (default: 10)
    REPO_SIGNALS_HF_MAX_CALLS  Hugging Face call cap for a "huggingface" signal file: 1 to the
                               hard bound (default: 25). Planned only: no client exists yet.
    REPO_SIGNALS_SHEET_ID      Google Sheet id (optional)
    REPO_SIGNALS_GITHUB_TOKEN  GitHub token (optional)
    REPO_SIGNALS_GOOGLE_CREDENTIALS
                               path to a Google service-account JSON key file (optional).
                               A file path only: the JSON itself is refused.
    REPO_SIGNALS_SENDER_NAME, REPO_SIGNALS_SENDER_ROLE, REPO_SIGNALS_ORG_NAME
                               sender details for drafts (single line each, optional)
    REPO_SIGNALS_POSTAL_ADDRESS, REPO_SIGNALS_OPTOUT_TEXT
                               put in every draft (up to 6 lines each, optional; drafting
                               refuses without them)
    REPO_SIGNALS_SUPPRESSION_FILE
                               path to a suppression list: one email or domain per line
    REPO_SIGNALS_SIGNAL_FILE   path to the signal definition (TOML) for the collector
    REPO_SIGNALS_DRY_RUN       "true" (the default) or exactly "false". Dry run is on unless
                               this is "false"; any other value is an error.

An empty variable counts as unset. Surrounding whitespace is an error, not
something to trim. Dry run is on by default. Turning it off only allows the
live commands (probe-org, collect --live, run --live); collect --live and
run --live still ask for confirmation every time, and probe-org does not.
Errors name the variable, never its value.
"""
import os
import re
from dataclasses import dataclass, fields

from .budget import DEFAULT_MAX_CALLS, HARD_MAX_CALLS

PROVIDER = "REPO_SIGNALS_PROVIDER"
MAX_CALLS = "REPO_SIGNALS_MAX_CALLS"
PROVIDER_MAX_CALLS = "REPO_SIGNALS_PROVIDER_MAX_CALLS"
SHEETS_MAX_CALLS = "REPO_SIGNALS_SHEETS_MAX_CALLS"
HF_MAX_CALLS = "REPO_SIGNALS_HF_MAX_CALLS"
DEFAULT_PROVIDER_MAX_CALLS = 25
DEFAULT_SHEETS_MAX_CALLS = 10
DEFAULT_HF_MAX_CALLS = 25
SHEET_ID = "REPO_SIGNALS_SHEET_ID"
GITHUB_TOKEN = "REPO_SIGNALS_GITHUB_TOKEN"
GOOGLE_CREDENTIALS = "REPO_SIGNALS_GOOGLE_CREDENTIALS"
SENDER_NAME = "REPO_SIGNALS_SENDER_NAME"
SENDER_ROLE = "REPO_SIGNALS_SENDER_ROLE"
ORG_NAME = "REPO_SIGNALS_ORG_NAME"
POSTAL_ADDRESS = "REPO_SIGNALS_POSTAL_ADDRESS"
OPTOUT_TEXT = "REPO_SIGNALS_OPTOUT_TEXT"
SENDER_VARS = (SENDER_NAME, SENDER_ROLE, ORG_NAME, POSTAL_ADDRESS, OPTOUT_TEXT)
MULTILINE_SENDER_VARS = (POSTAL_ADDRESS, OPTOUT_TEXT)
SUPPRESSION_FILE = "REPO_SIGNALS_SUPPRESSION_FILE"
SIGNAL_FILE = "REPO_SIGNALS_SIGNAL_FILE"
DRY_RUN = "REPO_SIGNALS_DRY_RUN"
DEFAULT_PROVIDER = "fake"

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_CALLS_RE = re.compile(r"[1-9][0-9]{0,5}")
_SHEET_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,128}")
_TOKEN_RE = re.compile(r"[\x21-\x7e]{20,255}")
_PATH_MAX = 4096
_SINGLE_LINE_MAX = 200
_MULTILINE_MAX = 500
_MULTILINE_LINES = 6


class ConfigError(ValueError):
    def __init__(self, var: str, problem: str):
        self.var = var
        super().__init__(f"{var} {problem}")


def _raw(environ, var):
    value = environ.get(var, "")
    return value if value != "" else None


def parse_provider(environ) -> str:
    raw = _raw(environ, PROVIDER)
    if raw is None:
        return DEFAULT_PROVIDER
    if not _PROVIDER_RE.fullmatch(raw):
        raise ConfigError(PROVIDER, "must be a provider name (lowercase letters, digits, '-', '_')")
    return raw


def _parse_cap(environ, var, default) -> int:
    raw = _raw(environ, var)
    if raw is None:
        return default
    if not _MAX_CALLS_RE.fullmatch(raw) or int(raw) > HARD_MAX_CALLS:
        raise ConfigError(var, f"must be a whole number from 1 to {HARD_MAX_CALLS}")
    return int(raw)


def parse_max_calls(environ) -> int:
    return _parse_cap(environ, MAX_CALLS, DEFAULT_MAX_CALLS)


def parse_provider_max_calls(environ) -> int:
    return _parse_cap(environ, PROVIDER_MAX_CALLS, DEFAULT_PROVIDER_MAX_CALLS)


def parse_sheets_max_calls(environ) -> int:
    return _parse_cap(environ, SHEETS_MAX_CALLS, DEFAULT_SHEETS_MAX_CALLS)


def parse_hf_max_calls(environ) -> int:
    return _parse_cap(environ, HF_MAX_CALLS, DEFAULT_HF_MAX_CALLS)


def parse_sheet_id(environ) -> str | None:
    raw = _raw(environ, SHEET_ID)
    if raw is not None and not _SHEET_ID_RE.fullmatch(raw):
        raise ConfigError(SHEET_ID, "is not a well-formed Google Sheet id")
    return raw


def parse_github_token(environ) -> str | None:
    raw = _raw(environ, GITHUB_TOKEN)
    if raw is not None and not _TOKEN_RE.fullmatch(raw):
        raise ConfigError(GITHUB_TOKEN, "is not a well-formed token")
    return raw


def _parse_path(environ, var, json_hint):
    raw = _raw(environ, var)
    if raw is None:
        return None
    if raw.lstrip().startswith(("{", "[")):
        raise ConfigError(var, json_hint)
    if (len(raw) > _PATH_MAX or raw != raw.strip()
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw)):
        raise ConfigError(var, "is not a well-formed file path")
    return raw


def parse_google_credentials(environ) -> str | None:
    return _parse_path(environ, GOOGLE_CREDENTIALS,
                       "must be a path to the service-account JSON file, not the JSON itself")


def parse_suppression_file(environ) -> str | None:
    return _parse_path(environ, SUPPRESSION_FILE,
                       "must be a path to the suppression list file, not its contents")


def parse_dry_run(environ) -> bool:
    """True unless REPO_SIGNALS_DRY_RUN is exactly "false". Only "true" and "false" are valid."""
    raw = _raw(environ, DRY_RUN)
    if raw is None or raw == "true":
        return True
    if raw == "false":
        return False
    raise ConfigError(DRY_RUN, 'must be "true" or "false"')


def parse_signal_file(environ) -> str | None:
    return _parse_path(environ, SIGNAL_FILE,
                       "must be a path to the signal file, not its contents")


def parse_sender_var(environ, var) -> str | None:
    """One sender detail. Unset or blank gives None; a malformed value is a ConfigError."""
    if var not in SENDER_VARS:
        raise ValueError("not a sender variable")
    raw = _raw(environ, var)
    if raw is None or raw.strip() == "":
        return None
    multiline = var in MULTILINE_SENDER_VARS
    lines = raw.split("\n")
    if (raw != raw.strip() or len(raw) > (_MULTILINE_MAX if multiline else _SINGLE_LINE_MAX)
            or (not multiline and len(lines) > 1) or len(lines) > _MULTILINE_LINES
            or not all(line.isprintable() and line.strip() for line in lines)):
        what = (f"at most {_MULTILINE_LINES} non-blank printable lines" if multiline
                else "one printable line")
        raise ConfigError(var, f"must be {what}")
    return raw


@dataclass(frozen=True, repr=False)
class Settings:
    provider: str = DEFAULT_PROVIDER
    max_calls: int = DEFAULT_MAX_CALLS
    sheet_id: str | None = None
    github_token: str | None = None
    google_credentials: str | None = None
    dry_run: bool = True
    provider_max_calls: int = DEFAULT_PROVIDER_MAX_CALLS
    sheets_max_calls: int = DEFAULT_SHEETS_MAX_CALLS
    hf_max_calls: int = DEFAULT_HF_MAX_CALLS

    def __repr__(self):
        shown = ", ".join(
            f"{f.name}={'<unset>' if getattr(self, f.name) is None else '<set>'}"
            for f in fields(self))
        return f"Settings({shown})"

    __str__ = __repr__


def load_settings(environ=None) -> Settings:
    """Parse every variable; raise ConfigError on the first bad one."""
    environ = os.environ if environ is None else environ
    return Settings(
        provider=parse_provider(environ),
        max_calls=parse_max_calls(environ),
        sheet_id=parse_sheet_id(environ),
        github_token=parse_github_token(environ),
        google_credentials=parse_google_credentials(environ),
        dry_run=parse_dry_run(environ),
        provider_max_calls=parse_provider_max_calls(environ),
        sheets_max_calls=parse_sheets_max_calls(environ),
        hf_max_calls=parse_hf_max_calls(environ),
    )
