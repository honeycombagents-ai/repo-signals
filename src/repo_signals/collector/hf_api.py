"""The Hugging Face client for the collector: an organisation overview and repository listings.

    get_org_overview(org)                             GET the organisation's overview
    list_repos(kind, org, expand, cursor, limit)      one page of an org's models, datasets
                                                      or Spaces
    list_all(kind, org, expand, limit, max_pages)     up to ``max_pages`` pages, then stop

Every request is charged to the CallBudget BEFORE it is sent, goes through ``http.HttpClient``
(https only, the Hugging Face host only, no redirects, a body cap, a timeout), uses GET only and
sends no token (anonymous). The overview keeps only the organisation's name, ``isVerified`` and
three counts; the bio and everything else are dropped at once. Listing records are passed through
untouched for ``hf_fields`` to read its allowlist from, and are never logged or printed.

A 401 or 429, or a response reporting no requests remaining, stops the client: no further request
is sent in that run. It never sleeps and never retries. The next page is requested from the known
host and path plus a validated cursor taken from the ``Link`` header; a URL from a header is never
followed. In dry run (the default) it builds no transport and every call raises ``dry_run``.

It is NOT reachable from the command line yet (build step 3 of docs/huggingface-design.md).
"""
import re
from typing import NamedTuple
from urllib.parse import parse_qs, quote, urlsplit

from .. import __version__
from ..budget import BudgetExceeded, CallBudget
from ..http import HttpClient
from ..providers.base import ProviderError
from .signal import _LOGIN_RE

# --- Hugging Face API details --------------------------------------------------------------
# Checked live on 2026-10-26 (anonymous, read-only, org "huggingface"):
# - GET /api/organizations/{name}/overview returns 200 with isVerified, counts (models, datasets,
#   Spaces) and a free-text bio, and no website, blog or domain field. An unknown organisation
#   returns 404. Users are a separate endpoint.
# - GET /api/models, /api/datasets and /api/spaces accept author, search, sort=lastModified,
#   direction=-1 and limit; anonymous reads work. Dates are ISO 8601 UTC with milliseconds and a
#   trailing Z.
# - Default fields differ: models lack author, gated and lastModified unless requested with
#   expand; datasets carry author, lastModified and gated; Spaces lack author and downloads.
#   private was false on every anonymous result.
# - Search ignores case but is not a substring match, so names are checked by the caller.
# - Pagination is a Link header with rel="next" and an opaque cursor; a limit above 1,000 is
#   silently capped at 1,000.
# - A listing for an unknown author returns an empty 200; an unknown or private repository
#   returns 401.
# - The anonymous rate limit is 500 requests per 300-second fixed window, shared across
#   endpoints, reported in the RateLimit and RateLimit-Policy headers.
# - Public URL shapes: huggingface.co/ORG/NAME (model), /datasets/ORG/NAME, /spaces/ORG/NAME.
# Checked live on 2026-10-27 (anonymous, read-only, org "huggingface"):
# - /api/models and /api/datasets accept expand for author, lastModified, private, gated and
#   disabled, and return all five on every record.
# - /api/spaces returns 400 when expand includes gated; with author, lastModified, private and
#   disabled it returns 200 and no gated key. Hence EXPAND_BY_KIND.
# - private and disabled were false on every record seen.
# Checked live on 2026-10-27 (collect --live), anonymously, through collect --live and
# hf-probe-org against the organisation "huggingface":
# - hf-probe-org: isVerified yes; 18 models, 48 datasets and 47 Spaces.
# - A model listing: 18 records in one page, all parsed (no bad id or bad date); 16 outside a
#   365-day window, 2 without the term.
# - A dataset listing (80 records, one page) and a Spaces listing (46 records, one page) both
#   returned 200; Spaces with the per-kind expand (no gated) did not fail.
# - Evidence had the right kind wording (model, dataset, Space), dates, and URLs under
#   huggingface.co/, huggingface.co/datasets/ and huggingface.co/spaces/.
# - The cap of 10 items per target worked (4 dropped as over_target_cap).
# - No short-page cursor was seen (the unchecked item below stays unchecked).
# - Terms of one character are refused (TERM_MIN_CHARS is 2).
# Also seen live, in the earlier probe of the cursor: the Link header's cursor was 344 characters,
# all in the allowed set, so the cursor limit is 1024 characters (it was 512).
# Still unchecked: the 429 body and Retry-After; gated and private behaviour; the token quota and
# scope; redirects for renamed repositories; user-owned repositories; the exact search
# tokenisation; whether a short page (fewer records than the limit) can carry a next cursor;
# Hugging Face's terms for automated access and for outreach use.
# For every unchecked item: verify against Hugging Face's current documentation before first
# live use.
API_HOST = "huggingface.co"
API_BASE = "https://huggingface.co"
ALLOWED_HOSTS = frozenset({API_HOST})
OVERVIEW_PATH = "/api/organizations/{name}/overview"
LIST_PATHS = {"model": "/api/models", "dataset": "/api/datasets", "space": "/api/spaces"}
PARAM_AUTHOR = "author"
PARAM_SORT = "sort"
SORT_LAST_MODIFIED = "lastModified"
PARAM_DIRECTION = "direction"
DIRECTION_DESCENDING = "-1"
PARAM_LIMIT = "limit"
PARAM_EXPAND = "expand"
PARAM_CURSOR = "cursor"
EXPAND_BY_KIND = {"model": ("author", "lastModified", "private", "gated", "disabled"),
                  "dataset": ("author", "lastModified", "private", "gated", "disabled"),
                  "space": ("author", "lastModified", "private", "disabled")}  # no gated: 400
