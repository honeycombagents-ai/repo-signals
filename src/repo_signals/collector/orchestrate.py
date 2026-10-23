"""Collector orchestration: signal -> plan -> matching -> searches -> filters -> Evidence.

``collect(signal, client, suppression, budget_cap, today)`` takes any client with ``get_org``,
``search_orgs`` and ``search_repos`` (see CollectorClient). It never builds a client and imports
no HTTP code. ``collect --live`` and ``run --live`` call it after their gates and a typed "yes".

1. The plan is built first. A plan over the budget or the search-rate ceiling is refused before
   any client call.
2. Targets are matched. Suppressed targets are skipped before matching.
3. Each usable target is searched once per term.
4. Every item is parsed from its allowlisted keys and passed through the existing filters.
5. An item becomes Evidence only if the term appears in the repository NAME (ignoring case).
   The drafter quotes "matches the term" verbatim, so it must be literally true; a hit that
   matched only through the description or topics is dropped as ``term_not_in_name``.
6. Evidence is ``repo`` kind only, at most EVIDENCE_PER_TARGET per target, deduplicated by URL.

A client error for one search is recorded and collection continues, except ``rate_limited`` and
``budget_exhausted``, which stop everything; what was collected so far is kept and the result is
marked partial. Nothing here raises for a client problem.
"""
import dataclasses
import re
from datetime import timedelta
from typing import NamedTuple, Protocol

from .evidence import try_build_evidence
from .filters import FilterReport, apply_filters, summarize_report
from .github_fields import parse_repo
from .matching import ERROR, RATE_LIMITED, match_all
from .plan import PlanError, build_plan
from .signal import EVIDENCE_PER_TARGET, Signal

KIND = "repo"
STOP_CODES = ("rate_limited", "budget_exhausted")
# Codes a client raises BEFORE sending anything: those calls made no request.
NO_REQUEST_CODES = ("budget_exhausted", "dry_run", "bad_term", "bad_org", "bad_name",
                    "bad_language", "bad_date")
COMPLETE, PARTIAL, REFUSED = "complete", "partial", "refused"
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class CollectorClient(Protocol):
    def get_org(self, login): ...

    def search_orgs(self, name): ...

    def search_repos(self, term, org, language=None, pushed_after=None): ...


class SkippedSearch(NamedTuple):
    domain: str
    term_number: int      # 1-based position of the term in the signal file
    code: str


class RunReport(NamedTuple):
    status: str                          # complete, partial or refused
    code: str | None                     # the refusal or stop code
    refusal: str | None                  # e.g. "plan_over_budget: 27 planned, 25 allowed"
    planned_requests: int
    suppressed: tuple[str, ...]          # target domains skipped as suppressed
    searches_made: int                   # search requests made
    skipped_searches: tuple[SkippedSearch, ...]
    matching_requests: int = 0           # org lookups and org searches made

    @property
    def requests_made(self) -> int:
        return self.matching_requests + self.searches_made


class CollectResult(NamedTuple):
    evidence_by_domain: dict             # domain -> tuple of Evidence
    match_report: object                 # matching.MatchReport, or None when refused
    filter_report: FilterReport
    run_report: RunReport


def _code(e):
    code = getattr(e, "code", None)
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "client_error"


class _CountingClient:
    """Counts the client calls that made a request (matching and searches separately)."""

    def __init__(self, client):
        self._client = client
        self.matching = 0
        self.searches = 0

    def _call(self, kind, method, *args):
        try:
            result = method(*args)
        except Exception as e:
            if getattr(e, "code", None) not in NO_REQUEST_CODES:
                setattr(self, kind, getattr(self, kind) + 1)
            raise
        setattr(self, kind, getattr(self, kind) + 1)
        return result

    def get_org(self, login):
        return self._call("matching", self._client.get_org, login)

    def search_orgs(self, name):
        return self._call("matching", self._client.search_orgs, name)

    def search_repos(self, term, org, language=None, pushed_after=None):
        return self._call("searches", self._client.search_repos, term, org, language,
                          pushed_after)


def _refused(error):
    run = RunReport(REFUSED, error.code, str(error), 0, (), 0, ())
    return CollectResult({}, None, FilterReport(), run)


