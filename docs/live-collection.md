# Live collection

Two commands make real GitHub requests: `repo-signals probe-org` (exactly one request) and
`repo-signals collect --live` (the planned requests, after you type "yes"). For a Hugging Face
signal file, `collect --live` makes Hugging Face requests instead, and `repo-signals hf-probe-org`
is the one-request check (see [Hugging Face](#hugging-face)). All of them refuse to run, making no
request, unless dry run is off:

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

Dates are compared in UTC: "today" is the UTC date, matching GitHub's `pushed_at` times.

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

## Hugging Face

A signal file with `source = "huggingface"` (see [signal-file.md](signal-file.md)) goes through
`collect --live` with the same gates, in the same order, as a GitHub file. `run --live` takes it
too (see [run.md](run.md#a-hugging-face-signal-file)). Requests are anonymous: no token
is read or sent.

### Probe one organisation

```sh
repo-signals hf-probe-org example-org
```

This makes **exactly one** request, the organisation overview, with a call budget of 1. It needs
dry run off, and no terminal or typed "yes", like `probe-org`. It prints only:

```
hf-probe-org: login shape ok
is_verified: yes
models: 3
datasets: 0
spaces: 2
outcome: ok
```

It never prints the organisation's bio or any other text. It refuses with no request if dry run
is on or the name is not a valid organisation name shape. Exit codes: 0 for `ok`, 1 if the
request failed (the outcome line gives the code, for example `org_not_found`, `rate_limited` or
`unauthorised`), and 2 if it refused.

### collect --live for a Hugging Face file

```sh
export REPO_SIGNALS_HF_MAX_CALLS=7
repo-signals collect --dry-run
repo-signals collect --live
```

It refuses, with exit code 2 and no request, unless dry run is off, the signal file parses, the
suppression list (if set) parses, the plan fits `REPO_SIGNALS_HF_MAX_CALLS` and the rate ceiling,
and stdin and stdout are a terminal. It then prints the plan and asks for exactly `yes`:

```
signal: examplelib-hf
targets: 1 (1 planned, 0 skipped as suppressed)
terms: 1
source: huggingface; kinds: model, dataset, space
planned requests: at most 7 (1 overview per target, at most 2 listing pages per kind); budget 7 (REPO_SIGNALS_HF_MAX_CALLS); rate ceiling 400 per 300 seconds
Type "yes" to make these Hugging Face requests:
```

The Hugging Face client is built only after you type `yes`. Then it prints the run summary and
each Evidence item:

```
collect: complete
planned requests: 7; requests made: 3 (overviews 1, listing pages 2)
matching: matched 1
example.com -> example-org (huggingface, confirmed, verified)
evidence: example.com: 1
kept: 1
dropped 1: term_not_in_name
evidence for example.com:
  summary: The public Hugging Face model example-org/examplelib-7b matches the term examplelib, last modified on 2026-10-20.
  url: <the model link>
```

A kind whose overview count is zero is not listed (the datasets above). A listing that failed,
was not attempted or was capped shows as `listing incomplete: example.com dataset: <code>`, and
the run ends as partial (exit 3). `unauthorised`, `rate_limited`, `budget_exhausted`,
`server_error` and `unexpected_shape` stop the run; what was collected is printed. The exit codes
are the ones in [Exit codes](#exit-codes).

It never:

- reads, prints or stores an organisation bio, a model or dataset card, a description or any
  other free text;
- lists a target whose organisation was not found, or one that is suppressed;
- follows a redirect or the URL in a `Link` header (only its cursor is read);
- retries or sleeps: a rate limit stops the run;
- writes anything to disk or sends anything anywhere except the Hugging Face API host.

### Observed on a real organisation

On 2026-10-27, `hf-probe-org huggingface` and an anonymous `collect --live` against the
organisation `huggingface` worked as described above. The probe printed `is_verified: yes` and
counts of 18 models, 48 datasets and 47 spaces. Each listing fitted in one page (18 models, 80
datasets, 46 Spaces), and the Spaces listing worked without `gated` in its expand list. All 18
models parsed and were then dropped: 16 as `outside_window` (older than 365 days) and 2 as
`term_not_in_name`. Evidence came out with the right kind wording and links, and the cap of 10
items per target dropped 4 items as `over_target_cap`. The full list is in
[huggingface-design.md](huggingface-design.md#10-checked-and-unchecked-api-details).

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