HEADER_LINK = "link"                          # lowercased, as HttpClient returns them
LINK_REL_NEXT = "next"
HEADER_RATE_LIMIT = "ratelimit"               # '"api";r=<remaining>;t=<seconds to reset>'
HEADER_RATE_LIMIT_POLICY = "ratelimit-policy"  # '"fixed window";"api";q=<quota>;w=<seconds>'
OVERVIEW_KEYS = ("name", "isVerified", "numModels", "numDatasets", "numSpaces")
HEADER_ACCEPT = "Accept"
MEDIA_TYPE = "application/json"
HEADER_USER_AGENT = "User-Agent"
USER_AGENT = f"repo-signals/{__version__} (+https://honeycombagents.ai)"
# --------------------------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 15.0
MAX_PAGE_SIZE = 500               # the client never asks for more (the API caps at 1,000)
MAX_PAGES = 2                     # pages per listing, as in the request plan
LISTING_CAPPED = "listing_capped"
_CURSOR_RE = re.compile(r"[A-Za-z0-9_.=+/-]{1,1024}")   # cursors seen live: 344 characters
_LINK_PART_RE = re.compile(r'<([^<>\s]{1,2048})>\s*;\s*rel="?([a-z]+)"?')
_REL_NEXT_RE = re.compile(r'rel="?next"?')
_RATE_R_RE = re.compile(r"(?:^|;)\s*r=([0-9]{1,9})\s*(?:;|$)")
_RATE_T_RE = re.compile(r"(?:^|;)\s*t=([0-9]{1,9})\s*(?:;|$)")


class RateLimit(NamedTuple):
    """Integers only: no raw header text is kept."""

    remaining: int | None = None
    reset: int | None = None


class HFRateLimited(ProviderError):
    """``rate_limited``, with the seconds to the window's reset when the header gave them."""

    def __init__(self, reset=None):
        super().__init__("rate_limited")
        self.reset = reset if isinstance(reset, int) and not isinstance(reset, bool) else None


class Overview(NamedTuple):
    name: str
    is_verified: bool
    models: int
    datasets: int
    spaces: int


