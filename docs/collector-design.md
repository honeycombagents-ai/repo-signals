# Design: the GitHub signal collector

Status: **design only**. Nothing in this document is built yet. It describes what a build would
do, so the decisions can be made before any code is written. Every GitHub endpoint, field and
limit named here is unverified general knowledge (see section 4).

## 1. Purpose and scope

The collector turns a **signal definition** (a small file you write) into a list of
`models.Evidence` items per company domain, using only **public** GitHub data. Those Evidence
items are exactly what the drafter needs: each draft quotes an Evidence summary verbatim and
links to its URL (see [drafting.md](drafting.md)).

In scope for v1:

- GitHub only, through its documented REST API.
- Public organisation profiles and public repositories owned by organisations.
- Optional use of your own GitHub token, only to get higher rate limits.

Out of scope for v1, deliberately:

- Hugging Face and every other source.
- Scraping HTML pages, including GitHub's own web pages.
- Private repositories and anything visible only because you are signed in. The token is used
  for rate limits, never to see more data.
- Individuals: no commit authors, contributors, user accounts or emails (see section 6).
- Sending anything. The collector only produces Evidence. A person reviews every draft.

The collector follows the conventions of the rest of Repo Signals: dry run by default, every
request charged to a `CallBudget` before it is sent, settings from environment variables only,
code-only errors, and an offline `doctor`.

## 2. The signal definition format

A signal definition is one small file, in TOML or JSON. Both are parsed with the standard library
(`tomllib`, `json`), and the format is chosen by the file extension. A planned setting,
`REPO_SIGNALS_SIGNAL_FILE`, would name the file by path, like the other file settings.

### Example 1 (TOML): a library keyword, user-supplied orgs

```toml
schema = 1
name = "examplelib-adopters"
keywords = ["examplelib"]
language = "Python"
pushed_within_days = 180

[[targets]]
domain = "example.com"
org = "example-org"

[[targets]]
domain = "example.invalid"
```

The first target names its organisation. The second gives only a domain, so the collector must
match it to an organisation (section 3) or report it as unmatched.

### Example 2 (JSON): a second keyword, org-only and domain-only targets

```json
{
  "schema": 1,
  "name": "example-tool-config",
  "keywords": ["example-tool"],
  "min_repo_age_days": 30,
  "targets": [
    {"org": "example-labs"},
    {"domain": "shop.example.com"}
  ]
}
```

### Fields

| field | required | meaning |
|---|---|---|
| `schema` | yes | must be `1` |
| `name` | yes | a label for the signal: lowercase letters, digits and `-`, up to 48 characters |
| `keywords` | yes | terms matched against repository names, descriptions and topics |
| `language` | no | keep only repositories whose primary language matches |
| `pushed_within_days` | no | keep only repositories pushed within this many days (1 to 3650, as `collector/signal.py` enforces; this table previously said 1 to 730); defaults to 365 |
| `min_repo_age_days` | no | keep only repositories at least this old (0 to 3650) |
| `targets` | yes | the companies: each entry has a `domain`, an `org`, or both, and optionally `confirm_org = true` (section 3) |

### Validation rules

The parser is strict. Anything it does not recognise is an error, not something to guess at.

- The file is UTF-8, at most 64 KB, and parses as TOML or JSON. A parse error is reported as
  `signal_not_toml` or `signal_not_json`. The parser's own message is never printed, because it
  can quote file content.
- Unknown keys are refused (`signal_unknown_key`). Only key **names** may appear in an error,
  never a value.
- `keywords` is a list of strings, 2 to 64 characters each. A keyword uses only letters, digits
  and `. _ + -`. No spaces, quotes, colons or other characters that could add a search
  qualifier. A term therefore cannot change the query beyond the term itself. File-name signals
  (code search) are not part of v1 (decided).
- Duplicate terms (ignoring case) and duplicate targets are refused.
- A `domain` must pass the same domain check as `Evidence.company_domain`. An `org` must look
  like a GitHub organisation login: letters, digits and single hyphens, up to 39 characters.
  That format is unverified.
- A target whose domain is on the suppression list (`REPO_SIGNALS_SUPPRESSION_FILE`) is skipped
  before any request is made, and the report counts it.

