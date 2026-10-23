"""Hunter.io people provider: one domain search per lookup, behind the call budget.

Dry run (the default) never touches the network: it returns the fake provider's
synthetic people and says so on one line. A live lookup reads HUNTER_API_KEY
from the environment, charges the CallBudget it was given BEFORE the request,
sends the key in a header (never in the URL) and maps every failure to a
code-only ProviderError. It never sleeps or retries.

Hunter has no title parameter, so titles are filtered here: a case-insensitive substring match
on the position. "CTO" matches "Co-founder & CTO" but not "Chief Technology Officer". A record
without a usable email, a name or a position, or filtered out by title, is counted in
``last_dropped`` (code to count, reset at the very start of every lookup) and never logged.
"""
import os
import re
import sys

from .. import http as http_mod
from ..budget import BudgetExceeded, CallBudget
from ..models import Person
from .base import ProviderError
from .fake import FakeProvider

# --- Hunter API details ------------------------------------------------------------------
# Checked against a real Hunter account on 2026-10-22: the endpoint, the X-API-KEY header,
# the domain and limit parameters, the data and emails wrapper keys, and the response fields
# for email, position, first and last name and confidence. Still unchecked: the largest
# allowed limit, the seniority and department filters, the score range, the error status
# codes and Retry-After, the error body and error ids, the credits-remaining field, and how
# many credits one search costs. Hunter has no title parameter: titles are matched here as a
# case-insensitive substring of FIELD_POSITION, so "CTO" does not match "Chief Technology Officer".
# verify against Hunter's current API documentation before first live use.
API_KEY_ENV = "HUNTER_API_KEY"
API_HOST = "api.hunter.io"
DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
API_KEY_HEADER = "X-API-KEY"
PARAM_DOMAIN = "domain"
PARAM_LIMIT = "limit"
PARAM_SENIORITY = "seniority"
PARAM_DEPARTMENT = "department"
MAX_LIMIT = 100
SENIORITY_VALUES = ("junior", "senior", "executive")
DEPARTMENT_VALUES = ("executive", "it", "finance", "management", "sales", "legal", "support",
                     "hr", "marketing", "communication", "education", "design", "health",
                     "operations")
RESP_DATA = "data"                # {"data": {"emails": [...]}, "meta": {...}}
RESP_EMAILS = "emails"
FIELD_EMAIL = "value"
FIELD_FIRST_NAME = "first_name"
FIELD_LAST_NAME = "last_name"
FIELD_POSITION = "position"
FIELD_SCORE = "confidence"        # 0..100
SCORE_MAX = 100
RESP_ERRORS = "errors"            # {"errors": [{"id": "...", "code": 402, "details": "..."}]}
ERROR_ID = "id"
PLAN_EXHAUSTED_ERROR_IDS = ("usage_limit_reached", "insufficient_credits")
CREDITS_REMAINING_PATHS = (("meta", "requests_remaining"), ("meta", "credits_remaining"),
                           ("meta", "calls", "available"))
# ------------------------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 15.0
RETRY_AFTER_MAX_SECONDS = 60
DRY_RUN_NOTICE = "hunter: dry run, returning synthetic people from the fake provider (no network call)"
# repo-signals run asks for 3 people per domain (run.PEOPLE_LIMIT; a test keeps the two in step)
# and the title filter runs on those, so titles can return nobody even when matches exist.
TITLES_WARNING = ("hunter: titles are filtered among the first 3 people Hunter returns, so a "
                  "title may return nobody even when matching people exist")
_DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                        r"[a-z][a-z0-9-]{0,61}[a-z0-9]", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"[0-9]{1,5}")


def _get_path(data, path):
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _score(raw):
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= SCORE_MAX:
        return 0.0
    return raw / SCORE_MAX


def _usable_email(value):
    return (isinstance(value, str) and value.count("@") == 1 and "." in value.split("@")[1]
            and not any(c.isspace() for c in value) and len(value) <= 254)


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def error_code(status, data, headers):
    """Map a non-2xx response to a ProviderError code."""
    errors = data.get(RESP_ERRORS) if isinstance(data, dict) else None
    ids = {e.get(ERROR_ID) for e in errors if isinstance(e, dict)} \
        if isinstance(errors, list) else set()
    if status == 402 or (400 <= status < 500 and status != 401
                         and ids & set(PLAN_EXHAUSTED_ERROR_IDS)):
        return "plan_exhausted"
    if status in (401, 403):
        return "auth_failed"
    if status == 429:
        raw = (headers or {}).get("retry-after", "")
        raw = raw.strip() if isinstance(raw, str) else ""
        if _RETRY_AFTER_RE.fullmatch(raw) and int(raw) <= RETRY_AFTER_MAX_SECONDS:
            return f"retry_after_{int(raw)}"
        return "rate_limited"
    if status in (400, 422):
        return "bad_request"
    if 500 <= status < 600:
        return "server_error"
    return "unexpected_status"


