"""The request plan: what a collection run WOULD request, as data only. No HTTP here.

A target is skipped (and reported) when its domain is on the suppression list, either as a
listed domain or as the domain of a listed email address. For each other target:

- a target that names its org: one org lookup (core);
- a domain-only target: one candidate search (search) plus up to MAX_CANDIDATES_CHECKED org
  lookups (core) to check candidates' websites;
- for every term: one repository search (search).

The plan is an upper bound. It is REFUSED, before anything runs, when its request count exceeds
the budget cap or its search count exceeds the search-rate ceiling.
"""
from types import SimpleNamespace
from typing import NamedTuple

from ..budget import HARD_MAX_CALLS
from .signal import Signal, SignalError

# UNVERIFIED: GitHub's search limit with a token, as general knowledge.
# Verify against GitHub's current documentation before first live use.
SEARCH_RATE_CEILING_PER_MINUTE = 30
MAX_CANDIDATES_CHECKED = 3

ORG_LOOKUP = "org_lookup"
CANDIDATE_SEARCH = "candidate_search"
REPO_SEARCH = "repo_search"
CORE, SEARCH = "core", "search"


class PlanError(SignalError):
    """A refused plan: a code plus the counts (numbers only, never file content)."""

    def __init__(self, code: str, planned: int, allowed: int):
        super().__init__(code)
        self.planned = int(planned)
        self.allowed = int(allowed)
        self.args = (f"{self.code}: {self.planned} planned, {self.allowed} allowed",)

    def __str__(self):
        return self.args[0]


class PlannedRequest(NamedTuple):
    kind: str
    resource: str
    target: int          # 1-based position in the signal file
    domain: str
    org: str | None = None
    term: str | None = None
    candidate: int | None = None

    def describe(self) -> str:
        """A synthetic description. Never a URL."""
        if self.kind == ORG_LOOKUP and self.candidate is None:
            return f"org lookup: {self.org} (for {self.domain})"
        if self.kind == ORG_LOOKUP:
            return f"org lookup: candidate {self.candidate} for {self.domain}"
        if self.kind == CANDIDATE_SEARCH:
            return f"candidate search: organisations for {self.domain}"
        where = self.org if self.org else f"the org matched for {self.domain}"
        return f"repository search: term {self.term} in {where}"


class Plan(NamedTuple):
    signal_name: str
    requests: tuple[PlannedRequest, ...]
    skipped: tuple[str, ...]        # suppressed target domains
    targets_planned: int

    @property
    def total(self) -> int:
        return len(self.requests)

    @property
    def search_count(self) -> int:
        return sum(1 for r in self.requests if r.resource == SEARCH)

    @property
    def core_count(self) -> int:
        return sum(1 for r in self.requests if r.resource == CORE)


def _suppressed(suppression, domain):
    """True when the domain, or a parent of it, is a listed domain or the domain of a listed
    email address. Skipping a whole target for one listed address is deliberately cautious."""
    if suppression is None:
        return False
    if suppression.matches(SimpleNamespace(email=None, company_domain=domain)):
        return True
    email_domains = {e.rpartition("@")[2] for e in suppression.emails}
    return any(domain == d or domain.endswith("." + d) for d in email_domains)


def build_plan(signal, suppression=None, budget_cap=HARD_MAX_CALLS) -> Plan:
    """Plan a run. Raise PlanError (and run nothing) when the plan does not fit."""
    if not isinstance(signal, Signal):
        raise TypeError("signal must be a Signal")
    if isinstance(budget_cap, bool) or not isinstance(budget_cap, int) \
            or not 1 <= budget_cap <= HARD_MAX_CALLS:
        raise ValueError(f"budget_cap must be from 1 to {HARD_MAX_CALLS}")
    requests, skipped = [], []
    for n, target in enumerate(signal.targets, 1):
        if _suppressed(suppression, target.domain):
            skipped.append(target.domain)
            continue
        if target.org:
            requests.append(PlannedRequest(ORG_LOOKUP, CORE, n, target.domain, target.org))
        else:
            requests.append(PlannedRequest(CANDIDATE_SEARCH, SEARCH, n, target.domain))
            for c in range(1, MAX_CANDIDATES_CHECKED + 1):
                requests.append(PlannedRequest(ORG_LOOKUP, CORE, n, target.domain, candidate=c))
        for term in signal.terms:
            requests.append(PlannedRequest(REPO_SEARCH, SEARCH, n, target.domain, target.org,
                                           term))
    plan = Plan(signal.name, tuple(requests), tuple(skipped),
                len(signal.targets) - len(skipped))
    if plan.total > budget_cap:
        raise PlanError("plan_over_budget", plan.total, budget_cap)
    if plan.search_count > SEARCH_RATE_CEILING_PER_MINUTE:
        raise PlanError("plan_over_search_rate", plan.search_count,
                        SEARCH_RATE_CEILING_PER_MINUTE)
    return plan
