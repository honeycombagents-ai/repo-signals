# Changelog

## 0.2.1 (2026-10-27)

A small fix to `doctor`. Nothing is ever sent by email, and dry run is still the default.

- **`doctor` no longer asks for a GitHub token for a Hugging Face signal file.** A Hugging Face
  file is collected anonymously, so `doctor` no longer warns that `REPO_SIGNALS_GITHUB_TOKEN` is
  missing when the signal file has `source = "huggingface"`. A malformed token still fails. With
  no signal file, a GitHub file or an unusable file, the line is as before. The `env.example`
  note written by `init` now says the token is optional and for GitHub signal files only.

### Not checked live

Nothing in this release was checked against a real service. The full `run --live` for a Hugging
Face file against a real sheet, and Hunter or Apollo returning people for a Hugging Face
organisation's domain, are still unchecked.

## 0.2.0 (2026-10-27)

A second collector source: public Hugging Face models, datasets and Spaces owned by an
organisation you name. Nothing is ever sent by email, and dry run is still the default. A signal
file without `source` is a GitHub file and behaves as before.

- **Hugging Face signal files.** A new optional `source` key (`"github"`, the default, or
  `"huggingface"`). A Hugging Face target needs both `org` and `confirm_org = true`, because an
  organisation profile has no website to check. `kinds` picks models, datasets and Spaces (all
  three by default) and `modified_within_days` (1 to 3650, default 365) is the window on the last
  modified date. See [docs/signal-file.md](docs/signal-file.md) and
  [docs/huggingface-design.md](docs/huggingface-design.md).
- **`REPO_SIGNALS_HF_MAX_CALLS`** (1 to 500, default 25) caps Hugging Face requests. The plan
  counts 1 overview plus at most 2 listing pages per kind per target and is refused before any
  request if it does not fit this budget or the rate ceiling (400 per 300-second window).
- **`collect --live` for a Hugging Face file**, behind the same gates as GitHub (dry run off, a
  terminal and a typed "yes"). Requests are anonymous. Only allowlisted fields are read (never a
  bio, card or description); items must be owned by the organisation, public, not gated or
  disabled, recently modified and have the term in their name. Evidence names the model, dataset
  or Space and links to it under huggingface.co. A listing that still had pages after the cap is
  reported as `listing_capped` and the run is partial.
- **`hf-probe-org`**, one Hugging Face request for one organisation overview, with dry run off and
  no terminal or typed "yes" (like `probe-org`). It prints only whether the organisation is
  verified, its three counts and the outcome.
- **`run --live` for a Hugging Face file**: the sheet preflight, then collection from Hugging Face,
  then people, drafts and the sheet, with every gate and budget as before. No GitHub token is
  needed for a Hugging Face file. The plan and the requests lines name Hugging Face instead of
  GitHub.
- **The intro template is source-neutral.** It now says the signal turned up "in an automated
  search of public repositories" instead of "public GitHub repositories". This changes GitHub
  drafts too.
- **The failing-gates line names the signal-file code.** When the signal file does not load,
  `run --live` and `doctor` now show `REPO_SIGNALS_SIGNAL_FILE (CODE)`, for example
  `signal_target_1_bad_org`, instead of the bare variable name. An unset variable is still named
  alone.
- **Setup guide fixes from a cold run:** a dragged-in key path with a leading `~` now works, the
  sheets budget numbers for a first and second run are explained, the organisation prompt says it
  wants the `NAME` in github.com/NAME and not a domain, `confirm_org = true` is explained, and a
  first run that writes nothing is described as not an error. The guide also has a Hugging Face
  variant of the signal-file step.
- **New troubleshooting rows** for the Hugging Face codes a user can now see, including
  `org_not_found`, `unauthorised`, `bad_link`, `listing_capped` and `client_error`.

### Checked live

On 2026-10-27, anonymously, against the organisation `huggingface`: `hf-probe-org`, and
`collect --live` listing models, datasets and Spaces. The overview fields, the listings with the
per-kind expand list (Spaces refuse `gated` in it), the record fields, the evidence wording and
links, and the cap of 10 items per target behaved as described in
[docs/huggingface-design.md](docs/huggingface-design.md).