### Hard caps

These are the decided v1 values.

| cap | value |
|---|---|
| search terms (`keywords`) | at most 5 |
| targets per file | at most 50 |
| repositories considered per target and term | at most 5 (first page only) |
| Evidence items per target | at most 10 |
| pages fetched per query | 1 |
| requests per run | bounded by `CallBudget`, which can never exceed 500 |

## 3. Matching a company to a GitHub organisation

Evidence is keyed by company domain, but GitHub is organised by organisation login. The link
between the two is the riskiest step: a wrong match produces a confident-sounding draft about the
wrong company.

| method | how it works | false-positive risk |
|---|---|---|
| **A. user-supplied org** | The target names its `org`. The collector fetches that org's public profile to confirm it exists and is an organisation. | Low. The main risk is a typo, which shows up as `org_not_found` or as a website that does not match the domain. |
| **B. verified-domain match** | For a domain-only target, look up candidate orgs, then accept one only if its public profile lists a website on exactly that domain (or `www.` plus that domain) **and** GitHub marks the org as having a verified domain. | Medium-low. "Verified" may cover a different domain from the website field (unverified), and finding candidates still needs a name search. |
| **C. name search** | Search organisations by a name derived from the domain (for example `example` from `example.com`). | High. Common words, look-alikes, abandoned orgs and unrelated orgs with the same name all match. |

**Recommended default: never guess silently.**

1. If the target names an org, use A.
2. Otherwise, use B. Candidates come from a name search, but a candidate is accepted only when
   exactly one passes the verified-domain check.
3. Name search alone (C) never produces Evidence. Its candidates appear in the report as
   suggestions for you to confirm by adding `org = "..."` to the target.
4. A target with no accepted org is reported as `unmatched`. A target with more than one passing
   candidate is reported as `ambiguous`, with the candidate org logins listed (org logins are
   organisation-level names, not individuals). **Neither is included in the output.**
5. If a user-supplied org's website is on a different domain from the target, the target is
   **excluded** and reported as `website_mismatch`, until you set `confirm_org = true` on that
   target to say the org is right (decided). A user-supplied org that lists **no website** is
   excluded the same way (code `no_website`) until `confirm_org = true` is set (decided).

**Confidence** is a label, not a number:

| label | meaning | included by default |
|---|---|---|
| `user` | you named the org in the signal file | yes |
| `verified-domain` | exactly one org passed the website-plus-verified check | yes |
| `website-only` | the website matches but the org is not marked verified | no, reported for confirmation |
| `name-only` | a name search candidate only | no, reported as a suggestion |

Confidence is shown on the report line for each target, for example
`example.com -> example-org (user)`. It is not put into the Evidence summary, because the summary
states only what the repository shows. `Evidence` itself is unchanged.

## 4. GitHub access

**Status:** the client exists (`collector/github_api.py`, see [github-client.md](github-client.md))
but is unreachable from the command line, and it is a dry run by default.

### Endpoints

> **Unverified.** Everything in this table is general knowledge, not checked against GitHub's
> documentation. **Verify against GitHub's current documentation before first live use.** In
> the build, these would sit in one constants block with that same comment, like
> `providers/hunter.py` and `sheets/google_api.py`.

Paths are relative to the GitHub REST API host (`API_HOST` in the build, the only host on the
`HttpClient` allowlist). Repository and file links point to the GitHub web host (`WEB_HOST`),
which the collector **never requests**. Links to it are only copied into Evidence.

| purpose | method and path | notes |
|---|---|---|
| organisation profile | `GET /orgs/{org}` | login, display name, website (`blog`), verified flag (`is_verified`), `html_url`, `type` |
| candidate organisations | `GET /search/users?q=<name>+type:org` | method B candidates only; counts against the search limit |
| repositories matching a keyword | `GET /search/repositories?q=<term>+org:<org>+fork:false+archived:false[+language:<l>][+pushed:>=<date>][+created:<=<date>]&per_page=<n>` | returns `full_name`, `html_url`, `description`, `language`, `pushed_at`, `created_at`, `fork`, `archived`, `topics`, `owner.type`; `total_count`; `incomplete_results` |
| one repository (re-check) | `GET /repos/{owner}/{repo}` | only to confirm a repository still exists; not needed in v1 if search results carry `fork` and `archived` |
| remaining limits | `GET /rate_limit` | for a future `doctor --online` only; v1 `doctor` stays offline |

