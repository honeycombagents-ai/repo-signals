"""SheetWriter: append people to a Google Sheet, never overwriting or deleting anything.

- The tool owns exactly the columns in ``COLUMNS``, in that order.
- On each write it checks the header row first. An empty sheet gets the headers. A sheet whose
  headers differ is REFUSED (``header_mismatch``): someone else's data is never overwritten.
- Rows are only appended. A person already in the sheet (same email or, when the email is
  empty, same name and company_domain) is skipped: never duplicated, never overwritten.
- email_draft and reviewed are written empty and never written again.
- Every cell built from external data passes the formula-injection guard: a value starting with
  = + - @ (or a tab or carriage return) that is not a plain number gets a leading single quote.
- Dry run (the default) never touches the network. It prints what WOULD be written, with
  emails masked, and writes nothing. A live write needs dry run off, a sheet id, a credentials
  path and a CallBudget; otherwise it stops with a code-only error.
"""
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import NamedTuple

from ..budget import CallBudget
from ..config import Settings
from ..models import Person
from ..providers.base import ProviderError
from . import google_api as g
from .auth import ServiceAccountAuth
from .client import DEFAULT_TAB, SheetsClient, build_http
from .layout import COLUMNS, LAST_COLUMN, PROTECTED_COLUMNS

__all__ = ["COLUMNS", "PROTECTED_COLUMNS", "SheetWriter", "WriteResult", "guard_cell",
           "mask_email"]

FORMULA_PREFIXES = ("=", "+", "-", "@")
CONTROL_PREFIXES = ("\t", "\r", "\n")
MAX_CELL_CHARS = 50000
DRY_RUN_PREVIEW_ROWS = 3
DRY_RUN_SHOWN = ("name", "email", "title", "company_domain", "source", "confidence",
                 "signal_evidence")
_PLAIN_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_I = {name: i for i, name in enumerate(COLUMNS)}


class WriteResult(NamedTuple):
    appended: int
    skipped: int
    dry_run: bool


def guard_cell(value: str) -> str:
    """Quote a value that a spreadsheet could read as a formula. Plain numbers pass."""
    if not isinstance(value, str):
        raise TypeError("cell values are strings")
    value = value[:MAX_CELL_CHARS]
    if value.startswith(CONTROL_PREFIXES) or (
            value.lstrip().startswith(FORMULA_PREFIXES)
            and not _PLAIN_NUMBER.fullmatch(value.strip())):
        return "'" + value
    return value


def mask_email(email: str) -> str:
    """``j***@example.invalid`` for display. Empty stays empty."""
    if not email:
        return ""
    local, at, domain = email.partition("@")
    if not at or not local or not domain:
        return "***"
    return local[0] + "***@" + domain


def _display(value, width=40):
    text = "".join(c if c.isprintable() else "?" for c in str(value))
    return text if len(text) <= width else text[:width - 3] + "..."


def _key(row):
    row = list(row) + [""] * (len(COLUMNS) - len(row))
    email = str(row[_I["email"]]).strip().lower()
    if email:
        return ("email", email)
    return ("name", str(row[_I["name"]]).strip().casefold(),
            str(row[_I["company_domain"]]).strip().casefold())


def _name_key(row):
    row = list(row) + [""] * (len(COLUMNS) - len(row))
    return ("name", str(row[_I["name"]]).strip().casefold(),
            str(row[_I["company_domain"]]).strip().casefold())


