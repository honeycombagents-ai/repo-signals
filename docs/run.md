# repo-signals run

`repo-signals run` runs the whole pipeline: **collect** evidence from GitHub, find **people** at
each company, **draft** an email for each person, and append rows to the **sheet** for a person to
review. Nothing is ever sent by email. There is no sending code at all.

```sh
repo-signals run            # dry run: no network, nothing written
repo-signals run --live     # real requests, after the gates and a typed "yes"
```

## The pipeline

1. **Collect** (GitHub): the signal file's plan, organisation matching, repository searches,
   filters and Evidence (see [orchestration.md](orchestration.md)). Evidence is printed as
   `summary` and `url` lines, exactly as `collect --live` prints it.
2. **People** (your people provider): one lookup per company domain that has Evidence and is not
   suppressed, with a limit of 3 and the signal file's optional `titles` (up to 5, see
   [signal-file.md](signal-file.md)).
3. **Draft**: one grounded draft per person, from the template drafter behind the guard (see
   [drafting.md](drafting.md)). A person with no name, a suppressed person, or a person with no
   Evidence for their company gets no draft. Each skip is counted by code. Suppressed people get
   no row at all.
4. **Sheet**: new rows are appended through the sheet writer, in order, in one append per 60 KB
   of new rows (see [google-sheets.md](google-sheets.md#appends-of-at-most-60-kb)).

## The three budgets

Each service has its own call budget. One stage hitting its cap never spends another's.

| variable | caps | default |
|---|---|---|
| `REPO_SIGNALS_MAX_CALLS` | GitHub requests | 25 |
| `REPO_SIGNALS_PROVIDER_MAX_CALLS` | people-provider requests | 25 |
| `REPO_SIGNALS_SHEETS_MAX_CALLS` | Google requests, including the token and each append | 10 |

Each must be a whole number from 1 to 500. When a stage fails or hits its cap, that stage stops,
later stages run on what exists, and the run ends `partial` with the stage and code, for example
`run: partial (people: budget_exhausted)`.

Dates are compared in UTC: "today" is the UTC date, matching GitHub's `pushed_at` times.

At the end the run prints the requests actually made, from each budget:
`requests made: github 4 of 10, provider 2 of 5, sheets 4 of 10`. The collect summary also shows
the GitHub total split into matching calls and searches.

## Dry run first

Dry run is the default and needs no flag. It makes **no network call and writes nothing**. It
runs the whole pipeline offline on one synthetic Evidence item on `example.invalid`, with the
fake provider. If the five sender variables are not all set, it uses a synthetic placeholder
sender. It prints:

- the stage counts;
- the column layout;
- exactly one sample row, with the email masked (`f***@example.invalid`);
- one sample draft, with the email masked.

## The gates for --live

`run --live` refuses, with exit code 2 and no request, unless **all** of these hold. A refusal
names only the variables or conditions that fail.

1. Dry run is off (`REPO_SIGNALS_DRY_RUN=false`).
2. `REPO_SIGNALS_SIGNAL_FILE` is set and the file parses.
3. The five sender variables are set (see [drafting.md](drafting.md)).
4. The selected provider's own variables are set (for example `HUNTER_API_KEY`).
5. `REPO_SIGNALS_SHEET_ID` and `REPO_SIGNALS_GOOGLE_CREDENTIALS` are set.
6. The suppression list, if set, parses.
7. The GitHub plan fits `REPO_SIGNALS_MAX_CALLS` and the search-rate ceiling.
8. stdin and stdout are a terminal.

`doctor` checks gates 1 to 7 offline and prints `run --live is ARMED` (WARN) or lists the failing
gates by name.

Then the run prints the plan: targets, terms, the most calls each service could use against its
budget, the provider name, the first 4 characters of the sheet id, and
`nothing is ever sent by email`. For the sheet, the plan says 5 calls for one append, plus 1
per extra append as needed. The number of appends depends on how many people are found and how
long their rows are, which is not known until after the people stage, so the plan cannot give
an exact worst case; `REPO_SIGNALS_SHEETS_MAX_CALLS` is the hard limit. You must type exactly `yes`. **There is no flag to skip the
prompt.** The real GitHub client, the people provider and the sheet writer are built only after
`yes`.

## First live run checklist

- [ ] `repo-signals probe-org <org>` works (see [live-collection.md](live-collection.md))
- [ ] `repo-signals run` (the dry run) looks right
- [ ] a **throwaway sheet**, shared with the service account, with nothing else in it
- [ ] one target with a named `org`, and one term
- [ ] tiny budgets, for example `REPO_SIGNALS_MAX_CALLS=3`, `REPO_SIGNALS_PROVIDER_MAX_CALLS=1`,
      `REPO_SIGNALS_SHEETS_MAX_CALLS=5`
- [ ] `repo-signals doctor` shows `run --live is ARMED`
- [ ] read every row and draft in the sheet before doing anything with it

## What is written where

Only the sheet is written to, and only by appending rows in the fixed layout:

| column | value |
|---|---|
| `name`, `email`, `title`, `company_domain`, `source`, `confidence` | from the people provider |
| `signal_evidence` | the Evidence summary, a space, and the Evidence URL |
| `email_draft` | the draft (subject and body), **only on a new row** |
| `reviewed` | always empty |
| `run_id` | a fresh random hex id for this run |
| `written_at` | the UTC time of the write |

If a sheet append fails part-way, the appends before it stay written and the run prints
`sheet: stopped (CODE): N row(s) written, M not written`. Running again skips the rows already
written and appends the rest.

Every value from outside passes the formula guard. A person already in the sheet (same email, or
same name, title and company domain when there is no email) is **skipped and never updated**, so
a person's edits to `email_draft` or `reviewed` are never touched.

## What is never stored or sent

- Nothing is ever sent by email. The package has no email-sending code.
- Nothing is written to disk. Output goes to the terminal.
- No token, no full email address and no sheet id (beyond its first 4 characters) is printed.
- Only the people provider's, GitHub's and Google's own APIs are called, within their budgets.

## Exit codes

| code | meaning |
|---|---|
| 0 | complete (or a finished dry run) |
| 1 | declined at the prompt: nothing was requested |
| 2 | refused before starting: nothing was requested |
| 3 | partial: a stage failed or hit its budget; later stages worked on what existed |
| 4 | refused by a stage, for example a sheet whose headers are not the tool's |