Request headers: `Accept` for the GitHub JSON media type, an API version header if GitHub
requires one, `User-Agent` (see section 7) and, only when a token is set,
`Authorization: Bearer <token>`. The collector never asks for the text-match media type. v1 makes
no code search requests (decided).

### Rate limits (unverified figures)

- **Core REST limit:** roughly 60 requests an hour without a token and about 5,000 an hour with
  one.
- **Search has its own, much lower limit:** roughly 10 requests a minute without a token and
  about 30 a minute with one. Search is the bottleneck: 50 targets with 5 terms could need 250 searches, which is far over a
  per-minute limit.
- **Secondary rate limits:** too many requests at once or too fast can be refused with 403 or
  429 even when the main limit has room. A `Retry-After` header may be sent.
- **Headers:** `x-ratelimit-remaining`, `x-ratelimit-reset` (an epoch time) and
  `x-ratelimit-resource` tell which limit applies.

v1 plans before it spends. Dry run, and the start of a live run, compute the number of requests
the definition needs, split into core and search. A live run refuses to start (`budget_too_small`
or `search_limit_too_small`) when the plan exceeds the remaining `CallBudget` or the per-minute
search limit. That is better than running half-way and stopping.

### Pagination

- Only the first page of each query (`per_page` at most 10). The `Link` header's `rel="next"` is
  noted in the report ("about N more results not fetched") but never followed in v1.
- Search returns at most about 1,000 results in total, whatever the paging. With the caps above,
  v1 never comes near that.

### Conditional requests

GitHub supports `ETag` with `If-None-Match`. A `304 Not Modified` reportedly does not count
against the core limit. Two things to note:

- `http.HttpClient` currently treats **every** 3xx, including 304, as a refused redirect
  (`redirect_refused`). Conditional requests would need a small change: return 304 as a normal
  status with no body, keep every other 3xx refused, and add tests.
- Conditional requests only help with a cache (section 6). v1 has no cache (decided), so v1
  skips them.

### Errors: surface a code, never sleep or retry

| response | code | effect |
|---|---|---|
| 401 | `auth_failed` | stop the run (the token is wrong) |
| 403 with `x-ratelimit-remaining: 0` | `rate_limited` (or `retry_after_N`, N up to 60, when `Retry-After` is sent) | stop the run; report the reset time as a UTC clock time |
| 403 otherwise | `forbidden` | stop the run (secondary limit, abuse detection or an access policy) |
| 429 | `retry_after_N` or `rate_limited` | stop the run |
| 404 on an org | `org_not_found` | report the target; continue with the others |
| 404 on a repository | `repo_not_found` | skip it; continue |
| 301 or any other 3xx | `redirect_refused` (reported as `repo_moved`) | skip it; never follow |
| 422 | `bad_query` | report the term; continue with the other terms |
| 5xx | `server_error` | stop the run |
| timeout, malformed JSON, oversized body, unexpected shape | `timeout`, `malformed_json`, `response_too_large`, `unexpected_shape` | stop the run |

The collector never sleeps and never retries in v1. A stopped run still returns what it has
already collected, marked partial (section 8).

### Token, budget and dry run

- The token is optional and comes only from `REPO_SIGNALS_GITHUB_TOKEN`, which already exists in
  `config`. It is sent only in the `Authorization` header: never in a URL, a query string, a log,
  a report or an error. `doctor` reports it as set or missing, by name only, as it does today.
- Every request goes through `CallBudget.consume(1)` **before** it is sent. A request over the
  cap never reaches the transport.
- Requests go through `http.HttpClient`, with the API host as the only allowed host. That gives
  https only, no redirects, a 1 MB body cap, a timeout on every call and code-only errors.
- **Dry run is the default and never touches the network.** It parses and validates the signal
  file, applies the suppression list, and prints the plan: targets by match method, planned core
  and search requests, and whether they fit the budget and the search limit. It ends
  `nothing requested`.

## 5. Evidence output

