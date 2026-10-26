# Evidence from GitHub records

This page describes how the collector turns one GitHub repository record into one
`models.Evidence` item, and what it drops on the way. Everything here is a pure function with no
network: `collector/github_fields.py`, `collector/filters.py` and `collector/evidence.py`. The
design is in [collector-design.md](collector-design.md), sections 5 and 6.

## Step 1: read only an allowlist

`parse_repo(raw)` reads exactly these keys from a repository record, and nothing else:

| key | kept as |
|---|---|
| `owner.login` | `owner_login` |
| `owner.type` | `owner_type` |
| `name` | `name` |
| `full_name` | `full_name` |
| `html_url` | `html_url` |
| `language` | `language` (or none) |
| `pushed_at` | `pushed_at`, as a date (`YYYY-MM-DDTHH:MM:SSZ` only) |
| `fork`, `archived`, `private` | `fork`, `archived`, `private` (must be `true` or `false`) |

Every other field is never read. That includes the description, README text, topics, homepage,
license, commit authors, contributors, and the owner's name, email or blog. Because the record
is never read beyond the allowlist, none of it can reach Evidence.

A record that is missing a key, has the wrong type, or has a string that is too long gives
**no fact** (`malformed_record` in the report). Parsing never raises.

These key names, and the GitHub web host the links must point to (`WEB_HOST`), are unverified
general knowledge. Verify them against GitHub's current documentation before first live use.

## Step 2: filters

Each filter returns `(ok, code)`. A record is kept only if all of them pass. A dropped record is
counted under its code, never dropped silently.

| code | the record is dropped when |
|---|---|
| `owner_not_org` | the owner is not an organisation. A repository owned by a user identifies a person, so it is dropped even when it matches. |
| `fork` | it is a fork |
| `archived` | it is archived |
| `private` | it is private |
| `bad_full_name` | `full_name` is not `owner/name` with safe characters, or does not equal the owner login and name |
| `url_not_https` | `html_url` is not https |
| `url_other_host` | `html_url` is not on the GitHub web host exactly: no other host, subdomain, port or user part |
| `url_extra_path` | the path is not exactly two segments, or there is a query or fragment |
| `url_full_name_mismatch` | the path is not exactly `/` + `full_name` |
| `url_malformed` | the link does not parse |
| `outside_window` | it was last pushed more than `pushed_within_days` days before today. A push exactly that many days ago is kept. |
| `pushed_in_future` | its push date is after today |
| `language_mismatch` | the signal sets a `language` and the repository's language differs, ignoring case |
| `malformed_record` | the record could not be parsed (step 1) |

`summarize_report(report)` returns code-only lines such as:

```
kept: 2
dropped 1: fork
dropped 2: malformed_record
dropped 1: owner_not_org
```

## Step 3: fixed summary templates

`build_evidence(fact, term, kind, company_domain, today, terms=signal.terms)` builds the summary
from one fixed template. There is only one kind in v1, `repo`. There is no `topic` kind and no
`file` kind (decided).

| kind | template |
|---|---|
| `repo` | `The public repository {full_name} matches the term {term}{language_clause}, last pushed on {pushed_date}.` |

`{language_clause}` is empty when the repository has no language. Otherwise it is
` and its primary language on GitHub is {language}`.

Synthetic examples:

```
The public repository example-org/examplelib-tools matches the term examplelib and its primary language on GitHub is Python, last pushed on 2026-09-30.
The public repository example-org/examplelib-js matches the term examplelib, last pushed on 2026-09-30.
```

Every inserted value is checked, and the hit is dropped (not patched) with a code when a check
fails:

| code | when |
|---|---|
| `bad_kind` | the kind is not `repo` (`topic` and `file` are refused) |
| `term_not_in_signal` | the term is not exactly one of the signal's own terms |
| `owner_not_org`, `bad_full_name`, `url_*` | the same checks as the filters, repeated as a safeguard |
| `bad_language` | the language is not a short name: letters, digits, `+ # . -` and single spaces, starting with a letter, at most 32 characters |
| `bad_pushed_date` | the date is not `YYYY-MM-DD` or is after today |
| `summary_too_long` | the summary is over 300 characters |
| `summary_not_printable`, `summary_has_at`, `summary_has_url` | the summary has a control character, an `@`, or anything that looks like a URL |
| `bad_evidence` | `Evidence` itself refuses the result (for example, a bad company domain) |

The link goes only in `Evidence.url`, never inside the summary.

When the collector runs (see [orchestration.md](orchestration.md)), a hit becomes Evidence only if
the term appears in the repository **name**, ignoring case. A hit that matched only through the
description or topics is dropped as `term_not_in_name`, because the summary says "matches the
term" and the drafter quotes it verbatim.

## Why the summaries are fixed

The drafter quotes each summary verbatim in an email draft. A fixed template built from checked
fields cannot carry a person's name or email, a claim about the company, a foreign link, or a
phrase that implies a past relationship. Free text from a description or README could.

## Personal data excluded

- Commit authors and committers, with their names and emails.
- Logins of individual users, including owners of user-owned repositories. Those repositories
  are dropped entirely.
- Contributors, stargazers, watchers and fork owners.
- Issue, pull request and discussion authors and text.
- README bodies, code, code-search fragments, descriptions and homepages.
- An organisation's public email address.

Nothing is written to disk.

## Hugging Face records

Build step 2 of the Hugging Face source ([huggingface-design.md](huggingface-design.md)) turns
Hugging Face listing records into Evidence with the same pattern. It is pure and not yet wired to
any command: no client exists.

1. **Allowlist** (`collector/hf_fields.py`): only `id`, `author` (where present), `lastModified`,
   `private`, `gated` and `disabled` are read. Descriptions, tags, downloads, likes, card data and
   file lists are never read. A record that is not a dict, or has no `id` or `lastModified`, or an
   `id` that is not `ORG/NAME` (letters, digits and `. _ -` in the name; the owner shape is
   unchecked for Hugging Face), or a date that is not ISO 8601 UTC with a trailing Z, is dropped
   under a code (`malformed_record`, `missing_id`, `missing_last_modified`, `bad_id`,
   `bad_last_modified`).
2. **Filters** (`collector/hf_evidence.py`), in order, each drop counted under a code:
   - owner: the id's owner equals the target's `org`, ignoring case, and an `author`, when sent,
     agrees (`owner_not_target`);
   - public: `private` is exactly false (`private`); gated and disabled items are dropped
     (`gated`, `disabled`), an unchecked default until gated and private items are checked live;
   - recent: last modified within `modified_within_days` of today in UTC, inclusive
     (`outside_window`); a future date is dropped (`modified_in_future`);
   - term: the name part contains one of the terms, ignoring case; a term found only in the owner
     part does not count (`term_not_in_name`).
3. **Fixed templates:** one sentence per kind, built from the kind, `org/name`, the term and the
   date only, with the URL built from a fixed prefix and the id and never requested:

| kind | URL | summary |
|---|---|---|
| `hf-model` | `https://huggingface.co/{org}/{name}` | `The public Hugging Face model {org}/{name} matches the term {term}, last modified on {date}.` |
| `hf-dataset` | `https://huggingface.co/datasets/{org}/{name}` | `The public Hugging Face dataset {org}/{name} matches the term {term}, last modified on {date}.` |
| `hf-space` | `https://huggingface.co/spaces/{org}/{name}` | `The public Hugging Face Space {org}/{name} matches the term {term}, last modified on {date}.` |

The summary rules are the same as for GitHub: one printable line, at most 300 characters, no URL
and no `@`, every inserted value checked, and a failing hit dropped, never patched. At most 10
items per target, deduplicated by URL (`over_target_cap`, `duplicate_url`). These drop codes are
internal counts for now; they will be printed in the collect summary once the source is wired in.