class ListingPage(NamedTuple):
    records: tuple          # raw records, untouched, for hf_fields to parse
    next_cursor: str | None


class Listing(NamedTuple):
    records: tuple
    pages: int
    code: str | None        # LISTING_CAPPED when pages remained past the cap, else None


def parse_rate_limit(headers):
    """``RateLimit(remaining, reset)`` from the RateLimit header, integers only."""
    value = (headers or {}).get(HEADER_RATE_LIMIT)
    if not isinstance(value, str):
        return RateLimit()
    r, t = _RATE_R_RE.search(value), _RATE_T_RE.search(value)
    return RateLimit(int(r.group(1)) if r else None, int(t.group(1)) if t else None)


def next_cursor(headers, path):
    """The opaque cursor from a ``Link: <...>; rel="next"`` header, or None when there is none.

    The URL in the header is only parsed, never followed: it must be on the Hugging Face host and
    the same path, carry exactly one cursor of safe characters (at most 1024), and nothing else
    suspicious; otherwise ProviderError("bad_link").
    """
    value = (headers or {}).get(HEADER_LINK)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096:
        raise ProviderError("bad_link")
    for target, rel in _LINK_PART_RE.findall(value):
        if rel != LINK_REL_NEXT:
            continue
        try:
            parts = urlsplit(target)
            port = parts.port
        except ValueError:
            raise ProviderError("bad_link") from None
        if (parts.scheme != "https" or (parts.hostname or "").lower() != API_HOST
                or port is not None or "@" in parts.netloc or parts.path != path
                or parts.fragment):
            raise ProviderError("bad_link")
        cursors = parse_qs(parts.query, keep_blank_values=True).get(PARAM_CURSOR, [])
        if len(cursors) != 1 or not _CURSOR_RE.fullmatch(cursors[0]):
            raise ProviderError("bad_link")
        return cursors[0]
    if _REL_NEXT_RE.search(value):
        raise ProviderError("bad_link")   # a next link that could not be parsed safely
    return None


def allowlist_overview(raw):
    """An Overview from the allowlisted keys, or None if any is missing or malformed."""
    if not isinstance(raw, dict):
        return None
    name, verified = raw.get("name"), raw.get("isVerified")
    counts = [raw.get(k) for k in ("numModels", "numDatasets", "numSpaces")]
    if (not isinstance(name, str) or not _LOGIN_RE.fullmatch(name) or not isinstance(verified, bool)
            or not all(isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in counts)):
        return None
    return Overview(name, verified, *counts)


