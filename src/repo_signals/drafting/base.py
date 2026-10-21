"""The drafting interface: Drafter, Sender, Draft, DraftError and the shared rules.

Nothing in the drafting package sends anything or touches a network. A draft is text for a
person to review.
"""
import os
import re
from dataclasses import dataclass, fields
from typing import NamedTuple, Protocol, runtime_checkable

from .. import config
from ..models import Evidence, Person

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VAR = re.compile(r"REPO_SIGNALS_[A-Z0-9_]{1,50}")


class DraftError(Exception):
    """A drafting refusal, described by a short code and, at most, a variable NAME.

    The message is ``code`` or ``code: VARIABLE_NAME``. It never holds a value: not a sender
    detail, an email, a summary or a path. Anything that is not a plain code becomes
    ``unspecified``; a ``var`` that is not a REPO_SIGNALS_ variable name is dropped.
    """

    def __init__(self, code: str, var: str | None = None):
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            code = "unspecified"
        if not isinstance(var, str) or not _VAR.fullmatch(var):
            var = None
        self.code = code
        self.var = var
        super().__init__(code if var is None else f"{code}: {var}")


@dataclass(frozen=True, repr=False)
class Sender:
    """Who the draft is from. Every field comes from the environment (see config)."""

    name: str | None = None
    role: str | None = None
    org_name: str | None = None
    postal_address: str | None = None
    optout_text: str | None = None

    def __repr__(self):
        shown = ", ".join(
            f"{f.name}={'<unset>' if not getattr(self, f.name) else '<set>'}"
            for f in fields(self))
        return f"Sender({shown})"

    __str__ = __repr__


SENDER_FIELD_VARS = {"name": config.SENDER_NAME, "role": config.SENDER_ROLE,
                     "org_name": config.ORG_NAME, "postal_address": config.POSTAL_ADDRESS,
                     "optout_text": config.OPTOUT_TEXT}


@dataclass(frozen=True)
class Draft:
    """A draft for a person to review. ``grounded_on`` lists the evidence URLs it uses."""

    subject: str
    body: str
    grounded_on: tuple[str, ...]

    def __repr__(self):
        return f"Draft(grounded_on={len(self.grounded_on)} url(s))"


class DraftResult(NamedTuple):
    """``status`` is ``drafted``, ``suppressed`` or ``refused`` (with ``code``)."""

    status: str
    draft: Draft | None = None
    code: str | None = None


@runtime_checkable
class Drafter(Protocol):
    name: str

    def draft(self, person: Person, evidence: tuple[Evidence, ...], sender: Sender) -> Draft:
        ...


def sender_from_env(environ=None) -> Sender:
    """Read the five sender variables. Missing ones stay None (drafting then refuses)."""
    environ = os.environ if environ is None else environ
    values = {}
    for field, var in SENDER_FIELD_VARS.items():
        try:
            values[field] = config.parse_sender_var(environ, var)
        except config.ConfigError:
            raise DraftError("sender_malformed", var) from None
    return Sender(**values)


def require_sender(sender) -> None:
    """The compliance guard on the sender: every field set, or refuse naming the variable."""
    if not isinstance(sender, Sender):
        raise DraftError("sender_missing")
    for field, var in SENDER_FIELD_VARS.items():
        value = getattr(sender, field)
        if not isinstance(value, str) or not value.strip():
            raise DraftError("sender_missing", var)


def first_name(person) -> str:
    """The first word of the person's name; refuse when there is no real name."""
    name = getattr(person, "name", None)
    word = name.split()[0] if isinstance(name, str) and name.split() else ""
    if not any(c.isalpha() for c in word):
        raise DraftError("no_name")
    return word


def relevant_evidence(person, evidence) -> tuple[Evidence, ...]:
    """The evidence about this person's company. Refuse when there is none."""
    if not isinstance(evidence, (tuple, list)) or not all(isinstance(e, Evidence)
                                                          for e in evidence):
        raise DraftError("bad_evidence")
    domain = getattr(person, "company_domain", "")
    domain = domain.lower() if isinstance(domain, str) else ""
    relevant = tuple(e for e in evidence if e.company_domain.lower() == domain)
    if not domain or not relevant:
        raise DraftError("no_evidence")
    return relevant
