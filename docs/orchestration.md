# Collector orchestration

`src/repo_signals/collector/orchestrate.py` runs the collector's steps in order and turns a signal
file into Evidence for the drafter. It is **not wired to the command line**: `repo-signals
collect` without `--dry-run` still prints `live collection is not implemented yet` and exits 2.
It takes any client with `get_org`, `search_orgs` and `search_repos`. It never builds one and
contains no HTTP code, and the tests use a fake client.

```python
from repo_signals.collector.orchestrate import collect, evidence_lookup_from, summarize

result = collect(signal, client, suppression, budget_cap, today)
print("\n".join(summarize(result)))
rows = fill_drafts(rows, TemplateDrafter(), evidence_lookup_from(result))
```

## The steps

1. **Plan first.** The plan is built before anything else (see [signal-file.md](signal-file.md)).
   A plan over the call budget or over the search-rate ceiling is **refused**, and the client is
   never called. The result is `refused`, with a code such as
   `plan_over_budget: 27 planned, 25 allowed`.
2. **Suppression.** Targets whose domain is on the suppression list are skipped before matching.
3. **Matching.** Each remaining target is matched to an organisation
   (see [matching.md](matching.md)). Only usable matches are searched: confidence `user` or
   `verified-domain`. Unmatched, ambiguous, `website_mismatch`, `website-only` and `name-only`
   targets get no search.
4. **Searches.** Each usable target gets one repository search per term, matching the term
   against repository names only (`in:name`). The search passes the
   signal's language, if set, and a push date of today minus `pushed_within_days`.
5. **Filters.** Every item is read from its allowlisted keys, then passed through the existing
   filters: organisation owner, not a fork, not archived, not private, a well-formed name and
   link, inside the push window, and the right language (see [evidence.md](evidence.md)).
6. **The term must be in the repository name.** See the next section.
7. **Evidence.** Each surviving item becomes one `repo` Evidence item from the fixed template.
   There are at most 10 per target, and duplicates by URL are dropped.

## Why the term must be in the name

The repository search asks GitHub for name matches only (`in:name`, an unverified qualifier;
see [github-client.md](github-client.md)). The Evidence summary says:

`The public repository example-org/examplelib-tools matches the term examplelib, last pushed on 2026-09-30.`

The drafter quotes that sentence **verbatim** in an email draft, and a person may send it. If the
term appeared only in the description or topics, the sentence would not be literally true of the
repository's name, and the description itself is never used (it is free text). So the term
check stays as a **second guard**, in case the search returns anything else: an item becomes
Evidence only if the term appears in the repository name, ignoring case. Every other hit is
dropped and counted as `term_not_in_name`.

## Partial results

A client error never escapes `collect`.

- `rate_limited` or `budget_exhausted`, during matching or during a search, **stops
  everything**. No further call is made. What was collected so far is kept, and the result is
  `partial` with that code.
- Any other error for one search (for example `server_error`) is recorded, and collection
  continues with the next search. The result is `partial`.
- Skipped searches are listed by target domain, term position and code, for example
  `search skipped: example.com term 2: rate_limited`. A term is named by its position in the
  signal file, never by its text.

## Drop codes

Besides the filter codes in [evidence.md](evidence.md), orchestration counts:

| code | meaning |
|---|---|
| `term_not_in_name` | the term is not in the repository name |
| `duplicate_url` | the same repository already gave Evidence for this target |
| `over_target_cap` | the target already has 10 Evidence items |

## The report

`summarize(result)` returns code-only lines: the status, the planned request count and the
requests actually made (matching calls plus searches; a call refused before sending, such as
`budget_exhausted`, is not counted),
the suppressed domains, a count by match status, the match lines, Evidence counts per domain,
drop counts and skipped searches. The lines hold domains, org logins, labels, codes and numbers
only: never a description, a website, an error message or other free text. A synthetic example:

```
collect: partial (rate_limited)
planned requests: 6; requests made: 4 (matching 2, searches 2)
matching: matched 2
example.com -> example-org (user)
example.invalid -> example-labs (user)
evidence: example.com: 1
evidence: example.invalid: 0
kept: 1
search skipped: example.com term 2: rate_limited
search skipped: example.invalid term 1: not_attempted
search skipped: example.invalid term 2: not_attempted
```

## Hand-off to the drafter

`evidence_lookup_from(result)` returns a function from domain to a tuple of Evidence, which
`drafting.sheet.fill_drafts` accepts as its `evidence_lookup`. An unknown domain gives an empty
tuple, so that person gets no draft.
