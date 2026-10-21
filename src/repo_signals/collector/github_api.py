"""The GitHub REST client for the collector: three read-only calls, nothing else.

    get_org(login)                                   GET the organisation's public profile
    search_orgs(name)                                search organisations (candidates only)
    search_repos(term, org, language, pushed_after)  search one org's public repositories

Every call is charged to the CallBudget BEFORE it is sent, goes through ``http.HttpClient``
(https only, the API host only, no redirects, a body cap, a timeout), sends the token only in the
Authorization header, and keeps only allowlisted fields from the response. A rate-limit response
(403, 429, or no requests remaining) stops the client: no further call is made in that run.
It never sleeps and never retries. In dry run (the default) it builds no transport and every call
raises ``dry_run``. It is NOT reachable from the command line.
"""
import re
from datetime import date, datetime
from typing import NamedTuple
from urllib.parse import quote

from .. import __version__
from ..budget import BudgetExceeded, CallBudget
from ..http import HttpClient
from ..providers.base import ProviderError
from .plan import MAX_CANDIDATES_CHECKED
from .signal import (REPOS_PER_TARGET_AND_TERM, TERM_MAX_CHARS, TERM_MIN_CHARS, _LANGUAGE_RE,
                     _LOGIN_RE, _TERM_RE)

# --- GitHub API details --------------------------------------------------------------------
# UNVERIFIED general knowledge, not checked against GitHub's documentation:
# verify against GitHub's current documentation before first live use.
API_HOST = "api.github.com"
API_BASE = "https://api.github.com"
ALLOWED_HOSTS = frozenset({API_HOST})
ORG_PATH = "/orgs/{org}"
SEARCH_ORGS_PATH = "/search/users"
SEARCH_REPOS_PATH = "/search/repositories"
PARAM_QUERY = "q"
PARAM_PER_PAGE = "per_page"
HEADER_ACCEPT = "Accept"
MEDIA_TYPE = "application/vnd.github+json"
HEADER_API_VERSION = "X-GitHub-Api-Version"
API_VERSION = "2022-11-28"
HEADER_AUTHORIZATION = "Authorization"
AUTH_SCHEME = "Bearer"
HEADER_USER_AGENT = "User-Agent"
USER_AGENT = f"repo-signals/{__version__} (+https://honeycombagents.ai)"
HEADER_RATE_REMAINING = "x-ratelimit-remaining"   # lowercased, as HttpClient returns them
HEADER_RATE_RESET = "x-ratelimit-reset"           # epoch seconds
QUALIFIER_TYPE_ORG = "type:org"
QUALIFIER_ORG = "org:{org}"
QUALIFIER_IN = "in:name"
QUALIFIER_NO_FORKS = "fork:false"
QUALIFIER_NO_ARCHIVED = "archived:false"
QUALIFIER_LANGUAGE = "language:{language}"
QUALIFIER_PUSHED_AFTER = "pushed:>={date}"
RESP_ITEMS = "items"
RESP_TOTAL_COUNT = "total_count"
RESP_INCOMPLETE = "incomplete_results"
ORG_KEYS = ("login", "type", "blog", "is_verified")
CANDIDATE_KEYS = ("login", "type")
REPO_KEYS = ("name", "full_name", "html_url", "language", "pushed_at", "fork", "archived",
             "private", "owner")
OWNER_KEYS = ("login", "type")
# --------------------------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 15.0
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DIGITS_RE = re.compile(r"[0-9]{1,12}")
_TOKEN_RE = re.compile(r"[\x21-\x7e]{20,255}")
_SCALARS = (str, bool, int, type(None))


class RateLimit(NamedTuple):
    """Integers only: no raw header text is kept."""

    remaining: int | None = None
    reset: int | None = None


class RateLimitedError(ProviderError):
    """``rate_limited``, with the reset time (epoch seconds) as a number when GitHub sent one."""

    def __init__(self, reset=None):
        super().__init__("rate_limited")
        self.reset = reset if isinstance(reset, int) and not isinstance(reset, bool) else None
        self.args = ("rate_limited" if self.reset is None
                     else f"rate_limited: reset {self.reset}",)

    def __str__(self):
        return self.args[0]


class SearchResult(NamedTuple):
    items: tuple[dict, ...]
    total_count: int | None
    incomplete: bool


def _int_header(headers, name):
    value = (headers or {}).get(name)
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def allowlist_org(raw):
    """``{login, type, blog, is_verified}`` from an org profile, or None if malformed."""
    if not isinstance(raw, dict):
        return None
    org = {k: raw.get(k) for k in ORG_KEYS}
    if (not isinstance(org["login"], str) or not _LOGIN_RE.fullmatch(org["login"])
            or not isinstance(org["type"], str)
            or not isinstance(org["blog"], (str, type(None)))
            or not isinstance(org["is_verified"], (bool, type(None)))):
        return None
    return org


def allowlist_candidate(raw):
    if not isinstance(raw, dict):
        return None
    candidate = {k: raw.get(k) for k in CANDIDATE_KEYS}
    if not isinstance(candidate["login"], str) or not _LOGIN_RE.fullmatch(candidate["login"]) \
            or not isinstance(candidate["type"], str):
        return None
    return candidate


def allowlist_repo(raw):
    """Only the repository keys ``collector.github_fields`` reads, with scalar values."""
    if not isinstance(raw, dict) or not isinstance(raw.get("owner"), dict):
        return None
    repo = {k: raw.get(k) for k in REPO_KEYS if k != "owner"}
    if not all(isinstance(v, _SCALARS) for v in repo.values()):
        return None
    owner = {k: raw["owner"].get(k) for k in OWNER_KEYS}
    if not all(isinstance(v, str) for v in owner.values()):
        return None
    repo["owner"] = owner
    return repo


