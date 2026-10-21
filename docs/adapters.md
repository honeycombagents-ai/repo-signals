# Writing a people-provider adapter

A people provider takes a company domain and returns people at that company. Repo Signals ships
three built-in providers: `fake`, which returns synthetic people and makes no network calls,
`hunter` (dry run by default, see [hunter.md](hunter.md)) and `apollo` (dry run by default,
search only, see [apollo.md](apollo.md)).

Any other source can be added as a custom adapter.

## The interface

An adapter is any object with these three members (see `src/repo_signals/providers/base.py`):

| member | type | meaning |
|---|---|---|
| `name` | `str` | lowercase letters, digits, `-` or `_`; unique in the registry |
| `required_env` | `tuple[str, ...]` | the environment variable **names** the adapter reads, such as its API key |
| `find_people(company_domain, titles, limit)` | returns `list[Person]` | at most `limit` people |

`Person` (in `src/repo_signals/models.py`) is a frozen record with these fields:

| field | type | notes |
|---|---|---|
| `name` | `str` | |
| `email` | `str` or `None` | `None` when the source found no address |
| `title` | `str` | |
| `company_domain` | `str` | |
| `source` | `str` | your adapter's name |
| `confidence` | `float` | from 0 to 1 |

## Rules every adapter follows

1. **Keys come from the environment only.** List every variable name in `required_env`. Read the
   values inside `find_people`. Never accept a key as an argument, and never log one. `doctor`
   reports whether each name is set, never its value.
2. **Every call goes through the budget.** Callers use `find_people_within(budget, provider, ...)`.
   It charges a `CallBudget` before the call, so the call cap cannot be bypassed. The budget can
   never exceed the hard bound of 500 calls.
3. **Failures are codes.** Raise `ProviderError("rate_limited")`, `ProviderError("auth_failed")`
   and so on. The message is the code alone: never a key, a URL or a response body. Raise it with
   `from None` so the original exception is not chained.
4. **Return at most `limit` people**, all of them `Person` objects. Anything else is rejected with
   `ProviderError("bad_response")`.
5. **Read the provider's terms.** Using a provider's data is governed by its terms, which are yours
   to follow.

## A synthetic example

This adapter is synthetic. It returns a fixed list and calls nothing. A real adapter would call its
provider's API where the comment says so.

```python
import os

from repo_signals.models import Person
from repo_signals.providers import ProviderError, register


class ExampleDirectory:
    name = "example-directory"
    required_env = ("EXAMPLE_DIRECTORY_KEY",)

    def find_people(self, company_domain, titles, limit):
        key = os.environ.get("EXAMPLE_DIRECTORY_KEY", "")
        if not key:
            raise ProviderError("missing_key")
        # A real adapter calls its API here, authenticating with `key`,
        # and maps each failure to a ProviderError code.
        rows = [("Sample Person", "Head of Engineering")]
        return [
            Person(name=n, email=None, title=t, company_domain=company_domain,
                   source=self.name, confidence=0.6)
            for n, t in rows
        ][:limit]


register(ExampleDirectory())
```

Then select it and check the setup:

```sh
export REPO_SIGNALS_PROVIDER=example-directory
export EXAMPLE_DIRECTORY_KEY=...      # set in your shell or CI secrets, never in a file you commit
repo-signals doctor
```

Expected `doctor` lines:

- `PASS REPO_SIGNALS_PROVIDER names a registered provider`
- `PASS EXAMPLE_DIRECTORY_KEY is set`, or `FAIL EXAMPLE_DIRECTORY_KEY is missing` if the key is not set

## Testing your adapter

Test offline: give the adapter a stubbed transport and synthetic data. Use addresses on
`example.invalid`, a reserved domain that can never receive mail. The repository's own tests show
the pattern (`tests/test_providers.py`).
