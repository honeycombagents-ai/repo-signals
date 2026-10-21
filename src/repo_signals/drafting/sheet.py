"""Fill the email_draft column of sheet rows. Never overwrites, never touches reviewed.

``fill_drafts(rows, drafter, evidence_lookup)`` takes rows in the sheet writer's column layout
(lists of cells, as read from or built for the sheet) and returns new rows. A row gets a draft
only when its email_draft cell is empty, the person has a real name, there is evidence about
their company, and they are not on the suppression list. A non-empty email_draft is never
replaced, and the reviewed column is never written.

Dry run (the default) prints how many drafts WOULD be filled and one sample draft with the
recipient's email masked, and returns the rows unchanged. Nothing is ever sent.
"""
import os
import sys
from collections.abc import Mapping

from ..config import Settings
from ..models import Person
from ..sheets.cells import FORMULA_PREFIXES, guard_cell, mask_email
from ..sheets.layout import COLUMNS
from .base import DraftError, DraftResult, require_sender, sender_from_env
from .guard import GuardedDrafter
from .suppression import suppression_from_env

_I = {name: i for i, name in enumerate(COLUMNS)}
DRAFT_COL = _I["email_draft"]
REVIEWED_COL = _I["reviewed"]


def draft_for(person, evidence, drafter, sender, suppression=None) -> DraftResult:
    """One person's outcome. Suppression is checked first: a listed person gets no draft.

    A sender problem is not a per-person outcome: it raises (drafting refuses entirely).
    """
    if suppression is not None and suppression.matches(person):
        return DraftResult("suppressed", None, "suppressed")
    try:
        return DraftResult("drafted", drafter.draft(person, evidence, sender), None)
    except DraftError as e:
        if e.var is not None:
            raise
        return DraftResult("refused", None, e.code)


def draft_cell(draft) -> str:
    return guard_cell(f"Subject: {draft.subject}\n\n{draft.body}")


def _unguard(value):
    """Undo the sheet writer's formula quote so the original value is used."""
    value = "" if value is None else str(value)
    if value.startswith("'") and value[1:].lstrip().startswith(FORMULA_PREFIXES):
        return value[1:]
    return value


def _person(row):
    cells = [_unguard(c) for c in row] + [""] * (len(COLUMNS) - len(row))
    try:
        confidence = float(cells[_I["confidence"]] or 0)
    except ValueError:
        confidence = 0.0
    try:
        return Person(name=cells[_I["name"]].strip(), email=cells[_I["email"]].strip() or None,
                      title=cells[_I["title"]].strip(),
                      company_domain=cells[_I["company_domain"]].strip(),
                      source=cells[_I["source"]].strip() or "sheet",
                      confidence=confidence if 0.0 <= confidence <= 1.0 else 0.0)
    except ValueError:
        return None


def _lookup(evidence_lookup, domain):
    if isinstance(evidence_lookup, Mapping):
        found = evidence_lookup.get(domain, ())
    elif callable(evidence_lookup):
        found = evidence_lookup(domain)
    else:
        raise TypeError("evidence_lookup must be a mapping or a function")
    return tuple(found or ())


def fill_drafts(rows, drafter, evidence_lookup, *, sender=None, suppression=None,
                settings=None, environ=None, out=None) -> list[list]:
    """Return rows with email_draft filled where allowed. See the module docstring.

    ``sender`` and ``suppression`` default to the environment (see config). Dry run is the
    default (``Settings()``).
    """
    settings = settings if settings is not None else Settings()
    environ = os.environ if environ is None else environ
    out = out if out is not None else sys.stdout
    sender = sender if sender is not None else sender_from_env(environ)
    require_sender(sender)
    if suppression is None:
        suppression = suppression_from_env(environ)
    guarded = drafter if isinstance(drafter, GuardedDrafter) else GuardedDrafter(drafter)

    result, sample = [], None
    counts = {"drafted": 0, "suppressed": 0, "refused": 0, "already": 0, "unusable": 0}
    for row in rows:
        if not isinstance(row, (list, tuple)):
            raise TypeError("each row is a list of cells")
        row = list(row)
        if len(row) > DRAFT_COL and row[DRAFT_COL] not in ("", None):  # even whitespace counts
            counts["already"] += 1
            result.append(row)
            continue
        person = _person(row)
        if person is None:
            counts["unusable"] += 1
            result.append(row)
            continue
        outcome = draft_for(person, _lookup(evidence_lookup, person.company_domain), guarded,
                            sender, suppression)
        counts[outcome.status] += 1
        if outcome.status != "drafted":
            result.append(row)
            continue
        if sample is None:
            sample = (person, outcome.draft)
        filled = row + [""] * (DRAFT_COL + 1 - len(row))
        filled[DRAFT_COL] = draft_cell(outcome.draft)
        result.append(filled)

    summary = (f"{counts['suppressed']} suppressed, {counts['refused']} refused (no evidence, "
               f"no name or failed the guard), {counts['already']} already drafted, "
               f"{counts['unusable']} unusable row(s)")
    if settings.dry_run:
        print(f"drafting: dry run, would fill {counts['drafted']} draft(s) ({summary}); "
              "nothing written", file=out)
        if sample is not None:
            person, draft = sample
            masked = mask_email(person.email or "")
            text = f"Subject: {draft.subject}\n\n{draft.body}"
            if person.email:
                text = text.replace(person.email, masked)
            print("--- sample draft ---", file=out)
            print(f"To: {masked or '(no email)'}", file=out)
            print(text, file=out)
            print("--- end of sample ---", file=out)
        return [list(r) for r in rows]
    print(f"drafting: filled {counts['drafted']} draft(s) ({summary}); nothing sent", file=out)
    return result
