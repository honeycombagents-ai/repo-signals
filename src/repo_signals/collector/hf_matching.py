"""Match each Hugging Face target to its organisation (design section 4). Pure: not wired.

Hugging Face organisation overviews have no website field, so the only rule is the user's own
confirmation: the target names the org and sets ``confirm_org = true``. One overview request
then checks that the org exists. ``isVerified`` is shown in the report line as supporting
information only and never includes or excludes a target.

Each target gets one outcome, counted under its code:

- ``matched``: confirmed and the overview was found;
- ``unresolved``: the overview raised ``org_not_found``; matching continues with the next target;
- ``suppressed``: the domain is on the suppression list; nothing is requested;
- ``unconfirmed``: defensive, since the signal file already refuses it: a target without an org
  or without ``confirm_org = true`` is never requested.

Any other client error (``unauthorised``, ``rate_limited``, ``budget_exhausted``,
``server_error``, ``unexpected_shape`` or anything else) stops matching. It is returned as the
report's stop code with the results so far, never raised, and every target from that one on is
counted as ``not_attempted``.

Only the Overview's name, isVerified and three counts are read; the bio is never read, kept or
printed. Matching takes any client with ``get_org_overview`` and imports no HTTP code.
"""
import re
from collections import Counter
from typing import NamedTuple, Protocol

from .plan import _suppressed
from .signal import HUGGINGFACE, Signal, _LOGIN_RE

MATCHED, UNRESOLVED, SUPPRESSED, UNCONFIRMED = "matched", "unresolved", "suppressed", "unconfirmed"
NOT_ATTEMPTED = "not_attempted"
STATUSES = (MATCHED, UNRESOLVED, SUPPRESSED, UNCONFIRMED, NOT_ATTEMPTED)
ORG_NOT_FOUND = "org_not_found"

_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class OverviewClient(Protocol):
    """What matching needs from a client. HuggingFaceClient fits; tests use a fake."""

    def get_org_overview(self, org): ...


class HFMatchResult(NamedTuple):
    domain: str
    org: str | None
    status: str
    is_verified: bool | None = None
    models: int | None = None
    datasets: int | None = None
    spaces: int | None = None
    code: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == MATCHED

    @property
    def no_repos(self) -> bool:
        """Matched with zero models, datasets and Spaces: the caller can skip its listings."""
        return self.status == MATCHED and self.models == self.datasets == self.spaces == 0


class HFMatchReport(NamedTuple):
    results: tuple[HFMatchResult, ...]
    counts: dict
    lines: tuple[str, ...]
    stop: str | None = None          # the code that stopped matching, or None

    @property
    def usable(self) -> tuple[HFMatchResult, ...]:
        return tuple(r for r in self.results if r.usable)


def _code(e):
    code = getattr(e, "code", None)
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "client_error"


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _overview_fields(overview, org):
    """(name, is_verified, models, datasets, spaces) from the Overview, or None if malformed."""
    name = getattr(overview, "name", None)
    verified = getattr(overview, "is_verified", None)
    counts = tuple(getattr(overview, k, None) for k in ("models", "datasets", "spaces"))
    if (not isinstance(name, str) or not _LOGIN_RE.fullmatch(name)
            or name.casefold() != org.casefold() or not isinstance(verified, bool)
            or not all(_count(c) for c in counts)):
        return None
    return (name, verified) + counts


def report_line(r) -> str:
    """One line of domains, org names, labels and codes. Never the bio or any other text."""
    if r.status == MATCHED:
        line = (f"{r.domain} -> {r.org} (huggingface, confirmed, "
                f"{'verified' if r.is_verified else 'unverified'}")
        return line + (", no models, datasets or Spaces)" if r.no_repos else ")")
    if r.status == UNRESOLVED:
        return f"{r.domain}: org {r.org} not found on Hugging Face (unresolved)"
    if r.status == SUPPRESSED:
        return f"{r.domain}: suppressed (nothing requested)"
    if r.status == UNCONFIRMED:
        return f"{r.domain}: not confirmed (set org and confirm_org = true; nothing requested)"
    return f"{r.domain}: not matched ({r.code})"


def match_hf_targets(signal, client, suppression=None) -> HFMatchReport:
    """Match every target in order; stop at the first client error other than org_not_found."""
    if not isinstance(signal, Signal) or signal.source != HUGGINGFACE:
        raise TypeError("signal must be a Hugging Face Signal")
    results, stop = [], None
    for target in signal.targets:
        domain, org = target.domain, target.org
        if _suppressed(suppression, domain):
            results.append(HFMatchResult(domain, org, SUPPRESSED))
            continue
        if not org or target.confirm_org is not True:
            results.append(HFMatchResult(domain, org, UNCONFIRMED, code="hf_needs_confirm_org"))
            continue
        try:
            overview = client.get_org_overview(org)
        except Exception as e:  # a client problem is a code, never raised mid-way
            code = _code(e)
            if code == ORG_NOT_FOUND:
                results.append(HFMatchResult(domain, org, UNRESOLVED, code=code))
                continue
            stop = code
            break
        fields = _overview_fields(overview, org)
        if fields is None:
            stop = "unexpected_shape"
            break
        name, verified, models, datasets, spaces = fields
        results.append(HFMatchResult(domain, name, MATCHED, verified, models, datasets, spaces))
    counts = Counter(r.status for r in results)
    lines = [report_line(r) for r in results]
    if stop is not None:
        counts[NOT_ATTEMPTED] = len(signal.targets) - len(results)
        lines.append(f"matching stopped ({stop}): {counts[NOT_ATTEMPTED]} target(s) not "
                     "attempted")
    return HFMatchReport(tuple(results), dict(counts), tuple(lines), stop)
