"""Apollo.io people provider: one people search per lookup, behind the call budget.

Search only. Email reveal is intentionally NOT implemented: this provider never calls an
email-reveal, enrichment or any other credit-spending endpoint. Adding reveal would need an
explicit opt-in setting plus its own separate budget. A person whose email is missing or
locked is kept with ``email=None`` and confidence 0.0, so a reviewer still sees the name and
title.

Dry run (the default) never touches the network: it returns the fake provider's synthetic
people and says so on one line. A live lookup reads APOLLO_API_KEY from the environment,
charges the CallBudget it was given BEFORE the request, sends the key in a header (never in
the URL or the body) and maps every failure to a code-only ProviderError. It never sleeps or
retries.
"""
import os
import re
import sys

from .. import http as http_mod
from ..budget import BudgetExceeded, CallBudget
from ..models import Person
from .base import ProviderError
from .fake import FakeProvider

# --- Apollo API details ------------------------------------------------------------------
# UNVERIFIED general knowledge, not checked against Apollo's documentation:
# verify against Apollo's current API documentation before first live use.
API_KEY_ENV = "APOLLO_API_KEY"
API_HOST = "api.apollo.io"
PEOPLE_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"  # POST, JSON body
API_KEY_HEADER = "X-Api-Key"
BODY_DOMAINS = "q_organization_domains_list"   # list of domains
BODY_TITLES = "person_titles"                  # list of titles, sent only when titles are given
BODY_PER_PAGE = "per_page"
BODY_PAGE = "page"
MAX_PER_PAGE = 100
RESP_PEOPLE = "people"           # {"people": [...], "pagination": {...}}
FIELD_NAME = "name"
FIELD_FIRST_NAME = "first_name"
FIELD_LAST_NAME = "last_name"
FIELD_TITLE = "title"
FIELD_EMAIL = "email"            # may be missing, null, "" or a locked placeholder
FIELD_EMAIL_STATUS = "email_status"
LOCKED_EMAIL_LOCAL_PARTS = ("email_not_unlocked",)   # placeholder address meaning "locked"
NO_ADDRESS_EMAIL_STATUSES = ("locked", "unavailable", "bounced")
EMAIL_STATUS_CONFIDENCE = {"verified": 0.9, "likely_to_engage": 0.7, "guessed": 0.5,
                           "extrapolated": 0.5}    # any other status: 0.0
RESP_ERROR_CODE = "error_code"   # {"error": "...", "error_code": "..."}
PLAN_EXHAUSTED_ERROR_CODES = ("insufficient_credits", "credits_exhausted")
# ------------------------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 15.0
RETRY_AFTER_MAX_SECONDS = 60
DRY_RUN_NOTICE = "apollo: dry run, returning synthetic people from the fake provider (no network call)"
REVEAL_NOTE = "apollo: email reveal is not implemented (search only, no credit-spending calls)"
_DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                        r"[a-z][a-z0-9-]{0,61}[a-z0-9]", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"[0-9]{1,5}")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _real_email(value, status):
    """The address if it is a usable one; None if missing, empty, locked or malformed."""
    if not isinstance(value, str) or status in NO_ADDRESS_EMAIL_STATUSES:
        return None
    value = value.strip()
    if (value.count("@") != 1 or any(c.isspace() for c in value) or len(value) > 254
            or "." not in value.split("@")[1]):
        return None
    if value.split("@")[0].lower() in LOCKED_EMAIL_LOCAL_PARTS:
        return None
    return value


def error_code(status, data, headers):
    """Map a non-2xx response to a ProviderError code."""
    err = data.get(RESP_ERROR_CODE) if isinstance(data, dict) else None
    if status == 402 or (400 <= status < 500 and status != 401
                         and err in PLAN_EXHAUSTED_ERROR_CODES):
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


class ApolloProvider:
    """``ApolloProvider(budget, dry_run=False)`` for live use; the bare default is a dry run.

    The provider never creates its own budget: live mode refuses to start without one.
    """

    name = "apollo"
    required_env = (API_KEY_ENV,)

    def __init__(self, budget=None, *, dry_run=True, http=None, environ=None, out=None):
        if budget is not None and not isinstance(budget, CallBudget):
            raise TypeError("budget must be a CallBudget")
        if not isinstance(dry_run, bool):
            raise TypeError("dry_run must be True or False")
        if not dry_run and budget is None:
            raise ValueError("live mode needs a CallBudget")
        self.dry_run = dry_run
        self._budget = budget
        self._http = http
        self._environ = environ
        self._out = out

    def __repr__(self):
        return f"ApolloProvider(dry_run={self.dry_run})"

    def doctor_lines(self, settings):
        lines = []
        if settings.dry_run:
            lines.append(("WARN", "apollo: live use is not enabled until dry run is turned off"))
        lines.append(("PASS", REVEAL_NOTE))
        return lines

    def find_people(self, company_domain: str, titles: tuple[str, ...],
                    limit: int) -> list[Person]:
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
        payload = {BODY_DOMAINS: [company_domain], BODY_PER_PAGE: min(limit, MAX_PER_PAGE),
                   BODY_PAGE: 1}
        if titles:
            payload[BODY_TITLES] = [t.strip() for t in titles]
        headers = {API_KEY_HEADER: key, "Accept": "application/json",
                   "User-Agent": "repo-signals"}
        status, data = resp = self._client().post_json(
            PEOPLE_SEARCH_URL, payload, headers, REQUEST_TIMEOUT_SECONDS)
        if not 200 <= status < 300:
            raise ProviderError(error_code(status, data, resp.headers))
        # Search only. Locked emails stay None: no reveal or enrichment call, by design.
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

    def _parse(self, data, company_domain, titles, limit):
        rows = data.get(RESP_PEOPLE) if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ProviderError("unexpected_shape")
        wanted = tuple(t.strip().casefold() for t in titles)
        people = []
        for row in rows:
            if len(people) >= limit:
                break
            name = _text(row.get(FIELD_NAME)) or " ".join(
                p for p in (_text(row.get(FIELD_FIRST_NAME)), _text(row.get(FIELD_LAST_NAME))) if p)
            title = _text(row.get(FIELD_TITLE))
            if not name or not title:
                continue
            if wanted and not any(w in title.casefold() for w in wanted):
                continue
            status = row.get(FIELD_EMAIL_STATUS)
            status = status.strip().lower() if isinstance(status, str) else None
            email = _real_email(row.get(FIELD_EMAIL), status)
            confidence = EMAIL_STATUS_CONFIDENCE.get(status, 0.0) if email else 0.0
            people.append(Person(name=name, email=email, title=title,
                                 company_domain=company_domain, source=self.name,
                                 confidence=confidence))
        return people
