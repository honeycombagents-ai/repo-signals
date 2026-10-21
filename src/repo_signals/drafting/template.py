"""TemplateDrafter: the default drafter. No model, no network: plain-text templates only.

A template file is::

    Subject: <subject line>
    <blank line>
    <body>

Only these placeholders exist: {first_name}, {title}, {company_domain}, {evidence_summary},
{evidence_url}, {sender_name}, {sender_role}, {org_name}, {postal_address}, {optout_text}.
The subject may use only {company_domain}. The body must use {evidence_summary},
{evidence_url}, {postal_address} and {optout_text}. Any other brace is an error.

Rendering is strict: an unknown placeholder, a missing value, or a newline in a single-line
field raises DraftError. Values are inserted once, as plain text: a value that looks like a
placeholder is not expanded.
"""
import re
from pathlib import Path
from typing import NamedTuple

from .base import (Draft, DraftError, first_name, relevant_evidence, require_sender)
from .guard import MAX_BODY_CHARS, check_draft

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
BUILTIN_TEMPLATES = ("intro", "follow_up")
ALLOWED_PLACEHOLDERS = ("first_name", "title", "company_domain", "evidence_summary",
                        "evidence_url", "sender_name", "sender_role", "org_name",
                        "postal_address", "optout_text")
SUBJECT_PLACEHOLDERS = ("company_domain",)
REQUIRED_BODY_PLACEHOLDERS = ("evidence_summary", "evidence_url", "postal_address",
                              "optout_text")
MULTILINE_FIELDS = ("postal_address", "optout_text")
MAX_TEMPLATE_BYTES = 16 * 1024
SUBJECT_PREFIX = "Subject: "
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


class Template(NamedTuple):
    name: str
    subject: str
    body: str


def placeholders(text) -> list[str]:
    """Every placeholder name in ``text``. Raise on an unknown one or a stray brace."""
    names = [m.group(1) for m in _PLACEHOLDER.finditer(text)]
    for n in names:
        if n not in ALLOWED_PLACEHOLDERS:
            raise DraftError("unknown_placeholder")
    if "{" in _PLACEHOLDER.sub("", text) or "}" in _PLACEHOLDER.sub("", text):
        raise DraftError("template_malformed")
    return names


def parse_template(text, name="custom") -> Template:
    if not isinstance(text, str) or "\r" in text or "\0" in text:
        raise DraftError("template_malformed")
    lines = text.split("\n")
    if len(lines) < 3 or not lines[0].startswith(SUBJECT_PREFIX) or lines[1] != "":
        raise DraftError("template_malformed")
    subject = lines[0][len(SUBJECT_PREFIX):].strip()
    body = "\n".join(lines[2:]).strip("\n")
    if not subject or not body.strip():
        raise DraftError("template_malformed")
    if not set(placeholders(subject)) <= set(SUBJECT_PLACEHOLDERS):
        raise DraftError("subject_placeholder_not_allowed")
    if not set(REQUIRED_BODY_PLACEHOLDERS) <= set(placeholders(body)):
        raise DraftError("template_missing_required_placeholder")
    return Template(name, subject, body)


def load_template(name="intro", path=None) -> Template:
    """A built-in template by name, or your own template file by path."""
    if path is None:
        if name not in BUILTIN_TEMPLATES:
            raise DraftError("unknown_template")
        path = TEMPLATE_DIR / f"{name}.txt"
    elif not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        name = "custom"
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_TEMPLATE_BYTES + 1)
    except (OSError, TypeError, ValueError):
        raise DraftError("template_unreadable") from None
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise DraftError("template_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DraftError("template_malformed") from None
    return parse_template(text, name)


def render(text, values) -> str:
    """Fill placeholders in one pass. Values are plain text, never re-expanded."""
    placeholders(text)

    def fill(m):
        field = m.group(1)
        value = values.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DraftError(f"missing_{field}")
        if field not in MULTILINE_FIELDS and ("\n" in value or "\r" in value):
            raise DraftError(f"newline_in_{field}")
        if not all(c.isprintable() or (c == "\n" and field in MULTILINE_FIELDS) for c in value):
            raise DraftError(f"control_character_in_{field}")
        return value

    return _PLACEHOLDER.sub(fill, text)


class TemplateDrafter:
    """``TemplateDrafter("intro")``, ``TemplateDrafter("follow_up")`` or
    ``TemplateDrafter(path="my_template.txt")``. Every draft passes the guard."""

    name = "template"

    def __init__(self, template="intro", *, path=None, max_body_chars=MAX_BODY_CHARS):
        self.template = load_template(template, path)
        self._max_body_chars = max_body_chars

    def __repr__(self):
        return f"TemplateDrafter({self.template.name!r})"

    def draft(self, person, evidence, sender) -> Draft:
        first = first_name(person)
        relevant = relevant_evidence(person, evidence)
        require_sender(sender)
        ev = relevant[0]
        values = {
            "first_name": first, "title": person.title, "company_domain": person.company_domain,
            "evidence_summary": ev.summary, "evidence_url": ev.url,
            "sender_name": sender.name, "sender_role": sender.role, "org_name": sender.org_name,
            "postal_address": sender.postal_address, "optout_text": sender.optout_text,
        }
        draft = Draft(render(self.template.subject, values), render(self.template.body, values),
                      (ev.url,))
        return check_draft(draft, person, evidence, sender, max_body_chars=self._max_body_chars)
