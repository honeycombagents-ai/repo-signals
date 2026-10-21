"""The guard every draft passes, whichever Drafter wrote it.

A draft is rejected (code-only DraftError) unless:

- the person has a real name and there is evidence about their company;
- every sender detail is set (postal address and opt-out text included);
- ``grounded_on`` names only evidence URLs for that company, and the body contains each of
  those URLs and quotes each one's summary verbatim;
- the body contains the sender's postal address and opt-out text;
- no URL appears other than the evidence URLs (and any URL inside the opt-out text);
- the subject is one short line, has no URL, and names the company domain or an evidence kind;
- nothing claims a relationship, meeting or prior contact that the evidence does not mention;
- the subject and body are within their length caps.
"""
import re

from .base import (Draft, DraftError, first_name, relevant_evidence, require_sender)

MAX_BODY_CHARS = 2000
MAX_SUBJECT_CHARS = 120
_URL_RE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"'`]+")
_TRAILING = ".,;:!?)]}>*"
# Phrases that claim a relationship, meeting or prior contact. Allowed only when an evidence
# summary itself contains the phrase.
RELATIONSHIP_PHRASES = (
    "as we discussed", "as discussed", "as promised", "as i mentioned", "as mentioned",
    "following up on our", "follow up on our", "our conversation", "our call", "our meeting",
    "our chat", "we met", "we spoke", "when we spoke", "great to meet", "nice to meet",
    "good to meet", "meeting you", "you asked", "you requested", "your request", "per our",
    "last time we", "thanks for your time", "thank you for your time", "reconnect",
    "catch up", "circling back", "circle back", "touching base", "touch base",
    "our partnership", "existing customer", "your account with us", "you signed up",
    "you downloaded", "you reached out", "you contacted", "referred me", "introduced us",
    "mutual friend", "in our last", "since we last")


def _urls(text):
    return {m.group(0).rstrip(_TRAILING) for m in _URL_RE.finditer(text)}


def _clean_text(text, multiline):
    return isinstance(text, str) and all(
        c.isprintable() or (multiline and c == "\n") for c in text)


def check_draft(draft, person, evidence, sender, *, max_body_chars=MAX_BODY_CHARS,
                max_subject_chars=MAX_SUBJECT_CHARS) -> Draft:
    """Return ``draft`` if it passes every rule; otherwise raise a code-only DraftError."""
    first_name(person)
    relevant = relevant_evidence(person, evidence)
    require_sender(sender)
    if not isinstance(draft, Draft) or not isinstance(draft.grounded_on, tuple):
        raise DraftError("bad_draft")
    subject, body = draft.subject, draft.body
    if not _clean_text(subject, False) or not subject.strip():
        raise DraftError("subject_malformed")
    if not _clean_text(body, True) or not body.strip():
        raise DraftError("body_malformed")
    if len(subject) > max_subject_chars:
        raise DraftError("subject_too_long")
    if len(body) > max_body_chars:
        raise DraftError("body_too_long")
    by_url = {e.url: e for e in relevant}
    if not draft.grounded_on or not all(u in by_url for u in draft.grounded_on):
        raise DraftError("ungrounded")
    for url in draft.grounded_on:
        if url not in body:
            raise DraftError("evidence_url_missing")
        if by_url[url].summary not in body:
            raise DraftError("evidence_summary_missing")
    if sender.postal_address not in body:
        raise DraftError("postal_address_missing")
    if sender.optout_text not in body:
        raise DraftError("optout_missing")
    allowed = {u.rstrip(_TRAILING) for u in by_url} | _urls(sender.optout_text)
    if _urls(subject):
        raise DraftError("url_in_subject")
    if not _urls(body) <= allowed:
        raise DraftError("foreign_url")
    summaries = " ".join(e.summary for e in relevant).lower()
    text = (subject + "\n" + body).lower()
    if any(p in text and p not in summaries for p in RELATIONSHIP_PHRASES):
        raise DraftError("relationship_claim")
    anchors = {person.company_domain.lower()} | {e.kind.lower() for e in relevant}
    if not any(a in subject.lower() for a in anchors):
        raise DraftError("subject_ungrounded")
    return draft


class GuardedDrafter:
    """Wraps any Drafter so that every draft it returns has passed ``check_draft``."""

    def __init__(self, inner, *, max_body_chars=MAX_BODY_CHARS):
        if not callable(getattr(inner, "draft", None)):
            raise TypeError("inner must be a Drafter")
        self.inner = inner
        self.name = getattr(inner, "name", "drafter")
        self._max_body_chars = max_body_chars

    def __repr__(self):
        return f"GuardedDrafter({self.name!r})"

    def draft(self, person, evidence, sender) -> Draft:
        # Refuse before calling the inner drafter, so it never sees an ungrounded request.
        first_name(person)
        relevant_evidence(person, evidence)
        require_sender(sender)
        return check_draft(self.inner.draft(person, evidence, sender), person, evidence, sender,
                           max_body_chars=self._max_body_chars)