Also on 2026-10-27, anonymously, with no sheet and no people provider: collection from the
organisation `huggingface` with the real client returned 6 evidence items of kind `hf-model`
(2 requests: 1 overview and 1 listing page). The first two were drafted through the real
guarded template drafter for a synthetic person whose company domain matched the evidence
domain. The guard accepted both, each draft contained its evidence summary and URL, and the
intro read "an automated search of public repositories". A sheet row built from one of them had
11 columns and an unchanged `signal_evidence` cell. A first attempt with a person on a different
domain was refused as `no_evidence`: the drafter's domain-match rule working as designed. No
Hunter, Apollo or Google calls were made.

Nothing else in this release was checked against a real service.

### Not checked live

- `run --live` for a Hugging Face file, end to end. Collection, drafting and building the sheet
  row were each checked separately (above), but the full run, with a real sheet and a people
  provider, was not. Whether Hunter or Apollo return people for a Hugging Face organisation's
  domain was not checked either.
- Hugging Face's 429 body and `Retry-After`.
- Hugging Face tokens: none is used, and their quota and scope are unknown.
- Redirects for renamed repositories, and user-owned repositories.
- Organisation names with unusual characters (underscores, dots, or a leading digit).
- Splitting a sheet write into several appends against a real sheet.
- Hugging Face's terms for automated access and for outreach use. Read them before a live run.

## 0.1.1 (2026-10-23)

Fixes from a review of 0.1.0. Nothing is ever sent by email, and dry run is still the default.

- **Sheet preflight before any paid call.** Right after the typed "yes", `run --live` reads the
  sheet's header row once, through the same client the sheet stage uses, before any GitHub
  request or people lookup. A refused token, an unshared or wrong sheet, a first tab not named
  `Sheet1` or someone else's header now ends the run there (exit 4) instead of after the paid
  lookups. It costs one extra sheets call, so the minimum `REPO_SIGNALS_SHEETS_MAX_CALLS` is now
  6 and the recommended tiny budget for a first run is 6.
- **Hunter limit checked live (2026-10-23, one account, domain search):** a limit of 3 returned 3
  people, a limit of 10 returned 10, and a limit of 100 was refused with HTTP 400. Whether the
  refusal is a plan cap or Hunter's global maximum is unknown, and the credit cost of a larger
  limit is unchecked. `MAX_LIMIT` stays 100 in the code as an unchecked upper bound, not a
  working value; `repo-signals run` still asks for 3.
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
- **The plan line** says 6 sheet calls for the header check and one append, plus 1 per extra
  append as needed.
- **Documented the read limit:** reading the existing rows is one request capped at 1 MB, so a
  sheet of roughly 750 to 800 typical rows needs trimming by hand (see
  [docs/google-sheets.md](docs/google-sheets.md)).
- **docs/deployment.md** now clones the repository and uses a virtual environment before
  `pip install -e .`.
- **Local sheet checks before the typed "yes".** `run --live` now refuses, before asking for
  "yes", when the key file is unreadable or malformed (checked with the same function the run
  uses to load it), when openssl is not on `PATH` and the built-in signer would be used, or when
  `REPO_SIGNALS_SHEETS_MAX_CALLS` is below the minimum. `doctor` uses the same key-file check, so
  it now fails on a malformed `client_email` or `private_key` too.
- **Hunter titles warning.** With Hunter, titles are filtered among only the first 3 people Hunter
  returns, so a title can return nobody. `doctor` and the `run --live` plan now warn about this,
  and the docs say so.
- **Small fixes:** the `doctor` dry-run line names `run --live`; the unused live branch of
  `collect --dry-run`'s code is removed; a `CONTRIBUTING.md` guide is added; `.gitignore` now
  covers service-account key files, `dist/`, `build/` and `.DS_Store`.

### Not checked live

Only the Hunter limit check above was made against a real service. Sheet batching, the sheet
preflight and the local sheet checks have run only against a fake Google in the test suite (which
CI runs), not against a real sheet. The scanner changes have run only on this repository and on
throwaway test repositories.

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