class HuggingFaceClient:
    """``HuggingFaceClient(http, budget, dry_run=False)`` for live use; the default is a dry run."""

    def __init__(self, http=None, budget=None, user_agent=None, *, dry_run=True):
        if not isinstance(dry_run, bool):
            raise TypeError("dry_run must be True or False")
        user_agent = USER_AGENT if user_agent is None else user_agent
        if not isinstance(user_agent, str) or not user_agent.isprintable() or not user_agent:
            raise ValueError("user_agent must be one printable line")
        self.dry_run = dry_run
        self.rate_limit = RateLimit()
        self._user_agent = user_agent
        self._stopped = None
        if dry_run:
            self._http = None
            self._budget = None
            return
        if not isinstance(budget, CallBudget):
            raise ValueError("live mode needs a CallBudget")
        if http is not None and http.allowed_hosts != ALLOWED_HOSTS:
            raise ValueError("the HTTP client must allow exactly the Hugging Face host")
        self._budget = budget
        self._http = http if http is not None else HttpClient(ALLOWED_HOSTS)

    def __repr__(self):
        return f"HuggingFaceClient(dry_run={self.dry_run})"

    def _get(self, path, params, not_found):
        if self.dry_run:
            raise ProviderError("dry_run")
        if self._stopped is not None:
            raise self._stopped
        try:
            self._budget.consume(1)
        except BudgetExceeded:
            raise ProviderError("budget_exhausted") from None
        headers = {HEADER_ACCEPT: MEDIA_TYPE, HEADER_USER_AGENT: self._user_agent}
        status, data = resp = self._http.get_json(API_BASE + path, params, headers,
                                                  REQUEST_TIMEOUT_SECONDS)
        self.rate_limit = parse_rate_limit(resp.headers)
        if status == 429:
            self._stopped = HFRateLimited(self.rate_limit.reset)
            raise self._stopped
        if status == 401:
            self._stopped = ProviderError("unauthorised")
            raise self._stopped
        if self.rate_limit.remaining == 0:
            # This request succeeded or failed on its own; the next one would be refused.
            self._stopped = HFRateLimited(self.rate_limit.reset)
        if 200 <= status < 300:
            return data, resp.headers
        if status == 404:
            raise ProviderError(not_found)
        if status in (400, 422):
            raise ProviderError("bad_query")
        if 500 <= status < 600:
            raise ProviderError("server_error")
        raise ProviderError("unexpected_status")

    def get_org_overview(self, org) -> Overview:
        if not isinstance(org, str) or not _LOGIN_RE.fullmatch(org):
            raise ProviderError("bad_org")
        data, _ = self._get(OVERVIEW_PATH.format(name=quote(org, safe="")), None, "org_not_found")
        overview = allowlist_overview(data)
        if overview is None:
            raise ProviderError("unexpected_shape")
        return overview

    def list_repos(self, kind, org, expand=(), cursor=None, limit=MAX_PAGE_SIZE) -> ListingPage:
        """One page. ``expand`` is a tuple of fields allowed for ``kind`` (EXPAND_BY_KIND), or
        None for all of them; any other field is refused before sending (``bad_expand``)."""
        if kind not in LIST_PATHS:
            raise ProviderError("bad_kind")
        if not isinstance(org, str) or not _LOGIN_RE.fullmatch(org):
            raise ProviderError("bad_org")
        if expand is None:
            expand = EXPAND_BY_KIND[kind]
        if not isinstance(expand, tuple) or not set(expand) <= set(EXPAND_BY_KIND[kind]):
            raise ProviderError("bad_expand")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ProviderError("bad_limit")
        if cursor is not None and (not isinstance(cursor, str) or not _CURSOR_RE.fullmatch(cursor)):
            raise ProviderError("bad_cursor")
        path = LIST_PATHS[kind]
        params = {PARAM_AUTHOR: org, PARAM_SORT: SORT_LAST_MODIFIED,
                  PARAM_DIRECTION: DIRECTION_DESCENDING, PARAM_LIMIT: min(limit, MAX_PAGE_SIZE)}
        if expand:
            params[PARAM_EXPAND] = expand
        if cursor is not None:
            params[PARAM_CURSOR] = cursor
        data, headers = self._get(path, params, "not_found")
        if not isinstance(data, list):
            raise ProviderError("unexpected_shape")
        return ListingPage(tuple(data), next_cursor(headers, path))

    def list_all(self, kind, org, expand=(), limit=MAX_PAGE_SIZE, max_pages=MAX_PAGES) -> Listing:
        """Up to ``max_pages`` pages (never more than MAX_PAGES). When a next cursor remains after
        the last page, full or not, the result says ``listing_capped`` instead of raising: whether
        a short page can carry a next cursor is unchecked, so any remaining cursor counts."""
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) \
                or not 1 <= max_pages <= MAX_PAGES:
            raise ProviderError("bad_max_pages")
        records, cursor, pages = [], None, 0
        while pages < max_pages:
            page = self.list_repos(kind, org, expand, cursor, limit)
            pages += 1
            records.extend(page.records)
            cursor = page.next_cursor
            if cursor is None:
                return Listing(tuple(records), pages, None)
        return Listing(tuple(records), pages, LISTING_CAPPED)
