# The Apollo people provider

`apollo` looks up people at a company domain with Apollo's people search. It is registered by
default as a **dry run**. Live use is not reachable from the command line yet: the `repo-signals`
command keeps dry run on, and `doctor` says so.

Code: `src/repo_signals/providers/apollo.py`, which uses the strict HTTPS client in
`src/repo_signals/http.py` (`post_json`).

## Setup

1. Get an API key from **your own** Apollo account.
2. Export it in your shell or in your CI secrets. Never put it in a file you commit:

   ```sh
   export REPO_SIGNALS_PROVIDER=apollo
   export APOLLO_API_KEY=...
   repo-signals doctor
   ```

3. Expected `doctor` lines:

   - `PASS APOLLO_API_KEY is set`, or `FAIL APOLLO_API_KEY is missing` if it is not set
   - `WARN apollo: live use is not enabled until dry run is turned off`
   - `PASS apollo: email reveal is not implemented (search only, no credit-spending calls)`

`doctor` stays offline. It names `APOLLO_API_KEY` but never prints its value.

## Dry run first

Dry run is the default. In a dry run the provider makes **no network call**. It returns the
`fake` provider's synthetic people (addresses on `example.invalid`, with `source` set to `fake`)
and prints one line to stderr:

```
apollo: dry run, returning synthetic people from the fake provider (no network call)
```

Live use needs a call budget and an explicit `dry_run=False`:

```python
from repo_signals.budget import CallBudget
from repo_signals.providers.apollo import ApolloProvider

budget = CallBudget(10)
provider = ApolloProvider(budget, dry_run=False)
people = provider.find_people("example.com", ("CTO", "VP Engineering"), 5)
```

## Search only: email reveal is not implemented

The provider makes exactly one request per lookup: the people search. **It never calls an
email-reveal, enrichment or other credit-spending endpoint.** This is intentional.

A person whose email is missing, empty or a locked placeholder is still returned, with
`email=None` and `confidence` 0.0, so a reviewer can see the name and title. Adding reveal would
need an explicit opt-in setting and its own separate budget. Neither exists in v1.

## The call budget

- Every live lookup is **one** people-search request. The provider charges `budget.consume(1)`
  **before** the request. A call that would pass the cap never reaches the network.
- The provider never creates its own budget. Live mode refuses to start without one.
- When the budget runs out, the lookup raises `ProviderError("budget_exhausted")`.
- If you also call through `find_people_within(budget, provider, ...)` with the **same** budget,
  each lookup is charged twice: once by the guard and once for the request. The cap is
  conservative. It never undercounts.
- No budget can be raised above the hard bound of 500 calls.

## What a lookup returns

- Each returned person becomes a `Person` with `source` set to `apollo`.
- `email` is `None` when the address is missing, empty, a locked placeholder, malformed, or has
  an email status listed in `NO_ADDRESS_EMAIL_STATUSES`.
- `confidence` comes from the email status, using `EMAIL_STATUS_CONFIDENCE`. It is 0.0 when
  there is no usable address or the status is unknown.
- People without a name or a title are skipped.
- When titles are given, they are sent with the search. A returned person is kept only if one of
  the requested titles appears in their title, ignoring case.
- At most `limit` people come back. The request asks for at most 100 per page and only the first
  page.

## Errors

Every failure is a `ProviderError` whose message is a short code and nothing else. It never
includes the key, the URL, a header or the request or response body. The provider never sleeps
and never retries.

| code | when |
|---|---|
| `auth_failed` | 401 or 403 |
| `plan_exhausted` | 402, or an error code listed in `PLAN_EXHAUSTED_ERROR_CODES` |
| `retry_after_N` | 429 with a `Retry-After` of N seconds, N from 0 to 60. Waiting and retrying are up to the caller. |
| `rate_limited` | 429 with no usable `Retry-After`, or one over 60 seconds |
| `bad_request` | 400 or 422 |
| `server_error` | any 5xx |
| `unexpected_status` | any other non-2xx |
| `timeout` | the request timed out |
| `malformed_json` | a 2xx body that is not JSON |
| `unexpected_shape` | JSON that does not match the assumed shape |
| `missing_key`, `bad_key` | `APOLLO_API_KEY` unset, blank or containing spaces or control characters |
| `budget_exhausted` | the call budget is used up |
| `redirect_refused`, `response_too_large`, `network_error`, `tls_error` | from the HTTP client |

## The key

The key is sent only in a request header. It is never put in the URL and never put in the JSON
body.

## Your responsibilities

- **Apollo's terms are yours to read and follow.** That includes any limits on how Apollo's data
  may be stored, exported or used. Repo Signals claims no compliance with them.
- **The email laws that apply to you are yours to follow,** for example CAN-SPAM and the GDPR.
  Repo Signals never sends email. A person reviews and sends every draft.
- This is not legal advice.

## Unverified API details

The endpoint and the parameter, header and field names come from general knowledge. **They have
not been checked against Apollo's documentation.** They all sit in one constants block at the
top of `providers/apollo.py`, marked "verify against Apollo's current API documentation before
first live use". The tests use synthetic JSON built to the same assumed shape, so passing tests
do not show that the assumptions are right.

Before the first live call, check each of these:

- [ ] `PEOPLE_SEARCH_URL`: the host, path and version of the people-search endpoint, and that it
      takes a POST with a JSON body
- [ ] that this search endpoint **does not spend credits or reveal emails** on your plan. If it
      does, do not use it until there is an opt-in setting and a separate budget.
- [ ] `API_KEY_HEADER`: that the key can be sent as an `X-Api-Key` header, keeping it out of the
      URL and the body
- [ ] `BODY_DOMAINS`, `BODY_TITLES`, `BODY_PER_PAGE`, `BODY_PAGE`, `MAX_PER_PAGE`
- [ ] `RESP_PEOPLE`: where the list of people sits in the response
- [ ] `FIELD_NAME`, `FIELD_FIRST_NAME`, `FIELD_LAST_NAME`, `FIELD_TITLE`
- [ ] `FIELD_EMAIL`, `FIELD_EMAIL_STATUS`, and what a locked email looks like:
      `LOCKED_EMAIL_LOCAL_PARTS`, `NO_ADDRESS_EMAIL_STATUSES`
- [ ] `EMAIL_STATUS_CONFIDENCE`: the email status values and what each one means
- [ ] the status codes for an auth failure, a rate limit and an exhausted plan, and whether
      `Retry-After` is sent
- [ ] `RESP_ERROR_CODE`, `PLAN_EXHAUSTED_ERROR_CODES`: the error body and the codes for an
      exhausted plan
- [ ] how many credits one search costs, so you can size `REPO_SIGNALS_MAX_CALLS`

A synthetic response in the assumed shape (made up, not captured from Apollo):

```json
{
  "people": [
    {"name": "Avery Alder", "title": "CTO",
     "email": "(an address on example.invalid)", "email_status": "verified"},
    {"name": "Blake Birch", "title": "VP Engineering",
     "email": "(a locked placeholder)", "email_status": "locked"}
  ],
  "pagination": {"page": 1, "per_page": 25}
}
```

The second person comes back as `email=None` with `confidence` 0.0.
