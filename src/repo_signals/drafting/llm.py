"""An OPTIONAL extension point for drafting with your own language model. NOT implemented.

Repo Signals ships no model client, no endpoint and no key handling. To draft with a model,
write a class that satisfies ``LlmDrafter`` and makes the model call yourself, then wrap it in
``GuardedDrafter`` (``fill_drafts`` wraps every drafter automatically)::

    class MyModelDrafter:
        name = "my-model"

        def draft(self, person, evidence, sender):
            ...  # your own model call, with your own key and your own terms
            return Draft(subject=..., body=..., grounded_on=(evidence[0].url,))

The guard applies the same rules to a model's output as to a template's, and rejects a draft
that fails any of them:

- the person has a real name and there is evidence about their company;
- the body contains each grounded evidence URL and quotes each one's summary verbatim;
- the body contains the sender's postal address and opt-out text;
- no URL appears other than the evidence URLs (and any URL in the opt-out text);
- no claimed relationship, meeting or prior contact that the evidence does not mention;
- the subject names the company domain or an evidence kind;
- the subject and body are within their length caps.

You are responsible for what you send to a model provider (it is personal data) and for that
provider's terms.
"""
from typing import Protocol, runtime_checkable

from ..models import Evidence, Person
from .base import Draft, Sender
from .guard import GuardedDrafter

__all__ = ["GuardedDrafter", "LlmDrafter"]


@runtime_checkable
class LlmDrafter(Protocol):
    """A Drafter backed by your own model call. Interface only: nothing here implements it."""

    name: str

    def draft(self, person: Person, evidence: tuple[Evidence, ...], sender: Sender) -> Draft:
        """Return a Draft grounded only on ``evidence``. It will be checked by the guard."""
        ...