def _require(pattern, value, code):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProviderError(code)
    return value


def _pushed_after(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        value = value.isoformat()
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ProviderError("bad_date")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ProviderError("bad_date") from None
    return value


class GitHubClient:
    """``GitHubClient(http, budget, token=None, user_agent=None, dry_run=False)`` for live use.

    The default is a dry run: no transport is built and every call raises ``dry_run``.
    """

    def __init__(self, http=None, budget=None, token=None, user_agent=None, *, dry_run=True):
        if not isinstance(dry_run, bool):
            raise TypeError("dry_run must be True or False")
        if token is not None and (not isinstance(token, str) or not _TOKEN_RE.fullmatch(token)):
            raise ValueError("token is not well formed")
        user_agent = USER_AGENT if user_agent is None else user_agent
        if not isinstance(user_agent, str) or not user_agent.isprintable() or not user_agent:
            raise ValueError("user_agent must be one printable line")
        self.dry_run = dry_run
        self.rate_limit = RateLimit()
        self._token = token
        self._user_agent = user_agent
        self._stopped = None
        if dry_run:
            self._http = None
            self._budget = None
            return
        if not isinstance(budget, CallBudget):
            raise ValueError("live mode needs a CallBudget")
        if http is not None and http.allowed_hosts != ALLOWED_HOSTS:
            raise ValueError("the HTTP client must allow exactly the GitHub API host")
        self._budget = budget
        self._http = http if http is not None else HttpClient(ALLOWED_HOSTS)

    def __repr__(self):
        return f"GitHubClient(dry_run={self.dry_run})"

    def _headers(self):
        headers = {HEADER_ACCEPT: MEDIA_TYPE, HEADER_API_VERSION: API_VERSION,
                   HEADER_USER_AGENT: self._user_agent}
        if self._token:
            headers[HEADER_AUTHORIZATION] = f"{AUTH_SCHEME} {self._token}"
        return headers

    def _get(self, path, params=None):
        if self.dry_run:
            raise ProviderError("dry_run")
        if self._stopped is not None:
            raise self._stopped
        try:
            self._budget.consume(1)
        except BudgetExceeded:
            raise ProviderError("budget_exhausted") from None
        status, data = resp = self._http.get_json(API_BASE + path, params, self._headers(),
                                                  REQUEST_TIMEOUT_SECONDS)
        self.rate_limit = RateLimit(_int_header(resp.headers, HEADER_RATE_REMAINING),
                                    _int_header(resp.headers, HEADER_RATE_RESET))
        if status in (403, 429):
            self._stopped = RateLimitedError(self.rate_limit.reset)
            raise self._stopped
        if status == 401:
            self._stopped = ProviderError("token_rejected")
            raise self._stopped
        if self.rate_limit.remaining == 0:
            # This call succeeded or failed on its own; the next one would be refused.
            self._stopped = RateLimitedError(self.rate_limit.reset)
        if 200 <= status < 300:
            if not isinstance(data, dict):
                raise ProviderError("unexpected_shape")
            return data
        if status == 404:
            raise ProviderError("not_found")
        if status == 422:
            raise ProviderError("bad_query")
        if 500 <= status < 600:
            raise ProviderError("server_error")
        raise ProviderError("unexpected_status")

    def _search(self, path, query, per_page, allowlist):
        data = self._get(path, {PARAM_QUERY: query, PARAM_PER_PAGE: per_page})
        items = data.get(RESP_ITEMS)
        if not isinstance(items, list):
            raise ProviderError("unexpected_shape")
        kept = tuple(x for x in map(allowlist, items[:per_page]) if x is not None)
        total = data.get(RESP_TOTAL_COUNT)
        total = total if isinstance(total, int) and not isinstance(total, bool) else None
        return SearchResult(kept, total, data.get(RESP_INCOMPLETE) is True)

    def get_org(self, login) -> dict:
        login = _require(_LOGIN_RE, login, "bad_org")
        org = allowlist_org(self._get(ORG_PATH.format(org=quote(login, safe=""))))
        if org is None:
            raise ProviderError("unexpected_shape")
        return org

    def search_orgs(self, name) -> SearchResult:
        name = _require(_LOGIN_RE, name, "bad_name")
        return self._search(SEARCH_ORGS_PATH, f"{name} {QUALIFIER_TYPE_ORG}",
                            MAX_CANDIDATES_CHECKED, allowlist_candidate)

    def search_repos(self, term, org, language=None, pushed_after=None) -> SearchResult:
        _require(_TERM_RE, term, "bad_term")
        if not TERM_MIN_CHARS <= len(term) <= TERM_MAX_CHARS:
            raise ProviderError("bad_term")
        parts = [term, QUALIFIER_IN, QUALIFIER_ORG.format(org=_require(_LOGIN_RE, org, "bad_org")),
                 QUALIFIER_NO_FORKS, QUALIFIER_NO_ARCHIVED]
        if language is not None:
            parts.append(QUALIFIER_LANGUAGE.format(
                language=_require(_LANGUAGE_RE, language, "bad_language")))
        if pushed_after is not None:
            parts.append(QUALIFIER_PUSHED_AFTER.format(date=_pushed_after(pushed_after)))
        return self._search(SEARCH_REPOS_PATH, " ".join(parts), REPOS_PER_TARGET_AND_TERM,
                            allowlist_repo)
