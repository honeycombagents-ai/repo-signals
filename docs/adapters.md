# Writing a people-provider adapter

A people provider takes a company domain and returns people at that company. Repo Signals ships
three built-in providers: `fake`, which returns synthetic people and makes no network calls,
`hunter` (dry run by default, see [hunter.md](hunter.md)) and `apollo` (dry run by default,
search only, see [apollo.md](apollo.md)).

Any other source can be added as a custom adapter, used as a library from your own Python
program.

## Library use only

**The `repo-signals` command cannot load a provider registered in your own script.** The command
has no plugin mechanism: it imports only its own modules, so a provider you register in a
separate script never exists inside the `repo-signals` process. There, `doctor` reports that
`REPO_SIGNALS_PROVIDER` does not name a registered provider, and `run --live` refuses.

To use a custom provider, import `repo_signals` in your own program, register the provider there,
and call the same entry points the command uses, passing your registry and your settings. If you
want the `repo-signals` command itself to know your provider, fork and edit the package.

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

## How the call budget is charged

`run --live` charges the people-provider budget (`REPO_SIGNALS_PROVIDER_MAX_CALLS`) in one of
two ways (see `build_live_provider` and `people_stage` in `src/repo_signals/run.py`):

- **A custom provider** is taken from the registry as it is, and every lookup goes through
  `find_people_within(budget, provider, ...)`. That charges **one call before each
  `find_people` call**, so the cap cannot be bypassed. Your adapter therefore must not take a
  `CallBudget` or charge one itself: it would be charged twice. One charge covers one
  `find_people` call, so an adapter that makes several requests per lookup spends more of your
  provider's credits than the budget counts.
- **Hunter and Apollo** are the exception. `run` builds them with the budget
  (`HunterProvider(budget, dry_run=False)`, `ApolloProvider(budget, dry_run=False)`) and they
  charge it themselves before each request. That is special-cased by name in
  `build_live_provider`, so a new provider that charges per request needs a fork and edit of
  that function.

The budget can never exceed the hard bound of 500 calls.

## Rules every adapter follows

1. **Keys come from the environment only.** List every variable name in `required_env`. Read the
   values inside `find_people`. Never accept a key as an argument, and never log one. `doctor`
   reports whether each name is set, never its value, and `run --live` refuses while one is
   missing.
2. **Failures are codes.** Raise `ProviderError("rate_limited")`, `ProviderError("auth_failed")`
   and so on. The message is the code alone: never a key, a URL or a response body. Raise it with
   `from None` so the original exception is not chained.
3. **Return at most `limit` people**, all of them `Person` objects. Anything else is rejected with
   `ProviderError("bad_response")`.
4. **Read the provider's terms.** Using a provider's data is governed by its terms, which are yours
   to follow.

If the adapter calls a real API, it should also send the API key in a request header (never in
the URL or the body), go through `repo_signals.http.HttpClient` with the API host on its
allowlist, keep the endpoint, parameter and field names in one constants block marked as
unchecked until you have checked them, and be tested with a fake transport and synthetic data,
never against the live API. [`providers/hunter.py`](../src/repo_signals/providers/hunter.py)
shows that layout.

## A synthetic example

This adapter is synthetic. It returns a fixed list and calls nothing. A real adapter would call its
provider's API where the comment says so.

```python
import os

from repo_signals.models import Person
from repo_signals.providers import ProviderError


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
```

Your own program registers it and passes the registry and the provider name to the same entry
points the command uses:

```python
import os
import sys

from repo_signals import doctor, run
from repo_signals.providers import default_registry

registry = default_registry()
registry.register(ExampleDirectory())
environ = {**os.environ, "REPO_SIGNALS_PROVIDER": "example-directory"}

doctor.run(environ=environ, registry=registry)
sys.exit(run.run(live=True, environ=environ, registry=registry))
```

`doctor` then prints `PASS REPO_SIGNALS_PROVIDER names a registered provider` and
`PASS EXAMPLE_DIRECTORY_KEY is set` (or `FAIL EXAMPLE_DIRECTORY_KEY is missing`). `run.run(live=True)`
applies the same gates as `repo-signals run --live`, including a terminal and a typed "yes".
`run.run(live=False)` is the dry run, which always uses the built-in `fake` provider.

## Testing your adapter

Test offline: give the adapter a stubbed transport and synthetic data. Use addresses on
`example.invalid`, a reserved domain that can never receive mail. The repository's own tests show
the pattern (`tests/test_providers.py`).

## Replacing the sheet

The sheet is not pluggable either. To write somewhere other than a Google Sheet, fork and edit
these files:

- **`run.py`**: it imports `SheetWriter`, `COLUMNS` and `mask_email`. The dry run builds its
  sample row with `SheetWriter.build_row` and finds the `email` and `email_draft` columns in
  `COLUMNS`. The live path builds `SheetWriter(Settings(dry_run=False, sheet_id=...,
  google_credentials=...), budget=..., http=..., signer=..., out=..., clock=..., run_id=...)`
  and calls `write(records)`, where each record is `(Person, signal_evidence, draft text)`. It
  treats the codes in `SHEET_REFUSAL_CODES` (`header_mismatch`, `header_missing`) as a refusal,
  prints `SHEETS_CALLS_PER_WRITE` and the first 4 characters of the sheet id in the plan, charges
  the sheet budget from `REPO_SIGNALS_SHEETS_MAX_CALLS`, and has the test seams `sheets_http`,
  `signer` and `clock`.
- **`run_gates.py`**: `REPO_SIGNALS_SHEET_ID` and `REPO_SIGNALS_GOOGLE_CREDENTIALS` are gates, so
  `run --live` refuses without them.
- **`config.py`**: the `SHEET_ID`, `GOOGLE_CREDENTIALS` and `SHEETS_MAX_CALLS` variables, their
  parsers, and the `sheet_id`, `google_credentials` and `sheets_max_calls` settings.
- **`doctor.py`**: the Google credentials check, the openssl check, the sheet id check and the
  sheet budget check.
- **`init_setup.py`**: the notes for the sheet id, the credentials path and the sheet budget in
  `env.example`.
- **`drafting/sheet.py`**: `fill_drafts` works on rows in the sheet's column layout
  (`sheets/layout.py`) and uses `guard_cell`, `mask_email` and `FORMULA_PREFIXES` from
  `sheets/cells.py`.

Duplicate detection lives in `SheetWriter._new_only` (with `_key` and `_name_key`) in
`sheets/writer.py`. A replacement must bring its own, or every run writes the same people again.
The tests in `tests/test_run.py` use a fake Google, so they need the same change.
