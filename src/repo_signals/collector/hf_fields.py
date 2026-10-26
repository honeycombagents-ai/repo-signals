"""Parse one Hugging Face listing record into an HFRecord, reading ONLY an allowlist of keys.

From a listing record only ``id``, ``author`` (where present), ``lastModified``, ``private``,
``gated`` and ``disabled`` are read. Everything else (descriptions, tags, downloads, likes, card
data, file lists and any other field) is never read, so it can never reach Evidence. Pure: no
network, and no exception for a malformed record, which comes back with a drop code instead.

The record shapes come from the live check of 2026-10-26 (docs/huggingface-design.md, section 10):
dates are ISO 8601 UTC with milliseconds and a trailing Z, and ids are ``ORG/NAME``.
"""
import re
from dataclasses import dataclass
from datetime import date

KINDS = ("model", "dataset", "space")
RECORD_KEYS = ("id", "author", "lastModified", "private", "gated", "disabled")
# ORG: letters, digits and single inner hyphens (the GitHub login shape, UNCHECKED for Hugging
# Face). NAME: letters, digits and . _ - (not "." or ".."), at most 96 characters (unchecked).
_ID_RE = re.compile(r"(?P<org>(?=[^/]{1,39}/)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)/"
                    r"(?P<name>(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,96})")
_MODIFIED_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                          r"(?:\.[0-9]{1,6})?Z")


@dataclass(frozen=True)
class HFRecord:
    kind: str                 # model, dataset or space (from the listing that returned it)
    org: str                  # the ORG part of the id
    name: str                 # the NAME part of the id
    author: str | None        # only when the listing sent it
    last_modified: date       # the UTC date of lastModified
    private: object           # as sent: only False passes the public filter
    gated: object             # as sent, or None when absent
    disabled: object          # as sent, or None when absent


def parse_hf_record(raw, kind):
    """``(HFRecord, None)``, or ``(None, code)`` for a record that cannot be used. Never raises."""
    try:
        if kind not in KINDS:
            return None, "bad_kind"
        if not isinstance(raw, dict):
            return None, "malformed_record"
        record_id = raw.get("id")
        if not isinstance(record_id, str) or not record_id:
            return None, "missing_id"
        modified = raw.get("lastModified")
        if not isinstance(modified, str) or not modified:
            return None, "missing_last_modified"
        m = _ID_RE.fullmatch(record_id)
        if not m:
            return None, "bad_id"
        d = _MODIFIED_RE.fullmatch(modified)
        if not d:
            return None, "bad_last_modified"
        try:
            day = date(int(d.group(1)), int(d.group(2)), int(d.group(3)))
        except ValueError:
            return None, "bad_last_modified"
        author = raw.get("author")
        if author is not None and not isinstance(author, str):
            return None, "malformed_record"
        return HFRecord(kind=kind, org=m.group("org"), name=m.group("name"), author=author,
                        last_modified=day, private=raw.get("private"), gated=raw.get("gated"),
                        disabled=raw.get("disabled")), None
    except Exception:  # never raise for a malformed record, whatever its shape
        return None, "malformed_record"
