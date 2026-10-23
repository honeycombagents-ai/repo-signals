# Changelog

## Unreleased

- **CI scans the pull request head.** The checkout in `.github/workflows/scan.yml` now uses the
  pull request's head commit instead of GitHub's temporary merge commit; push events still scan
  the pushed commit.
- **The history scan reads git by structure** (`scripts/scan.py --history`): it walks every ref
  (branches, tags, remotes), takes changed files from NUL-delimited `git diff-tree` output, and
  also scans commit messages, author and committer names and emails, annotated tags and ref
  names. Files with NUL bytes are scanned too, and UTF-16 files with a byte-order mark are
  decoded. Author, committer and tagger identities are checked for token-shaped strings and the
  deny-list but not by the email rule, so commits from contributors and merges made on GitHub
  pass; the email rule still applies to file content, commit messages and ref names.
- **Sheet appends are split under the request limit.** New rows go out in order, in appends of
  at most 60 KB each, so a run with many people no longer fails with `payload_too_large` and
  loses every row. Each append is charged to the sheets budget. If one fails, the earlier
  appends stay written and the run prints how many rows were written and how many were not.
- **New code `row_too_large`** for a single row too large for any append. The rows before it
  stay written.
- **A refused request body is no longer charged** to the sheets budget.
- **The plan line** says 5 sheet calls for one append, plus 1 per extra append as needed.
- **Documented the read limit:** reading the existing rows is one request capped at 1 MB, so a
  sheet of roughly 750 to 800 typical rows needs trimming by hand (see
  [docs/google-sheets.md](docs/google-sheets.md)).
- **docs/deployment.md** now clones the repository and uses a virtual environment before
  `pip install -e .`.

## 0.1.0 (2026-10-22)

The first release. Nothing is ever sent by email: Repo Signals writes drafts into a sheet, and a
person reviews and sends each one. Dry run is the default. `run --live` and `collect --live` need
dry run turned off, a terminal and a typed "yes". `probe-org` makes one real GitHub request with
only dry run turned off: it needs no terminal and no typed "yes".

### Checked live

These were checked against the real services, GitHub on 2026-10-21 and the others on
2026-10-22:

- **GitHub collect (2026-10-21):** the organisation lookup, the organisation search, the
  repository search, the search qualifiers, the rate-limit headers and the version header.
- **Hunter domain search (one lookup):** the endpoint, the `X-API-KEY` header, the `domain` and
  `limit` parameters, the `data` and `emails` wrapper keys, and the response fields for email,
  position, first and last name and confidence.
- **Apollo people search (one lookup):** the `api_search` path, the `x-api-key` header, the request
  body (`q_organization_domains_list`, `person_titles`, `per_page`), the `people` key, and the
  `first_name` and `title` fields. Search returns first names and titles and no email
  addresses.
- **Google Sheets:** the token exchange, signing with openssl, the header check, the append and
  the duplicate skip.
- **`repo-signals run`**, end to end with the fake people provider. Hunter and Apollo were each
  checked with a direct lookup, not through `run`.

### Not checked live

- **GitHub:** the `Accept` media type, the token scheme, the page-size parameter and its largest
  value, the response field names and the fields kept from each response, the 422 message for an
  org the caller cannot see, the rate-limit statuses and secondary rate limits, and the search
  rate ceiling. GitHub's documentation has not been re-read.
- **Hunter:** the largest limit, the seniority and department filters, the confidence score
  range, the error statuses and `Retry-After`, the error body and error ids, the credits field,
  and how many credits one search costs.
- **Apollo:** what `has_email` means, the largest `per_page`, the `page` parameter, whether
  search spends credits, the error body and error ids, the error statuses and `Retry-After`,
  the fields that were not in the results (full name, last name, email and email status), and
  how many credits one search costs.
- **Google Sheets:** the `expires_in` and `token_type` fields of the token response, the header
  write on an empty sheet, the response for an empty range, and the status codes for an unshared
  sheet and for rate limiting.

The full lists are in [docs/hunter.md](docs/hunter.md), [docs/apollo.md](docs/apollo.md),
[docs/google-sheets.md](docs/google-sheets.md) and [docs/github-client.md](docs/github-client.md).

### Not built

- **Apollo email enrichment.** If it is ever added, it must be opt-in and have its own call
  budget (see [docs/apollo.md](docs/apollo.md)).
- **`doctor --online`.** The flag is accepted but does nothing yet.
- **A headless live mode.** This is a deliberate non-goal: a person reads the plan and types
  "yes" before every `run --live` and `collect --live`.
