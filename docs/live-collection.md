# Live collection

Two commands make real GitHub requests: `repo-signals probe-org` (exactly one request) and
`repo-signals collect --live` (the planned requests, after you type "yes"). Both refuse to run,
making no request, unless dry run is off:

```sh
export REPO_SIGNALS_DRY_RUN=false     # exactly "false"; unset or "true" keeps dry run on
```

`REPO_SIGNALS_DRY_RUN` accepts only `true` or `false`. Any other value is an error. `doctor`
warns when dry run is off, and warns that live collection is **ARMED** when dry run is off and
`REPO_SIGNALS_SIGNAL_FILE` is set. It never prints a value.

`repo-signals collect --dry-run` is unchanged. It still makes no request, even with dry run off.

## The GitHub token (optional)

```sh
export REPO_SIGNALS_GITHUB_TOKEN=...   # optional; from your own GitHub account
```

The token is used only to get higher rate limits. It is sent only in the `Authorization`
header: never in a URL, the output, an error or a file. Without a token, the much lower
anonymous limits apply. A token with no extra permissions is enough, because only public data
is read.

## Step 1: probe one organisation

```sh
repo-signals probe-org example-org
```

This makes **exactly one** request, an organisation lookup, with a call budget of 1. It exists so
you can check the unverified API details with a single request before collecting anything. It
prints code-only fields:

```
probe-org: login shape ok
type: Organization
website listed: yes
is_verified: yes
rate limit remaining: 57
rate limit reset: 1767225600
outcome: ok
```

It never prints the website text. It refuses with no request if dry run is on or the login is
not a valid login shape. Exit codes: 0 for `ok`, 1 if the request failed (the outcome line gives
the code, for example `not_found`, `rate_limited` or `token_rejected`), and 2 if it refused.

## Step 2: the first live run

Start small:

- [ ] one target with a named `org`
- [ ] one term
- [ ] `REPO_SIGNALS_MAX_CALLS` of 3 to 5
- [ ] `repo-signals collect --dry-run` first, and read the plan
- [ ] `repo-signals doctor`, and expect the ARMED warning

```sh
export REPO_SIGNALS_MAX_CALLS=3
repo-signals collect --live
```

## What collect --live checks, before any request

It refuses, with exit code 2 and no request, unless **all** of these hold:

1. dry run is off (`REPO_SIGNALS_DRY_RUN=false`);
2. `REPO_SIGNALS_SIGNAL_FILE` is set and the file parses;
3. the suppression list, if set, parses;
4. the plan fits the call budget and the search-rate ceiling;
5. stdin and stdout are a terminal. It cannot be run from a script or a pipe.

## The confirmation prompt

It prints the plan, then asks:

```
signal: examplelib-adopters
targets: 1 (1 planned, 0 skipped as suppressed)
terms: 1
planned requests: at most 2 (core 1, search 1); budget 3; search ceiling 30 per minute (unverified)
Type "yes" to make these GitHub requests:
```

Only exactly `yes` continues. `y`, `YES`, `yes ` with a space, an empty line, end of input or
anything else exits 1 with no request. **There is no flag to skip the prompt.** The GitHub client
is built only after you type `yes`.

## What is printed

The run summary (see [orchestration.md](orchestration.md)), then each Evidence item as two lines:

```
evidence for example.com:
  summary: The public repository example-org/examplelib-tools matches the term examplelib, last pushed on 2026-09-30.
  url: <the repository link>
```

Nothing else from GitHub is printed: no descriptions, no websites, no individuals.

## Exit codes

| code | meaning |
|---|---|
| 0 | complete |
| 1 | not confirmed: nothing was requested |
| 2 | refused before starting: nothing was requested |
| 3 | partial: a rate limit, the budget, or an error stopped or skipped part of the run. What was collected is printed. |
| 4 | refused by the plan check inside the run |

## If a search reports org_not_visible

`search skipped: example.com term 1: org_not_visible` means GitHub refused to search that org.
Either the org name in the signal file is wrong, or your token cannot see that org. Public orgs
can be searched without a token. Private repositories need a token that can see them. The other
targets continue, and the run ends as partial (exit 3).

## What is never stored or sent

- **Nothing is written to disk.** Evidence goes to stdout only. Filling the sheet is a later step.
- Nothing is sent anywhere except the GitHub API host, and only the planned read-only requests.
- The token is never printed or stored.
- No retry, no sleep: a rate limit stops the run.

## Verify against GitHub's current documentation

Every endpoint, header, qualifier and field name is unverified general knowledge, kept in one
constants block at the top of `src/repo_signals/collector/github_api.py`. Before the first real
run, check the list in [github-client.md](github-client.md#unverified-details-checklist), in
particular:

- [ ] the API host and the three paths (`/orgs/{org}`, `/search/users`, `/search/repositories`)
- [ ] the `Accept` media type and the API version header
- [ ] the rate-limit header names, and that 403 and 429 mean rate limited
- [ ] the search qualifiers, including `in:name`, `fork:false` and `archived:false`
- [ ] the exact 422 message used for `org_not_visible` (`ORG_NOT_VISIBLE_MESSAGE`)
- [ ] the response fields kept for organisations and repositories
- [ ] the search-rate ceiling of 30 per minute used by the plan

Use `probe-org` to check the organisation fields and the rate-limit headers with one request.
