"""Match each signal target to a GitHub organisation, never guessing silently.

Method A (the target names an org): look the org up; it must exist and be an Organization. If
its website is on a different domain, or it lists no website at all, the target is excluded as
``website_mismatch`` (code ``website_mismatch`` or ``no_website``) unless the target sets
``confirm_org = true``.

Method B (domain only): search organisations by a name derived from the domain, look up at most
MAX_CANDIDATES_CHECKED candidates, and accept one only if exactly one has a website on exactly
the target domain (or www. plus it) AND is marked verified. A name match alone never produces a
match: those candidates are only suggestions.

Matching takes any client with ``get_org`` and ``search_orgs`` (see OrgClient); it imports no
HTTP code. A client error becomes a status with a code for that target and never aborts the
others, except ``rate_limited``, which stops matching for every later target.
"""
import re
from collections import Counter
from typing import NamedTuple, Protocol
from urllib.parse import urlsplit

from .github_fields import ORGANIZATION
from .plan import MAX_CANDIDATES_CHECKED
from .signal import Signal, _LOGIN_RE

MATCHED, UNMATCHED, AMBIGUOUS = "matched", "unmatched", "ambiguous"
WEBSITE_MISMATCH, NOT_AN_ORG, NOT_FOUND = "website_mismatch", "not_an_org", "not_found"
RATE_LIMITED, ERROR = "rate_limited", "error"
STATUSES = (MATCHED, UNMATCHED, AMBIGUOUS, WEBSITE_MISMATCH, NOT_AN_ORG, NOT_FOUND,
            RATE_LIMITED, ERROR)

USER, VERIFIED_DOMAIN, WEBSITE_ONLY, NAME_ONLY = "user", "verified-domain", "website-only", "name-only"
INCLUDED_CONFIDENCE = (USER, VERIFIED_DOMAIN)
MAX_SUGGESTIONS = 3

_HOST_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                      r"[a-z][a-z0-9-]{0,61}[a-z0-9]")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class OrgClient(Protocol):
    """What matching needs from a client. The collector's GitHub client fits; tests use a fake."""

    def get_org(self, login): ...

    def search_orgs(self, name): ...


class MatchResult(NamedTuple):
    domain: str
    org_login: str | None
    confidence: str | None
    status: str
    suggestions: tuple[str, ...] = ()
    code: str | None = None
    note: str | None = None          # "confirmed" or "no_website" for method A matches

    @property
    def usable(self) -> bool:
        return self.status == MATCHED and self.confidence in INCLUDED_CONFIDENCE


class MatchReport(NamedTuple):
    results: tuple[MatchResult, ...]
    counts: dict
    lines: tuple[str, ...]

    @property
    def usable(self) -> tuple[MatchResult, ...]:
        return tuple(r for r in self.results if r.usable)


def blog_host(blog) -> str | None:
    """The lowercase host of an org's website value, or None if it is not a plain website.

    The scheme is optional (http or https only). Userinfo, a port, a backslash, whitespace or a
    host that is not a plain hostname all give None.
    """
    if not isinstance(blog, str):
        return None
    value = blog.strip()
    if not value or len(value) > 2048 or "\\" in value or any(
            c.isspace() or not c.isprintable() for c in value):
        return None
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or port is not None:
        return None
    if "@" in parts.netloc or ":" in parts.netloc:
        return None
    host = parts.netloc.lower()
    return host if _HOST_RE.fullmatch(host) else None


def domain_matches(blog, domain) -> bool:
    """True only when the website's host is exactly ``domain`` or ``www.`` plus it."""
    host = blog_host(blog)
    domain = domain.lower()
    return host is not None and host in (domain, "www." + domain)


def search_name(domain) -> str | None:
    """The label left of the top-level domain (``example`` from ``shop.example.com``)."""
    labels = domain.lower().split(".")
    name = labels[-2] if len(labels) >= 2 else ""
    return name if _LOGIN_RE.fullmatch(name) else None


def _code(e):
    code = getattr(e, "code", None)
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "client_error"


def _login(org):
    login = org.get("login") if isinstance(org, dict) else None
    return login if isinstance(login, str) and _LOGIN_RE.fullmatch(login) else None


def _failure(domain, org_login, e):
    code = _code(e)
    if code == "rate_limited":
        return MatchResult(domain, org_login, None, RATE_LIMITED, code=code)
    if code == "not_found":
        return MatchResult(domain, org_login, None, NOT_FOUND, code=code)
    return MatchResult(domain, org_login, None, ERROR, code=code)