class SheetWriter:
    def __init__(self, settings, *, budget=None, http=None, signer=None, client=None,
                 out=None, clock=None, run_id=None, tab=DEFAULT_TAB):
        if not isinstance(settings, Settings):
            raise TypeError("settings must be a Settings")
        if budget is not None and not isinstance(budget, CallBudget):
            raise TypeError("budget must be a CallBudget")
        if http is not None and http.allowed_hosts != g.ALLOWED_HOSTS:
            raise ValueError("the HTTP client must allow exactly the Google Sheets hosts")
        run_id = run_id if run_id is not None else uuid.uuid4().hex[:12]
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("run_id must be letters, digits, '-' or '_'")
        self.run_id = run_id
        self._settings = settings
        self._budget = budget
        self._http = http
        self._signer = signer
        self._client = client
        self._out = out
        self._clock = clock
        self._tab = tab

    def __repr__(self):
        return f"SheetWriter(dry_run={self._settings.dry_run})"

    def _print(self, text):
        print(text, file=self._out if self._out is not None else sys.stdout)

    def _now(self):
        now = self._clock() if self._clock is not None else datetime.now(UTC)
        return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def build_row(self, person, signal_evidence, written_at):
        if not isinstance(person, Person) or not isinstance(signal_evidence, str):
            raise ValueError("each record is a Person and a signal_evidence string")
        cells = {
            "name": guard_cell(person.name), "email": guard_cell(person.email or ""),
            "title": guard_cell(person.title), "company_domain": guard_cell(person.company_domain),
            "source": guard_cell(person.source), "confidence": round(person.confidence, 3),
            "signal_evidence": guard_cell(signal_evidence),
            "email_draft": "", "reviewed": "",
            "run_id": guard_cell(self.run_id), "written_at": guard_cell(written_at),
        }
        return [cells[c] for c in COLUMNS]

    def _rows(self, records):
        written_at = self._now()
        rows = []
        for rec in records:
            person, evidence = (rec, "") if isinstance(rec, Person) else rec
            rows.append(self.build_row(person, evidence, written_at))
        return rows

    @staticmethod
    def _new_only(rows, existing):
        emails = {k for k in map(_key, existing) if k[0] == "email"}
        names = {_name_key(r) for r in existing}
        keep, skipped = [], 0
        for row in rows:
            k = _key(row)
            if (k in emails) if k[0] == "email" else (k in names):
                skipped += 1
                continue
            keep.append(row)
            if k[0] == "email":
                emails.add(k)
            names.add(_name_key(row))
        return keep, skipped

    def _live_client(self):
        s = self._settings
        if not s.sheet_id:
            raise ProviderError("sheet_id_missing")
        if not s.google_credentials:
            raise ProviderError("credentials_missing")
        if self._budget is None:
            raise ProviderError("budget_missing")
        if self._client is None:
            http = self._http if self._http is not None else build_http()
            auth = ServiceAccountAuth(s.google_credentials, http=http, budget=self._budget,
                                      signer=self._signer)
            self._client = SheetsClient(s.sheet_id, auth=auth, budget=self._budget, http=http,
                                        tab=self._tab)
        return self._client

    def _dry_run(self, rows, skipped):
        self._print(f"sheets: dry run, would write {len(rows)} row(s) "
                    f"({skipped} duplicate(s) in this batch skipped; the sheet itself is not "
                    "read in a dry run); nothing written")
        if not rows:
            return
        idx = [_I[c] for c in DRY_RUN_SHOWN]
        self._print(" | ".join(DRY_RUN_SHOWN))
        for row in rows[:DRY_RUN_PREVIEW_ROWS]:
            shown = list(row)
            shown[_I["email"]] = mask_email(str(row[_I["email"]]))
            self._print(" | ".join(_display(shown[i]) for i in idx))
        if len(rows) > DRY_RUN_PREVIEW_ROWS:
            self._print(f"... and {len(rows) - DRY_RUN_PREVIEW_ROWS} more row(s)")

    def write(self, records) -> WriteResult:
        rows, skipped = self._new_only(self._rows(records), [])
        if self._settings.dry_run:
            self._dry_run(rows, skipped)
            return WriteResult(0, skipped, True)
        client = self._live_client()
        header = client.read_header()
        if header and header[:len(COLUMNS)] != list(COLUMNS):
            raise ProviderError("header_mismatch")
        existing = client.read_rows()
        if not header:
            if any(any(str(c).strip() for c in r) for r in existing):
                raise ProviderError("header_missing")
            client.update_cells(f"A1:{LAST_COLUMN}1", [list(COLUMNS)])
        rows, more = self._new_only(rows, existing)
        skipped += more
        if rows:
            client.append_rows(rows)
        self._print(f"sheets: appended {len(rows)} row(s), skipped {skipped} duplicate(s)")
        return WriteResult(len(rows), skipped, False)
