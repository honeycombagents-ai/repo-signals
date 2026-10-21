"""The suppression list: people who must never get a draft.

The file named by REPO_SIGNALS_SUPPRESSION_FILE holds one email address or one domain per
line. Blank lines and lines starting with ``#`` are ignored. Anything else is an error (the
code names the line number, never its text). The file is capped at 1 MB.

A person is suppressed when their email is listed, or when their email's domain or their
company domain is a listed domain or a subdomain of one.
"""
import os
import re
from typing import NamedTuple

from .. import config
from .base import DraftError

MAX_SUPPRESSION_BYTES = 1024 * 1024
_DOMAIN = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]"
_DOMAIN_RE = re.compile(_DOMAIN)
_EMAIL_RE = re.compile(r"[a-z0-9._%+'-]{1,64}@" + _DOMAIN)


class Suppression(NamedTuple):
    emails: frozenset
    domains: frozenset

    def __repr__(self):
        return f"Suppression({len(self.emails)} email(s), {len(self.domains)} domain(s))"

    def _domain_listed(self, domain):
        domain = domain.lower().rstrip(".")
        return any(domain == d or domain.endswith("." + d) for d in self.domains)

    def matches(self, person) -> bool:
        email = getattr(person, "email", None)
        if isinstance(email, str) and email:
            email = email.strip().lower()
            if email in self.emails or self._domain_listed(email.rpartition("@")[2]):
                return True
        domain = getattr(person, "company_domain", None)
        return isinstance(domain, str) and bool(domain) and self._domain_listed(domain)


def parse_suppression(text) -> Suppression:
    emails, domains = set(), set()
    for n, line in enumerate(text.split("\n"), 1):
        entry = line.strip().lower()
        if not entry or entry.startswith("#"):
            continue
        if len(entry) <= 254 and _EMAIL_RE.fullmatch(entry):
            emails.add(entry)
        elif len(entry) <= 253 and _DOMAIN_RE.fullmatch(entry):
            domains.add(entry)
        else:
            raise DraftError(f"suppression_malformed_line_{n}")
    return Suppression(frozenset(emails), frozenset(domains))


def load_suppression(path) -> Suppression:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SUPPRESSION_BYTES + 1)
    except (OSError, TypeError, ValueError):
        raise DraftError("suppression_unreadable") from None
    if len(raw) > MAX_SUPPRESSION_BYTES:
        raise DraftError("suppression_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DraftError("suppression_malformed") from None
    if "\r" in text.replace("\r\n", "\n") or "\0" in text:
        raise DraftError("suppression_malformed")
    return parse_suppression(text.replace("\r\n", "\n"))


def suppression_from_env(environ=None) -> Suppression | None:
    """The list named by REPO_SIGNALS_SUPPRESSION_FILE, or None when it is not set."""
    environ = os.environ if environ is None else environ
    try:
        path = config.parse_suppression_file(environ)
    except config.ConfigError:
        raise DraftError("suppression_path_malformed", config.SUPPRESSION_FILE) from None
    return None if path is None else load_suppression(path)
