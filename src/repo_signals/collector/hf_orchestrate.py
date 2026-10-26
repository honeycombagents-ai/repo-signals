"""Hugging Face orchestration: signal -> plan -> matching -> listings -> filters -> Evidence.

``collect_hf(signal, client, suppression, today)`` takes any client with ``get_org_overview``
and ``list_all`` (see HFCollectorClient). It never builds a client, imports no HTTP code and is
not reachable from any command yet (the live switch is build step 6). It follows ``collect`` in
orchestrate.py and returns the same CollectResult and RunReport.

1. The plan is built first: 1 overview plus HF_PAGES_PER_LISTING pages per kind per target. A
   plan over the budget or the rate ceiling is refused before any client call.
2. Targets are matched with hf_matching (suppressed and unconfirmed targets are never
   requested; an unresolved target is never listed).
3. Each matched target is listed once per kind, in the signal's order, with at most
   HF_PAGES_PER_LISTING pages. A kind whose overview count is zero is skipped without a request.
4. Each target's records go through hf_evidence_for_target: the field allowlist, the filters, at
   most EVIDENCE_PER_TARGET items per target, deduplicated by URL. Every drop is counted.

Stops: a matching stop, or one of STOP_CODES from a listing, stops the run. Evidence built so far
is kept, the rest is recorded as ``not_attempted`` and the run is partial. Any other listing
error is recorded for that listing and the run continues, partial. A ``listing_capped`` listing
keeps its records and marks the run partial without stopping.

In the RunReport, ``matching_requests`` counts overview requests and ``searches_made`` counts
listing pages. ``skipped_searches`` lists every listing that did not complete, as
SkippedSearch(domain, kind_number, code) where ``kind_number`` is the 1-based position of the kind
in the signal's ``kinds``: failed, not attempted, or capped (its records kept). A listing that
raises counts as one page, since the client does not say whether a second page was sent; the
client's own budget remains the hard cap. Nothing here raises for a client problem, and only
codes are recorded, never a value.
"""
import re
from typing import Protocol

from ..budget import HARD_MAX_CALLS
from .filters import FilterReport
from .hf_evidence import hf_evidence_for_target
from .hf_fields import RECORD_KEYS
from .hf_matching import match_hf_targets
from .orchestrate import COMPLETE, PARTIAL, CollectResult, RunReport, SkippedSearch, _refused
from .plan import HF_PAGES_PER_LISTING, PlanError, build_plan
from .signal import HUGGINGFACE, Signal

STOP_CODES = ("unauthorised", "rate_limited", "budget_exhausted", "server_error",
              "unexpected_shape")
LISTING_CAPPED = "listing_capped"
NOT_ATTEMPTED = "not_attempted"
# Codes a client raises BEFORE sending anything: those calls made no request.
NO_REQUEST_CODES = ("budget_exhausted", "dry_run", "bad_org", "bad_kind", "bad_expand",
                    "bad_limit", "bad_cursor", "bad_max_pages")
# Listings ask for exactly the allowlisted record fields (hf_fields), less the id.
EXPAND = tuple(k for k in RECORD_KEYS if k != "id")
COUNT_FIELDS = {"model": "models", "dataset": "datasets", "space": "spaces"}
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class HFCollectorClient(Protocol):
    def get_org_overview(self, org): ...

    def list_all(self, kind, org, expand=(), limit=500, max_pages=HF_PAGES_PER_LISTING): ...


class _ListingError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _code(e):
    code = getattr(e, "code", None)
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "client_error"


class _CountingClient:
    """Counts overview requests and listing pages. Listings come back checked: a malformed
    Listing raises _ListingError("unexpected_shape")."""

    def __init__(self, client):
        self._client = client
        self.overviews = 0
        self.pages = 0

    def get_org_overview(self, org):
        try:
            overview = self._client.get_org_overview(org)
        except Exception as e:
            if getattr(e, "code", None) not in NO_REQUEST_CODES:
                self.overviews += 1
            raise
        self.overviews += 1
        return overview

    def list_all(self, kind, org):
        try:
            listing = self._client.list_all(kind, org, EXPAND, max_pages=HF_PAGES_PER_LISTING)
        except Exception as e:
            if getattr(e, "code", None) not in NO_REQUEST_CODES:
                self.pages += 1
            raise
        pages = getattr(listing, "pages", None)
        records = getattr(listing, "records", None)
        code = getattr(listing, "code", None)
        if (isinstance(pages, bool) or not isinstance(pages, int)
                or not 1 <= pages <= HF_PAGES_PER_LISTING
                or not isinstance(records, (tuple, list)) or code not in (None, LISTING_CAPPED)):
            self.pages += 1
            raise _ListingError("unexpected_shape")
        self.pages += pages
        return tuple(records), code


def _kinds_to_list(match, signal):
    """(kind_number, kind) for each kind in the signal's order whose overview count is not zero."""
    return [(n, kind) for n, kind in enumerate(signal.kinds, 1)
            if getattr(match, COUNT_FIELDS[kind]) != 0]


def collect_hf(signal, client, suppression, today, *, budget_cap=HARD_MAX_CALLS) -> CollectResult:
    if not isinstance(signal, Signal) or signal.source != HUGGINGFACE:
        raise TypeError("signal must be a Hugging Face Signal")
    try:
        plan = build_plan(signal, suppression, budget_cap)
    except PlanError as e:
        return _refused(e)

    counting = _CountingClient(client)
    match_report = match_hf_targets(signal, counting, suppression)
    filter_report = FilterReport()
    evidence, skipped = {}, []
    stop = match_report.stop

    for target, match in zip(signal.targets, match_report.results):
        if not match.usable:
            continue
        domain = match.domain
        records_by_kind = {}
        for number, kind in _kinds_to_list(match, signal):
            if stop is not None:
                skipped.append(SkippedSearch(domain, number, NOT_ATTEMPTED))
                continue
            try:
                records, code = counting.list_all(kind, target.org)
            except Exception as e:  # one listing's problem is recorded, never raised
                code = _code(e)
                skipped.append(SkippedSearch(domain, number, code))
                if code in STOP_CODES:
                    stop = code
                continue
            if code == LISTING_CAPPED:
                skipped.append(SkippedSearch(domain, number, LISTING_CAPPED))
            records_by_kind[kind] = records
        found, _ = hf_evidence_for_target(records_by_kind, target, signal, today, filter_report)
        evidence[domain] = found

    status = PARTIAL if (stop or skipped) else COMPLETE
    run = RunReport(status, stop, None, plan.total, plan.skipped, counting.pages, tuple(skipped),
                    counting.overviews)
    return CollectResult(evidence, match_report, filter_report, run)
