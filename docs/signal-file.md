# The signal file

A signal file tells the GitHub collector what to look for and which companies to look at. It is
one small TOML file, named by `REPO_SIGNALS_SIGNAL_FILE` (one file per run).

So far only the dry run exists. It validates the file and prints the requests a run would make,
and it requests nothing:

```sh
export REPO_SIGNALS_SIGNAL_FILE=~/signals/examplelib.toml
repo-signals collect --dry-run
repo-signals doctor          # reports whether the file parses (counts only)
```

`repo-signals collect` with neither `--dry-run` nor `--live` prints
`usage: repo-signals collect --dry-run | --live` and exits 2. Live collection is described in
[live-collection.md](live-collection.md), and the design is in
[collector-design.md](collector-design.md).

## Example 1: one library term, a user-named org and a domain-only target

```toml
name = "examplelib-adopters"
terms = ["examplelib"]
language = "Python"

[[targets]]
domain = "example.com"
org = "example-org"

[[targets]]
domain = "example.invalid"
```

`pushed_within_days` is not set, so it defaults to 365. The second target has no `org`, so a
live run would look for an organisation whose website is on `example.invalid` and that GitHub
marks as verified. If none passes, or more than one does, the target is reported, not used.

## Example 2: two terms, a confirmed org, a shorter window

```toml
schema = 1
name = "example-sdk-users"
terms = ["example-sdk", "example_sdk"]
pushed_within_days = 90

[[targets]]
domain = "shop.example.com"
org = "example-labs"
confirm_org = true

[[targets]]
domain = "example.com"
org = "example-org"
```

`confirm_org = true` says that you have checked `example-labs` really is the company at
`shop.example.com`. Without it, a live run would exclude the target if the org's website were on
another domain.

## Fields

| field | required | rule |
|---|---|---|
| `schema` | no | if present, must be `1` |
| `name` | yes | lowercase letters, digits and `-`; starts with a letter; at most 48 characters |
| `terms` | yes | 1 to 5 strings, each 2 to 60 characters: letters, digits and `. _ + -`, starting with a letter or digit |
| `language` | no | one word: letters, digits and `+ # . -`, at most 32 characters |
| `pushed_within_days` | no | a whole number from 1 to 3650; default 365 |
| `titles` | no | up to 5 job titles passed to the people provider by `repo-signals run`, for example `["CTO", "VP Engineering"]`: letters, digits, single spaces and `& . / + -`, at most 60 characters each, no duplicates (ignoring case). With Hunter, titles are filtered among only the first 3 people Hunter returns for each company, so a title may return nobody; Apollo filters titles on its own side. See [hunter.md](hunter.md) and [apollo.md](apollo.md) |
| `targets` | yes | 1 to 50 `[[targets]]` tables |

Each target:

| field | required | rule |
|---|---|---|
| `domain` | yes | a hostname with at least one dot, for example `example.com`. It is stored in lowercase. |
| `org` | no | a GitHub organisation login: letters, digits and single inner hyphens, at most 39 characters (this shape is unverified) |
| `confirm_org` | no | `true` or `false` (default `false`); `true` needs an `org` |

## Validation rules

- The file is at most 64 KB, UTF-8 and valid TOML.
- Unknown keys are refused, at the top level and inside a target. File-name signals
  (`filenames`) are not part of v1.
- Wrong types are refused: for example, a string where a number belongs, or `1` where `true`
  belongs.
- Duplicate terms (ignoring case), duplicate domains (ignoring case) and duplicate orgs
  (ignoring case) are refused.
- Terms cannot contain spaces, quotes or colons, so a term cannot add a search qualifier.

Every error is a short code, for example `signal_too_many_terms`, `signal_unknown_key` or
`signal_target_3_bad_domain`. An error can name a key or a position in the file, but it never
repeats anything from the file.

## The plan

The dry run turns the file into a plan: a list of the requests a live run would make, at most.

- A target with an `org`: one org lookup.
- A target with only a `domain`: one candidate search, plus up to 3 org lookups to check the
  candidates.
- For every term, for every target: one repository search.

A target whose domain is on your suppression list (`REPO_SIGNALS_SUPPRESSION_FILE`) is skipped
and listed. That includes a domain listed directly, a subdomain of one, and the domain of a
listed email address.

The plan is **refused** before anything runs when:

- its request count is over the call budget (`REPO_SIGNALS_MAX_CALLS`, default 25). The error is
  `plan_over_budget: <planned> planned, <allowed> allowed`.
- its search count is over the search-rate ceiling of 30 per minute (an unverified figure for
  GitHub's search limit with a token). The error is `plan_over_search_rate: ...`.

Example output for Example 1, with a budget of 25:

```
collect: dry run, nothing requested
signal: examplelib-adopters
targets: 2 (2 planned, 0 skipped as suppressed)
terms: 1
planned requests: 7 (core 4, search 3); budget 25; search ceiling 30 per minute (unverified)
first 5 planned request(s):
  1. org lookup: example-org (for example.com)
  2. repository search: term examplelib in example-org
  3. candidate search: organisations for example.invalid
  4. org lookup: candidate 1 for example.invalid
  5. org lookup: candidate 2 for example.invalid
```

The dry run prints descriptions, never URLs, and makes no network call.