def match_target(target, client) -> MatchResult:
    """One target's match. Never raises for a client problem: it becomes a status and code."""
    domain = target.domain
    if target.org:
        try:
            org = client.get_org(target.org)
        except Exception as e:  # a client problem is this target's status, never an abort
            return _failure(domain, target.org, e)
        login = _login(org)
        if login is None:
            return MatchResult(domain, target.org, None, ERROR, code="unexpected_shape")
        if org.get("type") != ORGANIZATION:
            return MatchResult(domain, login, None, NOT_AN_ORG, code="not_an_org")
        blog = org.get("blog")
        if blog_host(blog) is None and not (isinstance(blog, str) and blog.strip()):
            if target.confirm_org:  # no website to check: only the user's confirmation counts
                return MatchResult(domain, login, USER, MATCHED, note="confirmed")
            return MatchResult(domain, login, USER, WEBSITE_MISMATCH, code="no_website")
        if domain_matches(blog, domain):
            return MatchResult(domain, login, USER, MATCHED)
        if target.confirm_org:
            return MatchResult(domain, login, USER, MATCHED, note="confirmed")
        return MatchResult(domain, login, USER, WEBSITE_MISMATCH, code="website_mismatch")

    name = search_name(domain)
    if name is None:
        return MatchResult(domain, None, None, UNMATCHED, code="no_search_name")
    try:
        found = client.search_orgs(name)
    except Exception as e:
        return _failure(domain, None, e)
    items = getattr(found, "items", None)
    if not isinstance(items, (tuple, list)):
        return MatchResult(domain, None, None, ERROR, code="unexpected_shape")
    candidates = []
    for item in items:
        login = _login(item)
        if login and item.get("type") == ORGANIZATION and login not in candidates:
            candidates.append(login)
    candidates = candidates[:MAX_CANDIDATES_CHECKED]
    verified, website_only = [], []
    for login in candidates:
        try:
            org = client.get_org(login)
        except Exception as e:
            if _code(e) == "rate_limited":
                return _failure(domain, None, e)
            continue  # this candidate cannot be checked, so it cannot be accepted
        if _login(org) != login or org.get("type") != ORGANIZATION:
            continue
        if domain_matches(org.get("blog"), domain):
            (verified if org.get("is_verified") is True else website_only).append(login)
    suggestions = tuple(candidates[:MAX_SUGGESTIONS])
    if len(verified) == 1:
        return MatchResult(domain, verified[0], VERIFIED_DOMAIN, MATCHED)
    if len(verified) > 1:
        return MatchResult(domain, None, VERIFIED_DOMAIN, AMBIGUOUS,
                           tuple(verified[:MAX_SUGGESTIONS]), code="ambiguous")
    if len(website_only) == 1:
        return MatchResult(domain, website_only[0], WEBSITE_ONLY, MATCHED)
    if len(website_only) > 1:
        return MatchResult(domain, None, WEBSITE_ONLY, AMBIGUOUS,
                           tuple(website_only[:MAX_SUGGESTIONS]), code="ambiguous")
    return MatchResult(domain, None, NAME_ONLY if suggestions else None, UNMATCHED, suggestions,
                       code="unmatched")


def report_line(r) -> str:
    """One code-only line: domains, org logins, labels and codes. Never website or other text."""
    if r.status == MATCHED and r.confidence == USER:
        extra = {"confirmed": ", confirmed", "no_website": ", no website listed"}.get(r.note, "")
        return f"{r.domain} -> {r.org_login} (user{extra})"
    if r.status == MATCHED and r.confidence == VERIFIED_DOMAIN:
        return f"{r.domain} -> {r.org_login} (verified-domain)"
    if r.status == MATCHED and r.confidence == WEBSITE_ONLY:
        return (f'{r.domain} -> {r.org_login} (website-only: not included; add '
                f'org = "{r.org_login}" to confirm)')
    if r.status == WEBSITE_MISMATCH and r.code == "no_website":
        return (f"{r.domain} -> {r.org_login} (user, no website listed: excluded; set "
                "confirm_org = true to include)")
    if r.status == WEBSITE_MISMATCH:
        return (f"{r.domain} -> {r.org_login} (user, website_mismatch: excluded; set "
                "confirm_org = true to include)")
    if r.status == AMBIGUOUS:
        return (f"{r.domain}: ambiguous ({len(r.suggestions)} candidate orgs: "
                f'{", ".join(r.suggestions)}; add org = "..." to confirm)')
    if r.status == UNMATCHED:
        line = f"{r.domain}: unmatched (no organisation passed the domain check)"
        return line + (f"; suggestions: {', '.join(r.suggestions)}" if r.suggestions else "")
    if r.status == NOT_AN_ORG:
        return f"{r.domain}: {r.org_login} is not an organisation"
    if r.status == NOT_FOUND:
        return f"{r.domain}: org {r.org_login} not found" if r.org_login \
            else f"{r.domain}: not found"
    if r.status == RATE_LIMITED and r.code == "not_attempted":
        return f"{r.domain}: not attempted (rate limited earlier in this run)"
    return f"{r.domain}: not matched ({r.code})"


def match_all(signal, client) -> MatchReport:
    """Match every target in order. After a rate limit, later targets are not attempted."""
    if not isinstance(signal, Signal):
        raise TypeError("signal must be a Signal")
    results, stopped = [], False
    for target in signal.targets:
        if stopped:
            result = MatchResult(target.domain, None, None, RATE_LIMITED, code="not_attempted")
        else:
            result = match_target(target, client)
            stopped = result.status == RATE_LIMITED
        results.append(result)
    counts = dict(Counter(r.status for r in results))
    return MatchReport(tuple(results), counts, tuple(report_line(r) for r in results))