Each accepted hit becomes one `models.Evidence(company_domain, summary, url, kind)`:

- `company_domain` is the target's domain.
- `url` is the `html_url` GitHub returned for the repository. It is kept only if it is
  https and on the GitHub web host. Otherwise the hit is dropped and the report counts it.
- `kind` is one of the kinds below.
- `summary` is one short factual sentence built from a **fixed template**.

### Kinds

| kind | when | summary template |
|---|---|---|
| `repo` | the repository's name or description matches a keyword | `The public repository {full_name} matches the term {term}{language_clause}, last pushed on {pushed_date}.` |

`{language_clause}` is either empty or ` and its primary language on GitHub is {language}`.
`{pushed_date}` is the `pushed_at` date as `YYYY-MM-DD`. A rendered example, with synthetic
values:

`The public repository example-org/widget matches the term examplelib and its primary language on GitHub is Python, last pushed on 2026-09-30.`

### Rules for every summary

- Only fields GitHub returned are used: the repository full name, the term that matched, the
  primary language and the last push date. The repository description is excluded in v1
  (decided). If it is ever allowed, it is quoted, truncated to 80 characters, and
  dropped when it contains a URL, an `@` or a control character.
- Never free-form text from READMEs, issues, pull requests, commits, code or wikis.
- Never a claim about the company beyond what the repository shows. The summary names the
  repository, not the company. It says "matches the term", never "uses" or "depends on".
- One printable line, at most 300 characters, as `Evidence` already requires.
- Every inserted value is checked: the full name matches `owner/repo` with safe characters, the
  term is one of the definition's own terms, the language passes a short character allowlist,
  and the date parses. A value that fails any check drops the hit; it is not patched up.

### Why summaries are built from templates

The drafter quotes each summary verbatim, and the guard only checks that the summary and its URL
are present. It cannot judge whether the summary is true or fair. So the summary must already be
safe when the collector produces it:

- **factual**: built only from structured fields;
- **bounded**: one line, a fixed shape, a length cap;
- **free of personal data**: no names, logins or emails of individuals;
- **free of foreign URLs**: the guard would reject the draft anyway; and
- **free of relationship claims**: the guard rejects phrases like "as we discussed" unless the
  evidence contains them, so evidence must never smuggle them in.

Free text copied from a README could carry any of those problems into an email a person might
send. A fixed template cannot.

## 6. Personal data

The collector collects **organisation-level repository facts only**.

Deliberately excluded:

| excluded | why |
|---|---|
| commit authors and committers, their names and emails | personal data about individuals, and not needed to show a company-level signal |
| logins of individual users, including owners of user-owned repositories | identifies a person. Repositories owned by a user rather than an organisation are dropped entirely, even when they match. |
| contributor lists, stargazers, watchers, fork owners | lists of individuals |
| issue, pull request and discussion authors and text | individuals and free text |
| README bodies, code contents, code search text fragments | free text, and possibly personal data. The text-match media type is never requested. |
| an org's public email address | not needed. Contact details come from the people providers, under their own terms. |

The response parser keeps an allowlist of fields and discards everything else as soon as a
response is parsed, so excluded data never reaches Evidence, the report or a cache.

**What is stored:** nothing on disk by default. Evidence lives in memory and is passed to the
drafter's `evidence_lookup`.

**v1 ships no cache (decided).** A cache, if one is added after v1, would:

- live in a directory named by a new setting, created with mode 0700, with files at mode 0600;
- hold only allowlisted fields (org login, repository full name, `html_url`, language,
  `pushed_at`, `fork`, `archived`) and the `ETag`. Never the token and never a full response;
- be keyed by a hash of the request path and query, never including the token;
- expire entries after 24 hours by default, with a maximum of 7 days;
- be capped at 10 MB, evicting the oldest entries first;
- be cleared by a `repo-signals cache purge` command, which deletes only files it created; and
- be reported by `doctor` as entry count and size only.

## 7. Terms, ethics and limits

This is plain language, not legal advice.

- GitHub's Terms of Service, Acceptable Use Policies and API terms restrict some uses of
  information about GitHub users (for example, using it for spam or selling it) and restrict
  bulk collection. **You must read them yourself.** Repo Signals does not interpret them for
  you and claims no compliance with them.