class HunterProvider:
    """``HunterProvider(budget, dry_run=False)`` for live use; the bare default is a dry run.

    The provider never creates its own budget: live mode refuses to start without one.
    ``credits_remaining`` holds the remaining requests or credits from the last response,
    when the response carried that number (no extra call is ever made for it).
    """

    name = "hunter"
    required_env = (API_KEY_ENV,)

    def __init__(self, budget=None, *, dry_run=True, http=None, environ=None, out=None,
                 seniority=None, department=None):
        if budget is not None and not isinstance(budget, CallBudget):
            raise TypeError("budget must be a CallBudget")
        if not isinstance(dry_run, bool):
            raise TypeError("dry_run must be True or False")
        if not dry_run and budget is None:
            raise ValueError("live mode needs a CallBudget")
        if seniority is not None and seniority not in SENIORITY_VALUES:
            raise ValueError("seniority is not one of the supported values")
        if department is not None and department not in DEPARTMENT_VALUES:
            raise ValueError("department is not one of the supported values")
        self.dry_run = dry_run
        self.seniority = seniority
        self.department = department
        self.credits_remaining: int | None = None
        self.last_dropped: dict[str, int] = {}
        self._budget = budget
        self._http = http
        self._environ = environ
        self._out = out

    def __repr__(self):
        return f"HunterProvider(dry_run={self.dry_run})"

    def doctor_lines(self, settings):
        lines = []
        if settings.dry_run:
            lines.append(("WARN", "hunter: live use is not enabled until dry run is turned off"))
        if self.credits_remaining is not None:
            lines.append(("PASS", f"hunter: {self.credits_remaining} requests or credits "
                                  "remaining at the last response"))
        return lines

    def find_people(self, company_domain: str, titles: tuple[str, ...],
                    limit: int) -> list[Person]:
        self.last_dropped = {}  # first, so a lookup that raises leaves no stale counts
        if not isinstance(company_domain, str) or not _DOMAIN_RE.fullmatch(company_domain):
            raise ValueError("company_domain must be a domain name")
        if not isinstance(titles, tuple) or not all(isinstance(t, str) and t.strip() for t in titles):
            raise ValueError("titles must be a tuple of non-empty strings")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive whole number")
        if self.dry_run:
            print(DRY_RUN_NOTICE, file=self._out if self._out is not None else sys.stderr)
            return FakeProvider().find_people(company_domain, titles, limit)
        key = self._key()
        try:
            self._budget.consume(1)
        except BudgetExceeded:
            raise ProviderError("budget_exhausted") from None
        params = {PARAM_DOMAIN: company_domain, PARAM_LIMIT: min(limit, MAX_LIMIT)}
        if self.seniority:
            params[PARAM_SENIORITY] = self.seniority
        if self.department:
            params[PARAM_DEPARTMENT] = self.department
        headers = {API_KEY_HEADER: key, "Accept": "application/json",
                   "User-Agent": "repo-signals"}
        status, data = resp = self._client().get_json(
            DOMAIN_SEARCH_URL, params, headers, REQUEST_TIMEOUT_SECONDS)
        self._record_credits(data)
        if not 200 <= status < 300:
            raise ProviderError(error_code(status, data, resp.headers))
        return self._parse(data, company_domain, titles, limit)

    def _key(self):
        environ = self._environ if self._environ is not None else os.environ
        key = environ.get(API_KEY_ENV, "")
        if not isinstance(key, str) or key.strip() == "":
            raise ProviderError("missing_key")
        if not all(0x20 < ord(c) < 0x7f for c in key):
            raise ProviderError("bad_key")
        return key

    def _client(self):
        if self._http is None:
            self._http = http_mod.HttpClient((API_HOST,))
        return self._http

    def _record_credits(self, data):
        for path in CREDITS_REMAINING_PATHS:
            n = _get_path(data, path)
            if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                self.credits_remaining = n
                return

    def _drop(self, code):
        self.last_dropped[code] = self.last_dropped.get(code, 0) + 1

    def _parse(self, data, company_domain, titles, limit):
        emails = _get_path(data, (RESP_DATA, RESP_EMAILS))
        if not isinstance(emails, list) or not all(isinstance(e, dict) for e in emails):
            raise ProviderError("unexpected_shape")
        wanted = tuple(t.strip().casefold() for t in titles)
        people = []
        for entry in emails:
            if len(people) >= limit:
                break
            email = entry.get(FIELD_EMAIL)
            name = " ".join(p for p in (_text(entry.get(FIELD_FIRST_NAME)),
                                        _text(entry.get(FIELD_LAST_NAME))) if p)
            position = _text(entry.get(FIELD_POSITION))
            if not _usable_email(email):
                self._drop("missing_email")
                continue
            if not name or not position:
                self._drop("missing_name" if not name else "missing_title")
                continue
            # Case-insensitive substring match on the position: Hunter cannot filter by title.
            if wanted and not any(w in position.casefold() for w in wanted):
                self._drop("filtered_by_title")
                continue
            people.append(Person(name=name, email=email, title=position,
                                 company_domain=company_domain, source=self.name,
                                 confidence=_score(entry.get(FIELD_SCORE))))
        return people
