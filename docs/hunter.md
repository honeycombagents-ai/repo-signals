# The Hunter people provider

`hunter` looks up people at a company domain with Hunter's domain search. It is registered by
default as a **dry run**. Live use is reachable from the command line only through the gated
`repo-signals run --live` (see [run.md](run.md)), with its own provider call budget.

Code: `src/repo_signals/providers/hunter.py`, which uses the strict HTTPS client in
`src/repo_signals/http.py`.

## Setup

1. Get an API key from **your own** Hunter account.
2. Export it in your shell or in your CI secrets. Never put it in a file you commit:

   ```sh
   export REPO_SIGNALS_PROVIDER=hunter
   export HUNTER_API_KEY=...
   repo-signals doctor
   ```

3. Expected `doctor` lines:

   - `PASS HUNTER_API_KEY is set`, or `FAIL HUNTER_API_KEY is missing` if it is not set
   - `WARN hunter: live use is not enabled until dry run is turned off`

`doctor` stays offline. It names `HUNTER_API_KEY` but never prints its value.

## Dry run first

Dry run is the default. In a dry run the provider makes **no network call**. It returns the
`fake` provider's synthetic people (addresses on `example.invalid`, with `source` set to `fake`)
and prints one line to stderr:

```
hunter: dry run, returning synthetic people from the fake provider (no network call)
```

Live use needs a call budget and an explicit `dry_run=False`:

```python
from repo_signals.budget import CallBudget
from repo_signals.providers.hunter import HunterProvider

budget = CallBudget(10)
provider = HunterProvider(budget, dry_run=False)
people = provider.find_people("example.com", ("CTO", "VP Engineering"), 5)
```

## The call budget

- Every live lookup is **one** domain-search request. The provider charges `budget.consume(1)`
  **before** the request. A call that would pass the cap never reaches the network.
- The provider never creates its own budget. Live mode refuses to start without one.
- When the budget runs out, the lookup raises `ProviderError("budget_exhausted")`.
- If you also call through `find_people_within(budget, provider, ...)` with the **same** budget,
  each lookup is charged twice: once by the guard and once for the request. The cap is
  conservative. It never undercounts.
- No budget can be raised above the hard bound of 500 calls.

## What a lookup returns

- Each returned address becomes a `Person` with `source` set to `hunter`.
- `confidence` is the provider's score divided by 100. A missing, non-numeric or out-of-range
  score becomes `0.0`.
- Entries without a name, a position or a well-formed address are skipped.
- When titles are given, a person is kept if any requested title appears in their position,
  ignoring case. For example, `cto` matches `Co-founder & CTO`.
- At most `limit` people come back. The request asks for at most 100.

## Errors

Every failure is a `ProviderError` whose message is a short code and nothing else. It never
includes the key, the URL, the query string, a header or the response body. The provider never
sleeps and never retries.

| code | when |
|---|---|
| `auth_failed` | 401 or 403 |
| `plan_exhausted` | 402, or an error id listed in `PLAN_EXHAUSTED_ERROR_IDS` |
| `retry_after_N` | 429 with a `Retry-After` of N seconds, N from 0 to 60. Waiting and retrying are up to the caller. |
| `rate_limited` | 429 with no usable `Retry-After`, or one over 60 seconds |
| `bad_request` | 400 or 422 |
| `server_error` | any 5xx |
| `unexpected_status` | any other non-2xx |
| `timeout` | the request timed out |
| `malformed_json` | a 2xx body that is not JSON |
| `unexpected_shape` | JSON that does not match the assumed shape |
| `missing_key`, `bad_key` | `HUNTER_API_KEY` unset, blank or containing spaces or control characters |
| `budget_exhausted` | the call budget is used up |
| `redirect_refused`, `response_too_large`, `network_error`, `tls_error` | from the HTTP client |

## The HTTP client

`src/repo_signals/http.py` uses the standard library only.

- It sends requests over https only, to hosts on an allowlist. For this provider the allowlist
  is the one API host.
- It never follows a redirect.
- It caps the response body at 1 MB.
- Every request has a timeout.
- It ignores proxy environment variables unless proxies are passed explicitly.
- The transport is injectable, so the tests run against a fake and never touch a network.

## Credits

If a response includes a remaining requests or credits count in the assumed shape, the provider
stores it in `provider.credits_remaining`, an integer. Getting this number costs no extra call.
`doctor` reports it when it is known.

## Your responsibilities

- **Hunter's terms are yours to read and follow.** Repo Signals claims no compliance with them.
- **The email laws that apply to you are yours to follow,** for example CAN-SPAM and the GDPR.
  Repo Signals never sends email. A person reviews and sends every draft.
- This is not legal advice.

## Unverified API details

The endpoint and field names come from general knowledge. **They have not been checked against
Hunter's documentation.** They all sit in one constants block at the top of
`providers/hunter.py`, marked "verify against Hunter's current API documentation before first
live use". The tests use synthetic JSON built to the same assumed shape, so passing tests do not
show that the assumptions are right.

Before the first live call, check each of these:

- [ ] `DOMAIN_SEARCH_URL`: the host, path and API version of the domain-search endpoint
- [ ] `API_KEY_HEADER`: that the key can be sent as an `X-API-KEY` header, keeping it out of the URL
- [ ] `PARAM_DOMAIN`, `PARAM_LIMIT`, `MAX_LIMIT`: the parameter names and the largest allowed limit
- [ ] `PARAM_SENIORITY`, `SENIORITY_VALUES`, `PARAM_DEPARTMENT`, `DEPARTMENT_VALUES`
- [ ] `RESP_DATA`, `RESP_EMAILS`: where the list of people sits in the response
- [ ] `FIELD_EMAIL`, `FIELD_FIRST_NAME`, `FIELD_LAST_NAME`, `FIELD_POSITION`
- [ ] `FIELD_SCORE` and `SCORE_MAX`: the score field and its range (assumed 0 to 100)
- [ ] the status codes for an auth failure, a rate limit and an exhausted plan, and whether
      `Retry-After` is sent
- [ ] `RESP_ERRORS`, `ERROR_ID`, `PLAN_EXHAUSTED_ERROR_IDS`: the error body and the error ids for
      an exhausted plan
- [ ] `CREDITS_REMAINING_PATHS`: whether the domain-search response carries a remaining count at
      all. If it does not, `credits_remaining` stays unset.
- [ ] how many credits one domain search costs, so you can size `REPO_SIGNALS_MAX_CALLS`

A synthetic response in the assumed shape (made up, not captured from Hunter):

```json
{
  "data": {
    "domain": "example.com",
    "emails": [
      {"value": "(an address on example.invalid)", "confidence": 90,
       "first_name": "Avery", "last_name": "Alder", "position": "CTO"}
    ]
  },
  "meta": {"results": 1, "requests_remaining": 42}
}
```