- The collector uses the documented REST API only. It never scrapes HTML.
- It honours rate limits: it plans requests up front, stops at the first rate-limit response,
  and never retries aggressively. In v1 it never retries at all.
- It identifies itself with a `User-Agent` that names the project and points to a project URL
  that holds no personal contact details: `repo-signals/<version> (+https://honeycombagents.ai)`
  (decided).
- It collects organisation-level facts only (section 6), and a domain on your suppression list
  is never looked up.

The README must say, in plain words:

1. The collector uses only GitHub's documented REST API and only public data. It does no HTML
   scraping and reads no private repositories.
2. It collects organisation-level repository facts only: no individuals, commit authors,
   emails or contributor lists.
3. The token is optional, read only from `REPO_SIGNALS_GITHUB_TOKEN`, used only for rate limits,
   sent only in a header, and never logged.
4. It honours GitHub's rate limits and never retries in v1.
5. Dry run is the default and makes no request.
6. GitHub's terms are yours to read and follow, and so is email law (CAN-SPAM, the GDPR and
   others). This is not legal advice.
7. An organisation that does not want to be looked up can be excluded with the suppression list.

## 8. Failure modes

| situation | what happens | code or report line |
|---|---|---|
| budget too small for the plan | live run refuses before any request | `budget_too_small` |
| plan exceeds the per-minute search limit | live run refuses before any request | `search_limit_too_small` |
| budget runs out mid-run (estimate was low) | stop; return what was collected | `budget_exhausted`; `partial: 12 of 30 targets done` |
| rate-limited mid-run | stop at the first 403 or 429 rate-limit response; no sleep, no retry | `rate_limited` or `retry_after_N`; `partial: ... (limit resets at 14:05 UTC)` |
| secondary rate limit | stop | `forbidden`; `partial: ...` |
| ambiguous org | target excluded | `example.com: ambiguous (3 candidate orgs: example-org, example-labs, example-hq; add org = "..." to confirm)` |
| unmatched company | target excluded | `example.invalid: unmatched (no organisation passed the domain check)` |
| user-supplied org not found | target excluded | `org_not_found`; `example.com: org example-org not found` |
| website on another domain | target excluded until `confirm_org = true` is set on it | `example.com -> example-org (user, website_mismatch: excluded; set confirm_org = true to include)` |
| a repository disappeared | hit skipped | `repo_not_found`; `example.com: 1 repository no longer available` |
| a repository moved (301) | hit skipped; the redirect is never followed | `repo_moved`; `example.com: 1 repository moved` |
| fork or archived repository | excluded by default (query qualifiers, then rechecked on the result fields) | `example.com: excluded 2 forks, 1 archived` |
| repository owned by a user, not an org | dropped | `example.com: excluded 1 user-owned repository` |
| link not on the GitHub web host, or not https | hit dropped | `example.com: dropped 1 hit with an unexpected link` |
| empty results | not an error | `example.com -> example-org (user): no matching public repositories` |
| huge result set | first page only, capped | `example.com: showing 5 of about 1,234 matches (first page only)` |
| search reports `incomplete_results` | kept, flagged | `example.com: search results incomplete` |
| a term rejected by search | that term skipped | `bad_query`; `term 2 rejected by search` (the term's position, not its text) |
| suppressed domain | skipped before any request | `skipped 3 suppressed target(s)` |
| bad token | stop | `auth_failed` |
| timeout, malformed or oversized response | stop | `timeout`, `malformed_json`, `unexpected_shape`, `response_too_large` |

The run result carries the Evidence collected so far, a `partial` flag, and the report lines.
Report lines name domains, org logins and repository names, which are organisation-level public
facts. They never contain a token, a URL query string, a response body or anything about an
individual.

## 9. Test plan

All tests are offline: a fake transport behind `http.HttpClient`, with fixtures built from
synthetic JSON shaped like the assumed responses. Fixtures use only `example.com` and
`example.invalid` domains and names such as `example-org` and `example-org/widget`. They
deliberately include fields the collector must drop: user logins, commit-author-like fields,
`text_matches` fragments, emails and README-like text. The tests assert that none of it appears
in the output.

Test areas:

- Signal definition: every validation rule and cap, TOML and JSON, unknown keys, injection-shaped
  terms, size cap, and errors that never contain a value.
- Query building: qualifiers (`org:`, `fork:false`, `archived:false`, `language:`, dates); terms
  passed through unchanged; nothing in the query except the definition's own values.
- Matching: methods A and B, `ambiguous`, `unmatched`, `website_mismatch`, and name-only
  candidates that never produce Evidence.
- Evidence: each template, the length cap, value checks, the link host check, and drafter
  compatibility (each summary passes `Evidence` validation and the drafting guard).
- Personal data: user-owned repositories dropped, excluded fields never present in any output.
- Budget: charged before every request; an over-cap request never reaches the transport;
  `budget_too_small` refused before any request.
- Dry run: zero transport calls, and the plan is printed.
- Errors: every row in the section 4 table; no sleep (`time.sleep` mocked to fail); no retry
  (exactly one request per planned request).
- Secrets: a sentinel token never appears in a URL, a report, an error or `doctor` output.

Mutants the build's harness must catch (outside the repository, as for the earlier builds):

1. budget bypassed (`consume` removed)
2. budget charged after the request
3. token put in the URL or query string
4. token printed by `doctor` or in a report
5. fork repositories included
6. archived repositories included
7. a user-owned repository included
8. personal data included (an individual's login or a commit author in a summary or report)
9. free text in summaries (the description or README text inserted by default)
10. silent org guess (a name-search candidate accepted without the domain check)
11. ambiguous match resolved by picking the first candidate
12. retries added (a loop on 403, 429 or 5xx)
13. sleeping on `Retry-After`
14. dry run touching the network
15. per-target Evidence cap ignored
16. a search qualifier injected through a term (`:` or a space accepted)
17. a moved repository's redirect followed
18. a summary longer than 300 characters or over one line accepted
19. an Evidence URL outside the GitHub web host accepted
20. a suppressed domain looked up
21. a second host (for example the web host) added to the `HttpClient` allowlist
22. text-match fragments requested or kept
23. the plan check skipped (a live run starts despite `budget_too_small`)

## 10. PR split and decisions

### PR split (dry run first)

Each PR is offline-tested with a fake transport and keeps dry run as the default.

1. **Signal definition and plan (no network code).** Parser, validation, caps, suppression
   skip, the request plan and `collect --dry-run` output. A `doctor` check that the signal file
   parses (counts only).
2. **Evidence templates and personal-data filters.** Pure functions from synthetic JSON to
   `Evidence`: field allowlist, owner-type check, link check, summary templates.
3. **GitHub client.** Endpoints and constants block (unverified), the header and token handling,
   error mapping, rate-limit header reading, the budget, and the host allowlist. Still
   unreachable from the command line.
4. **Organisation matching and the report.** Methods A and B, `ambiguous`, `unmatched`,
   suggestions and confidence labels.
5. **Collector orchestration.** Targets to Evidence, plan-before-spend refusal, partial results,
   and handing Evidence to the drafter's `evidence_lookup`.
6. **Live switch.** Only after the unverified details are checked against GitHub's
   documentation, and with John's sign-off.

### Decided (John)

1. **Org matching:** user-supplied org (A) plus verified-domain matching (B). Name search is used
   only for suggestions and never produces Evidence.
2. **Website mismatch:** a user-named org whose website is on a different domain is excluded
   until the target sets `confirm_org = true`.
3. **`website-only`** matches are report-only.
4. **No cache in v1.**
5. **Limits:** 5 terms and 50 targets per file, 5 repositories per target and term, 10 Evidence
   items per target.
6. **Repository description:** excluded from summaries.
7. **`User-Agent`:** `repo-signals/<version> (+https://honeycombagents.ai)`.
8. **Plan-before-spend:** a live run whose plan exceeds the budget or the search limit is refused
   before any request.
9. **No file-name signals in v1:** no code search, so no `file` kind.
10. **Signal file setting:** `REPO_SIGNALS_SIGNAL_FILE`, one file per run.
11. **Default window:** `pushed_within_days` defaults to 365.
12. **One evidence kind in v1:** `repo` only. The `topic` kind is dropped, as is `file`.
