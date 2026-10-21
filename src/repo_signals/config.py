"""Settings from environment variables only, parsed strictly.

    REPO_SIGNALS_PROVIDER      provider name (default: fake)
    REPO_SIGNALS_MAX_CALLS     whole number from 1 to the hard bound (default: 25)
    REPO_SIGNALS_SHEET_ID      Google Sheet id (optional)
    REPO_SIGNALS_GITHUB_TOKEN  GitHub token (optional)
    REPO_SIGNALS_GOOGLE_CREDENTIALS
                               path to a Google service-account JSON key file (optional).
                               A file path only: the JSON itself is refused.

An empty variable counts as unset. Surrounding whitespace is an error, not
something to trim. Dry run is always on: live mode does not exist yet.
Errors name the variable, never its value.
"""
import os
import re
from dataclasses import dataclass, fields

from .budget import DEFAULT_MAX_CALLS, HARD_MAX_CALLS

PROVIDER = "REPO_SIGNALS_PROVIDER"
MAX_CALLS = "REPO_SIGNALS_MAX_CALLS"
SHEET_ID = "REPO_SIGNALS_SHEET_ID"
GITHUB_TOKEN = "REPO_SIGNALS_GITHUB_TOKEN"
GOOGLE_CREDENTIALS = "REPO_SIGNALS_GOOGLE_CREDENTIALS"
DEFAULT_PROVIDER = "fake"

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_CALLS_RE = re.compile(r"[1-9][0-9]{0,5}")
_SHEET_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,128}")
_TOKEN_RE = re.compile(r"[\x21-\x7e]{20,255}")
_PATH_MAX = 4096


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


def parse_max_calls(environ) -> int:
    raw = _raw(environ, MAX_CALLS)
    if raw is None:
        return DEFAULT_MAX_CALLS
    if not _MAX_CALLS_RE.fullmatch(raw) or int(raw) > HARD_MAX_CALLS:
        raise ConfigError(MAX_CALLS, f"must be a whole number from 1 to {HARD_MAX_CALLS}")
    return int(raw)


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


def parse_google_credentials(environ) -> str | None:
    raw = _raw(environ, GOOGLE_CREDENTIALS)
    if raw is None:
        return None
    if raw.lstrip().startswith(("{", "[")):
        raise ConfigError(GOOGLE_CREDENTIALS,
                          "must be a path to the service-account JSON file, not the JSON itself")
    if (len(raw) > _PATH_MAX or raw != raw.strip()
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw)):
        raise ConfigError(GOOGLE_CREDENTIALS, "is not a well-formed file path")
    return raw


@dataclass(frozen=True, repr=False)
class Settings:
    provider: str = DEFAULT_PROVIDER
    max_calls: int = DEFAULT_MAX_CALLS
    sheet_id: str | None = None
    github_token: str | None = None
    google_credentials: str | None = None
    dry_run: bool = True

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
    )
