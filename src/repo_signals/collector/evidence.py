"""Turn a filtered RepoFact into Evidence, using FIXED templates only.

The summary is built from four checked values and nothing else: the repository full name, the
signal's own term, the primary language and the last push date. The description, README text,
other topics, the owner's personal fields and any other free text are never used: RepoFact does
not even carry them. If any check fails, the hit is dropped with a code.
"""
import re
from datetime import date, datetime

from ..models import EVIDENCE_SUMMARY_MAX, Evidence
from .filters import FULL_NAME_RE, check_owner, check_url
from .github_fields import RepoFact
from .signal import _TERM_RE

KINDS = ("repo", "topic")
TEMPLATES = {
    "repo": "The public repository {full_name} matches the term {term}{language_clause}, "
            "last pushed on {pushed_date}.",
    "topic": "The public repository {full_name} is tagged with the topic {term}"
             "{language_clause}, last pushed on {pushed_date}.",
}
LANGUAGE_CLAUSE = " and its primary language on GitHub is {language}"
MAX_SUMMARY_CHARS = EVIDENCE_SUMMARY_MAX
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LANGUAGE_RE = re.compile(r"(?=.{1,32}\Z)[A-Za-z][A-Za-z0-9+#.-]*(?: [A-Za-z0-9+#.-]+)*")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_URLISH = re.compile(r"(?i)://|www\.")


class EvidenceError(Exception):
    """A dropped hit, described by a short code only."""

    def __init__(self, code: str):
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            code = "unspecified"
        self.code = code
        super().__init__(code)


def build_evidence(fact, term, kind, company_domain, today, *, terms) -> Evidence:
    """One Evidence item from a fixed template, or EvidenceError with a drop code.

    ``terms`` is the signal's own terms: ``term`` must be one of them.
    """
    if not isinstance(fact, RepoFact):
        raise EvidenceError("bad_fact")
    if kind not in KINDS:
        raise EvidenceError("bad_kind")
    if not isinstance(terms, tuple) or term not in terms or not isinstance(term, str) \
            or not _TERM_RE.fullmatch(term):
        raise EvidenceError("term_not_in_signal")
    if not check_owner(fact)[0]:
        raise EvidenceError("owner_not_org")
    if not FULL_NAME_RE.fullmatch(fact.full_name):
        raise EvidenceError("bad_full_name")
    ok, code = check_url(fact)
    if not ok:
        raise EvidenceError(code)
    if fact.language is None:
        clause = ""
    elif _LANGUAGE_RE.fullmatch(fact.language):
        clause = LANGUAGE_CLAUSE.format(language=fact.language)
    else:
        raise EvidenceError("bad_language")
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")
    pushed = fact.pushed_at.isoformat() if isinstance(fact.pushed_at, date) else ""
    if not _DATE_RE.fullmatch(pushed) or fact.pushed_at > today:
        raise EvidenceError("bad_pushed_date")
    summary = TEMPLATES[kind].format(full_name=fact.full_name, term=term,
                                     language_clause=clause, pushed_date=pushed)
    if len(summary) > MAX_SUMMARY_CHARS:
        raise EvidenceError("summary_too_long")
    if not summary.isprintable():
        raise EvidenceError("summary_not_printable")
    if "@" in summary:
        raise EvidenceError("summary_has_at")
    if _URLISH.search(summary):
        raise EvidenceError("summary_has_url")
    try:
        return Evidence(company_domain=company_domain, summary=summary, url=fact.html_url,
                        kind=kind)
    except ValueError:
        raise EvidenceError("bad_evidence") from None


def try_build_evidence(fact, term, kind, company_domain, today, *, terms, report):
    """``build_evidence``, counting the result in ``report``. Returns Evidence or None."""
    try:
        evidence = build_evidence(fact, term, kind, company_domain, today, terms=terms)
    except EvidenceError as e:
        report.drop(e.code)
        return None
    report.keep()
    return evidence