def collect(signal, client, suppression, budget_cap, today) -> CollectResult:
    if not isinstance(signal, Signal):
        raise TypeError("signal must be a Signal")
    try:
        plan = build_plan(signal, suppression, budget_cap)
    except PlanError as e:
        return _refused(e)

    active = dataclasses.replace(
        signal, targets=tuple(t for t in signal.targets if t.domain not in plan.skipped))
    client = _CountingClient(client)
    match_report = match_all(active, client)
    filter_report = FilterReport()
    evidence = {}
    skipped = []
    stop = next((r.code for r in match_report.results
                 if r.status in (RATE_LIMITED, ERROR) and r.code in STOP_CODES), None)
    pushed_after = today - timedelta(days=signal.pushed_within_days)

    for match in match_report.usable:
        domain = match.domain
        found = evidence.setdefault(domain, [])
        seen = set()
        for number, term in enumerate(signal.terms, 1):
            if stop is not None:
                skipped.append(SkippedSearch(domain, number, "not_attempted"))
                continue
            try:
                result = client.search_repos(term, match.org_login, signal.language, pushed_after)
            except Exception as e:  # one search's problem is recorded, never raised
                code = _code(e)
                skipped.append(SkippedSearch(domain, number, code))
                if code in STOP_CODES:
                    stop = code
                continue
            items = getattr(result, "items", None)
            if not isinstance(items, (tuple, list)):
                skipped.append(SkippedSearch(domain, number, "unexpected_shape"))
                continue
            for item in items:
                fact = parse_repo(item)
                if fact is None:
                    filter_report.drop("malformed_record")
                    continue
                ok, code = apply_filters(fact, signal, today)
                if not ok:
                    filter_report.drop(code)
                    continue
                if term.casefold() not in fact.name.casefold():
                    filter_report.drop("term_not_in_name")
                    continue
                if fact.html_url in seen:
                    filter_report.drop("duplicate_url")
                    continue
                if len(found) >= EVIDENCE_PER_TARGET:
                    filter_report.drop("over_target_cap")
                    continue
                item_evidence = try_build_evidence(fact, term, KIND, domain, today,
                                                   terms=signal.terms, report=filter_report)
                if item_evidence is not None:
                    seen.add(fact.html_url)
                    found.append(item_evidence)

    match_trouble = any(r.status in (RATE_LIMITED, ERROR) for r in match_report.results)
    status = PARTIAL if (stop or skipped or match_trouble) else COMPLETE
    run = RunReport(status, stop, None, plan.total, plan.skipped, client.searches,
                    tuple(skipped), client.matching)
    return CollectResult({d: tuple(e) for d, e in evidence.items()}, match_report,
                         filter_report, run)


def evidence_lookup_from(result):
    """A function for ``drafting.sheet.fill_drafts``: domain -> tuple of Evidence (or ())."""
    by_domain = dict(result.evidence_by_domain)

    def lookup(domain):
        if not isinstance(domain, str):
            return ()
        return tuple(by_domain.get(domain.lower(), ()))

    return lookup


def summarize(result) -> list[str]:
    """Code-only lines: status, counts, match lines, evidence counts and drop codes."""
    run = result.run_report
    if run.status == REFUSED:
        return [f"collect: refused: {run.refusal}; nothing requested"]
    head = "collect: complete" if run.status == COMPLETE else \
        f"collect: partial ({run.code})" if run.code else "collect: partial"
    lines = [head, f"planned requests: {run.planned_requests}; requests made: "
                   f"{run.requests_made} (matching {run.matching_requests}, searches "
                   f"{run.searches_made})"]
    if run.suppressed:
        lines.append(f"skipped (suppressed): {', '.join(run.suppressed)}")
    counts = result.match_report.counts
    lines.append("matching: " + ", ".join(f"{status} {n}" for status, n in sorted(counts.items())))
    lines += list(result.match_report.lines)
    for domain, items in result.evidence_by_domain.items():
        lines.append(f"evidence: {domain}: {len(items)}")
    lines += summarize_report(result.filter_report)
    for s in run.skipped_searches:
        lines.append(f"search skipped: {s.domain} term {s.term_number}: {s.code}")
    return lines
