"""Filters and Evidence for Hugging Face listing records. Pure: no network, synthetic-testable.

Each record is parsed from its allowlisted keys (``hf_fields``), then kept only if every filter
passes, in order:

- owner: the id is ``ORG/NAME`` with ``ORG`` equal to the target's org (ignoring case), and an
  ``author``, when sent, agrees;
- public: ``private`` is exactly false; gated and disabled items are dropped (an UNCHECKED
  default: how gated and private items look has not been checked live);
- recent: ``lastModified`` is within ``modified_within_days`` of today (UTC), inclusive; a future
  date is dropped;
- term: the ``NAME`` part contains one of the signal's terms, ignoring case (a term found only in
  the owner part is not a match).

A kept record becomes Evidence from a FIXED template built from four checked values: the kind,
the ``org/name``, the term and the last-modified date. The URL is built from a fixed prefix and
the id and is never requested. Every drop is counted under a code in a ``FilterReport``. At most
EVIDENCE_PER_TARGET items per target, deduplicated by URL.
"""
import re
from datetime import date, datetime

from ..models import EVIDENCE_SUMMARY_MAX, Evidence
from .evidence import EvidenceError
from .filters import FilterReport
from .hf_fields import HFRecord, parse_hf_record
from .signal import EVIDENCE_PER_TARGET, HUGGINGFACE, Signal, _TERM_RE

WEB_PREFIX = "https://huggingface.co/"
URL_PREFIXES = {"model": WEB_PREFIX, "dataset": WEB_PREFIX + "datasets/",
                "space": WEB_PREFIX + "spaces/"}
EVIDENCE_KINDS = {"model": "hf-model", "dataset": "hf-dataset", "space": "hf-space"}
NOUNS = {"model": "model", "dataset": "dataset", "space": "Space"}
TEMPLATE = ("The public Hugging Face {noun} {org}/{name} matches the term {term}, "
            "last modified on {modified}.")
OK = (True, None)
_ORG_NAME_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,96}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_URLISH = re.compile(r"(?i)://|www\.")


def _check_today(today):
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")


def check_owner(record, org):
    """The id's owner (and the author, when sent) must be the target's org, ignoring case."""
    want = org.casefold()
    if record.org.casefold() != want:
        return (False, "owner_not_target")
    if record.author is not None and record.author.casefold() != want:
        return (False, "owner_not_target")
    return OK


def check_public(record):
    """Public only. Gated and disabled items are dropped: an unchecked default (see the design)."""
    if record.private is not False:
        return (False, "private")
    if record.gated not in (None, False):
        return (False, "gated")
    if record.disabled not in (None, False):
        return (False, "disabled")
    return OK


def check_recent(record, modified_within_days, today):
    """Last modified no earlier than ``modified_within_days`` days before ``today`` (inclusive)."""
    _check_today(today)
    age = (today - record.last_modified).days
    if age < 0:
        return (False, "modified_in_future")
    return OK if age <= modified_within_days else (False, "outside_window")


def matching_term(record, terms):
    """The first of ``terms`` that the NAME part contains, ignoring case, or None."""
    name = record.name.casefold()
    return next((t for t in terms if t.casefold() in name), None)


def build_hf_evidence(record, term, company_domain, today, *, terms) -> Evidence:
    """One Evidence item from the fixed template, or EvidenceError with a drop code."""
    if not isinstance(record, HFRecord) or record.kind not in EVIDENCE_KINDS:
        raise EvidenceError("bad_record")
    if not isinstance(terms, tuple) or term not in terms or not isinstance(term, str) \
            or not _TERM_RE.fullmatch(term):
        raise EvidenceError("term_not_in_signal")
    org_name = f"{record.org}/{record.name}"
    if not _ORG_NAME_RE.fullmatch(org_name):
        raise EvidenceError("bad_id")
    _check_today(today)
    modified = record.last_modified.isoformat() if isinstance(record.last_modified, date) else ""
    if not _DATE_RE.fullmatch(modified) or record.last_modified > today:
        raise EvidenceError("bad_last_modified")
    summary = TEMPLATE.format(noun=NOUNS[record.kind], org=record.org, name=record.name,
                              term=term, modified=modified)
    if len(summary) > EVIDENCE_SUMMARY_MAX:
        raise EvidenceError("summary_too_long")
    if not summary.isprintable():
        raise EvidenceError("summary_not_printable")
    if "@" in summary:
        raise EvidenceError("summary_has_at")
    if _URLISH.search(summary):
        raise EvidenceError("summary_has_url")
    try:
        return Evidence(company_domain=company_domain, summary=summary,
                        url=URL_PREFIXES[record.kind] + org_name,
                        kind=EVIDENCE_KINDS[record.kind])
    except ValueError:
        raise EvidenceError("bad_evidence") from None


def hf_evidence_for_target(records_by_kind, target, signal, today, report=None):
    """``(tuple of Evidence, FilterReport)`` for one target from its listing records.

    ``records_by_kind`` maps a kind (model, dataset, space) to the raw records its listing
    returned. Kinds are taken in the signal's order, records in listing order.
    """
    if not isinstance(signal, Signal) or signal.source != HUGGINGFACE:
        raise TypeError("signal must be a Hugging Face Signal")
    report = report if report is not None else FilterReport()
    found, seen = [], set()
    for kind in signal.kinds:
        for raw in records_by_kind.get(kind, ()):
            record, code = parse_hf_record(raw, kind)
            if record is None:
                report.drop(code)
                continue
            for ok, code in (check_owner(record, target.org), check_public(record),
                             check_recent(record, signal.modified_within_days, today)):
                if not ok:
                    report.drop(code)
                    break
            else:
                term = matching_term(record, signal.terms)
                if term is None:
                    report.drop("term_not_in_name")
                    continue
                try:
                    item = build_hf_evidence(record, term, target.domain, today,
                                             terms=signal.terms)
                except EvidenceError as e:
                    report.drop(e.code)
                    continue
                if item.url in seen:
                    report.drop("duplicate_url")
                    continue
                if len(found) >= EVIDENCE_PER_TARGET:
                    report.drop("over_target_cap")
                    continue
                seen.add(item.url)
                found.append(item)
                report.keep()
    return tuple(found), report
