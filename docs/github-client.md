# The GitHub client

`src/repo_signals/collector/github_api.py` holds the collector's GitHub client. It exists so it
can be tested offline against a fake transport. It is reachable from the command line only through
the gated `probe-org`, `collect --live` and `run --live`, and it is a **dry run by default**: in a dry run it builds no transport, and every call raises
`dry_run`. The design is in [collector-design.md](collector-design.md), section 4.

## What it does

`GitHubClient(http, budget, token=None, user_agent=None, dry_run=False)` has exactly three
read-only calls:

| call | request | returns |
|---|---|---|
| `get_org(login)` | the organisation's public profile | `{login, type, blog, is_verified}` |
| `search_orgs(name)` | an organisation search (candidates only, at most 3) | `SearchResult` of `{login, type}` |
| `search_repos(term, org, language, pushed_after)` | one org's public repositories (at most 5, first page only) | `SearchResult` of allowlisted repository records |

There is no code search, no repository contents, no HTML page and no other host.

Every call:

- is charged to the `CallBudget` **before** it is sent. A call over the cap never reaches the
  network (`budget_exhausted`);
- goes through `http.HttpClient`: https only, the GitHub API host only, no redirects, a 1 MB body
  cap and a timeout;
- sends the token, if there is one, **only** in the `Authorization` header: never in a URL, a
  body, an error or a log;
- sends `User-Agent: repo-signals/<version> (+https://honeycombagents.ai)`, the API version
  header and the JSON `Accept` header;
- keeps only allowlisted fields from the response and discards everything else at once.
  Repository records keep exactly the keys `collector/github_fields.py` reads (see
  [evidence.md](evidence.md)).

## Query building

Every value is validated before any request is made, and the request is refused (with a code)
if a value fails:

| value | rule | code |
|---|---|---|
| term | the signal file's term rule: 2 to 60 characters, letters, digits and `. _ + -` | `bad_term` |
| org, and the name for an org search | the GitHub login shape: letters, digits, single inner hyphens, at most 39 characters | `bad_org`, `bad_name` |
| language | the signal file's language rule: one word, at most 32 characters | `bad_language` |
| pushed after | a date, or a `YYYY-MM-DD` string that is a real date | `bad_date` |

The search query is assembled only from these validated values and fixed qualifiers. It is
passed through `HttpClient`'s params, so it is URL-encoded and never pasted into a URL. A
repository search matches the term against repository **names only** (`in:name`), never
descriptions, topics or READMEs. It always excludes forks (`fork:false`) and archived
repositories (`archived:false`), and it asks for at most 5 results. There is no pagination: the `Link` header is
ignored and a second page is never requested.

## Rate limits

The remaining-requests and reset headers are read into `client.rate_limit`, a
`RateLimit(remaining, reset)` of integers (or `None`). No raw header text is kept.

- A **403** or **429** response raises `rate_limited` (with `reset` as a number when GitHub sent
  one, for example `rate_limited: reset 1767225600`). Every later call in that run raises the same
  error **without making a request**.
- When a response says **0 requests remain**, that response is still returned, but the next call
  raises `rate_limited` without making a request.
- The client never sleeps and never retries.

## Error codes

Every error is a `ProviderError` with a short code. It never contains a URL, a token, a header, a
response body, an org name or a term.

| code | when |
|---|---|
| `dry_run` | any call in dry run |
| `budget_exhausted` | the call budget is used up |
| `rate_limited` | 403, 429, or a previous response said no requests remain |
| `token_rejected` | 401. Later calls in that run raise the same code without a request. |
| `not_found` | 404 |
| `org_not_visible` | 422 whose error message says the listed users or repositories cannot be searched. Either the org name is wrong, or the token cannot see that org. Public orgs can be searched without a token; private repositories need a token that can see them. Other targets continue. |
| `bad_query` | any other 422 |
| `server_error` | any 5xx |
| `unexpected_status` | any other non-2xx |
| `unexpected_shape` | JSON that does not match the expected shape |
| `bad_term`, `bad_org`, `bad_name`, `bad_language`, `bad_date` | a value failed validation; no request was made |
| `timeout`, `malformed_json`, `response_too_large`, `redirect_refused`, `network_error`, `tls_error` | from `http.HttpClient` |

## Unverified details checklist

**Live check, 2026-10-21:** the org lookup, the org search, the repository search, the search
qualifiers, the rate-limit headers and the version header were checked live and behaved as
assumed. GitHub's documentation has **not** been re-read yet, so the items below stay open until it
is.

Everything GitHub-specific sits in one constants block at the top of `github_api.py`, marked
"verify against GitHub's current documentation before first live use". Before the first live
call, check:

- [ ] `API_HOST` and `API_BASE`: the REST API host
- [ ] `ORG_PATH`, `SEARCH_ORGS_PATH`, `SEARCH_REPOS_PATH`: the three paths
- [ ] `PARAM_QUERY`, `PARAM_PER_PAGE`: the query parameter names, and the largest page size
- [ ] `HEADER_ACCEPT` and `MEDIA_TYPE`: the JSON media type
- [ ] `HEADER_API_VERSION` and `API_VERSION`: whether a version header is required, and its value
- [ ] `HEADER_AUTHORIZATION` and `AUTH_SCHEME`: how a token is sent
- [ ] `HEADER_RATE_REMAINING`, `HEADER_RATE_RESET`: the rate-limit header names, and that reset
      is in epoch seconds
- [ ] `QUALIFIER_TYPE_ORG`, `QUALIFIER_ORG`, `QUALIFIER_IN`, `QUALIFIER_NO_FORKS`,
      `QUALIFIER_NO_ARCHIVED`, `QUALIFIER_LANGUAGE`, `QUALIFIER_PUSHED_AFTER`: the search
      qualifiers and their syntax
- [ ] confirm the `in:name` qualifier (`QUALIFIER_IN`) against GitHub's current documentation:
      that it limits a repository search to names only
- [ ] `RESP_ERRORS`, `RESP_ERROR_MESSAGE` and `ORG_NOT_VISIBLE_MESSAGE`: that a search naming an
      org the caller cannot see returns 422 with exactly this text in `errors[].message`: "The
      listed users and repositories cannot be searched either because the resources do not
      exist or you do not have permission to view them." Only that exact text gives
      `org_not_visible`; the body is compared, never printed or stored
- [ ] `RESP_ITEMS`, `RESP_TOTAL_COUNT`, `RESP_INCOMPLETE`: the search response fields
- [ ] `ORG_KEYS`, `CANDIDATE_KEYS`, `REPO_KEYS`, `OWNER_KEYS`: the fields kept from each response
- [ ] that 403 and 429 are the rate-limit statuses, and what a secondary rate limit looks like
- [ ] the search rate ceiling used by the plan (`plan.SEARCH_RATE_CEILING_PER_MINUTE`, 30)
