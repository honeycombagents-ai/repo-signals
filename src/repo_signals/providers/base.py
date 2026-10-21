"""The interface every people provider implements."""
import re
from typing import Protocol, runtime_checkable

from ..budget import CallBudget
from ..models import Person

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


@runtime_checkable
class PeopleProvider(Protocol):
    """A source of people for a company domain.

    ``required_env`` lists the environment variable NAMES the provider reads
    (for example its API key). Providers read keys from the environment only.
    """

    name: str
    required_env: tuple[str, ...]

    def find_people(self, company_domain: str, titles: tuple[str, ...],
                    limit: int) -> list[Person]:
        ...


class ProviderError(Exception):
    """A provider failure, described by a short code only.

    The message is the code (for example ``auth_failed`` or ``rate_limited``)
    and nothing else: never a key, URL or response body. Anything that is not a
    plain lowercase code is replaced by ``unspecified``. Raise it ``from None``
    so the original exception (which may hold a URL or body) is not chained.
    """

    def __init__(self, code: str):
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            code = "unspecified"
        self.code = code
        super().__init__(code)


def find_people_within(budget: CallBudget, provider: PeopleProvider, company_domain: str,
                       titles: tuple[str, ...], limit: int) -> list[Person]:
    """Call ``provider.find_people`` after charging one call to ``budget``.

    All provider calls go through here so the cap cannot be bypassed.
    """
    budget.consume(1)
    people = provider.find_people(company_domain, titles, limit)
    if not isinstance(people, list) or len(people) > limit or not all(
            isinstance(p, Person) for p in people):
        raise ProviderError("bad_response")
    return people
