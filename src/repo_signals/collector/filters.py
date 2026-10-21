"""Filters on RepoFacts. Each is a pure function returning ``(ok, code)``.

A record is kept only if every filter passes. A dropped record is counted under its code in a
FilterReport: nothing is dropped silently.
"""
import re
from collections import Counter
from datetime import date, datetime
from urllib.parse import urlsplit

from .github_fields import ORGANIZATION, WEB_HOST, parse_repo
from .signal import Signal

OK = (True, None)
# owner (GitHub login shape, unverified) / repository name (letters, digits, . _ -)
FULL_NAME_RE = re.compile(r"(?=[^/]{1,39}/)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/"
                          r"(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,100}")


def _drop(code):
    return (False, code)


def check_owner(fact):
    """Only organisation-owned repositories: a user-owned repository identifies a person."""
    return OK if fact.owner_type == ORGANIZATION else _drop("owner_not_org")


def check_not_fork(fact):
    return OK if fact.fork is False else _drop("fork")


def check_not_archived(fact):
    return OK if fact.archived is False else _drop("archived")


def check_not_private(fact):
    return OK if fact.private is False else _drop("private")


def check_full_name(fact):
    if not FULL_NAME_RE.fullmatch(fact.full_name) \
            or fact.full_name != f"{fact.owner_login}/{fact.name}":
        return _drop("bad_full_name")
    return OK


def check_url(fact):
    """https, on the GitHub web host, with the path exactly /owner/name for this full_name."""
    try:
        parts = urlsplit(fact.html_url)
        port = parts.port
    except ValueError:
        return _drop("url_malformed")
    if parts.scheme != "https":
        return _drop("url_not_https")
    if parts.netloc != WEB_HOST or port is not None:
        return _drop("url_other_host")
    if parts.query or parts.fragment:
        return _drop("url_extra_path")
    segments = parts.path.split("/")[1:]
    if len(segments) != 2 or not all(segments):
        return _drop("url_extra_path")
    if parts.path != "/" + fact.full_name:
        return _drop("url_full_name_mismatch")
    return OK


def _check_today(today):
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")


def check_window(fact, pushed_within_days, today):
    """Pushed no earlier than ``pushed_within_days`` days before ``today`` (inclusive)."""
    _check_today(today)
    age = (today - fact.pushed_at).days
    if age < 0:
        return _drop("pushed_in_future")
    return OK if age <= pushed_within_days else _drop("outside_window")


def check_language(fact, language):
    if language is None:
        return OK
    if fact.language is not None and fact.language.casefold() == language.casefold():
        return OK
    return _drop("language_mismatch")


def apply_filters(fact, signal, today):
    """The first failing filter's code, or OK."""
    if not isinstance(signal, Signal):
        raise TypeError("signal must be a Signal")
    for result in (check_owner(fact), check_not_fork(fact), check_not_archived(fact),
                   check_not_private(fact), check_full_name(fact), check_url(fact),
                   check_window(fact, signal.pushed_within_days, today),
                   check_language(fact, signal.language)):
        if not result[0]:
            return result
    return OK


class FilterReport:
    """Counts of kept records and of dropped records per code. Holds no record content."""

    def __init__(self):
        self.kept = 0
        self.dropped = Counter()

    def keep(self):
        self.kept += 1

    def drop(self, code):
        self.dropped[code] += 1

    def __repr__(self):
        return f"FilterReport(kept={self.kept}, dropped={sum(self.dropped.values())})"


def summarize_report(report) -> list[str]:
    """Code-only lines: the kept count, then each drop code with its count."""
    lines = [f"kept: {report.kept}"]
    lines += [f"dropped {n}: {code}" for code, n in sorted(report.dropped.items())]
    return lines


def filter_records(raws, signal, today, report=None):
    """Parse and filter raw records. Returns ``(facts, report)``; every drop is counted."""
    report = report if report is not None else FilterReport()
    facts = []
    for raw in raws:
        fact = parse_repo(raw)
        if fact is None:
            report.drop("malformed_record")
            continue
        ok, code = apply_filters(fact, signal, today)
        if not ok:
            report.drop(code)
            continue
        report.keep()
        facts.append(fact)
    return facts, report
