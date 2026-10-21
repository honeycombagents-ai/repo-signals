"""The signal definition: a small, strict TOML file the user writes.

    schema = 1                      # optional; must be 1
    name = "examplelib-adopters"
    terms = ["examplelib"]          # 1 to 5 search terms
    language = "Python"             # optional
    pushed_within_days = 365        # optional; 1 to 3650; default 365
    titles = ["CTO"]                # optional; up to 5 job titles for the people lookup

    [[targets]]                     # 1 to 50 targets
    domain = "example.com"
    org = "example-org"             # optional GitHub organisation login
    confirm_org = true              # optional; include the org even if its website differs

Anything else is refused. Errors are code-only: they may name a key or a position (for example
``signal_target_3_bad_domain``) but never repeat file content.
"""
import os
import re
import tomllib
from dataclasses import dataclass

from .. import config

MAX_SIGNAL_BYTES = 64 * 1024
MAX_TERMS = 5
MAX_TARGETS = 50
TERM_MIN_CHARS = 2
TERM_MAX_CHARS = 60
DEFAULT_PUSHED_WITHIN_DAYS = 365
PUSHED_WITHIN_DAYS_MAX = 3650
REPOS_PER_TARGET_AND_TERM = 5      # used by the collector, not by the plan
EVIDENCE_PER_TARGET = 10           # used by the collector, not by the plan
SCHEMA_VERSION = 1
MAX_TITLES = 5
TITLE_MAX_CHARS = 60

TOP_KEYS = frozenset({"schema", "name", "terms", "language", "pushed_within_days", "targets",
                      "titles"})
TARGET_KEYS = frozenset({"domain", "org", "confirm_org"})
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,47}")
# Letters, digits and . _ + - only: no space, quote or colon, so a term cannot add a qualifier.
_TERM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_LANGUAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{0,31}")
# A job title: letters and digits, single spaces, and & . / + - ; for example "VP Engineering".
_TITLE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9&./+-]| (?! ))*")
_DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                        r"[a-z][a-z0-9-]{0,61}[a-z0-9]")
# GitHub login shape (unverified): letters, digits, single inner hyphens, at most 39 characters.
_LOGIN_RE = re.compile(r"(?=.{1,39}\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


class SignalError(Exception):
    """A signal file problem, described by a short code only (never file content)."""

    def __init__(self, code: str):
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            code = "unspecified"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Target:
    domain: str
    org: str | None = None
    confirm_org: bool = False


@dataclass(frozen=True)
class Signal:
    name: str
    terms: tuple[str, ...]
    targets: tuple[Target, ...]
    language: str | None = None
    pushed_within_days: int = DEFAULT_PUSHED_WITHIN_DAYS
    titles: tuple[str, ...] = ()

    def __repr__(self):
        return f"Signal({len(self.terms)} term(s), {len(self.targets)} target(s))"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _terms(raw):
    if raw is None:
        raise SignalError("signal_missing_terms")
    if not isinstance(raw, list):
        raise SignalError("signal_bad_terms")
    if not raw:
        raise SignalError("signal_no_terms")
    if len(raw) > MAX_TERMS:
        raise SignalError("signal_too_many_terms")
    seen = set()
    for n, term in enumerate(raw, 1):
        if (not isinstance(term, str) or not TERM_MIN_CHARS <= len(term) <= TERM_MAX_CHARS
                or not _TERM_RE.fullmatch(term)):
            raise SignalError(f"signal_bad_term_{n}")
        if term.casefold() in seen:
            raise SignalError("signal_duplicate_term")
        seen.add(term.casefold())
    return tuple(raw)


def _titles(raw):
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SignalError("signal_bad_titles")
    if len(raw) > MAX_TITLES:
        raise SignalError("signal_too_many_titles")
    seen = set()
    for n, title in enumerate(raw, 1):
        if (not isinstance(title, str) or not 1 <= len(title) <= TITLE_MAX_CHARS
                or not _TITLE_RE.fullmatch(title) or title.endswith(" ")):
            raise SignalError(f"signal_bad_title_{n}")
        if title.casefold() in seen:
            raise SignalError("signal_duplicate_title")
        seen.add(title.casefold())
    return tuple(raw)


def _targets(raw):
    if raw is None:
        raise SignalError("signal_missing_targets")
    if not isinstance(raw, list):
        raise SignalError("signal_bad_targets")
    if not raw:
        raise SignalError("signal_no_targets")
    if len(raw) > MAX_TARGETS:
        raise SignalError("signal_too_many_targets")
    targets, domains, orgs = [], set(), set()
    for n, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise SignalError(f"signal_target_{n}_not_a_table")
        if not set(entry) <= TARGET_KEYS:
            raise SignalError(f"signal_target_{n}_unknown_key")
        domain = entry.get("domain")
        if not isinstance(domain, str) or not _DOMAIN_RE.fullmatch(domain.lower()):
            raise SignalError(f"signal_target_{n}_bad_domain")
        domain = domain.lower()
        org = entry.get("org")
        if org is not None and (not isinstance(org, str) or not _LOGIN_RE.fullmatch(org)):
            raise SignalError(f"signal_target_{n}_bad_org")
        confirm = entry.get("confirm_org", False)
        if not isinstance(confirm, bool):
            raise SignalError(f"signal_target_{n}_bad_confirm_org")
        if confirm and org is None:
            raise SignalError(f"signal_target_{n}_confirm_org_without_org")
        if domain in domains:
            raise SignalError("signal_duplicate_domain")
        if org is not None and org.lower() in orgs:
            raise SignalError("signal_duplicate_org")
        domains.add(domain)
        if org is not None:
            orgs.add(org.lower())
        targets.append(Target(domain, org, confirm))
    return tuple(targets)


def parse_signal(raw) -> Signal:
    """Parse a signal file's bytes (or text). Raise a code-only SignalError on any problem."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes):
        raise SignalError("signal_not_toml")
    if len(raw) > MAX_SIGNAL_BYTES:
        raise SignalError("signal_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SignalError("signal_not_utf8") from None
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError):
        raise SignalError("signal_not_toml") from None
    if not set(data) <= TOP_KEYS:
        raise SignalError("signal_unknown_key")
    schema = data.get("schema", SCHEMA_VERSION)
    if not _is_int(schema) or schema != SCHEMA_VERSION:
        raise SignalError("signal_bad_schema")
    name = data.get("name")
    if name is None:
        raise SignalError("signal_missing_name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise SignalError("signal_bad_name")
    terms = _terms(data.get("terms"))
    language = data.get("language")
    if language is not None and (not isinstance(language, str)
                                 or not _LANGUAGE_RE.fullmatch(language)):
        raise SignalError("signal_bad_language")
    days = data.get("pushed_within_days", DEFAULT_PUSHED_WITHIN_DAYS)
    if not _is_int(days) or not 1 <= days <= PUSHED_WITHIN_DAYS_MAX:
        raise SignalError("signal_bad_pushed_within_days")
    return Signal(name=name, terms=terms, targets=_targets(data.get("targets")),
                  language=language, pushed_within_days=days,
                  titles=_titles(data.get("titles")))


def load_signal(path) -> Signal:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SIGNAL_BYTES + 1)
    except (OSError, TypeError, ValueError):
        raise SignalError("signal_unreadable") from None
    return parse_signal(raw)


def signal_from_env(environ=None) -> Signal | None:
    """The signal named by REPO_SIGNALS_SIGNAL_FILE, or None when it is not set."""
    environ = os.environ if environ is None else environ
    try:
        path = config.parse_signal_file(environ)
    except config.ConfigError:
        raise SignalError("signal_path_malformed") from None
    return None if path is None else load_signal(path)
