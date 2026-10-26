# Design: a Hugging Face source for the collector

Status: **build steps 1 to 3 of 7 built** (section 12): the signal file's `source` key, the
Hugging Face target rules, the request plan, `REPO_SIGNALS_HF_MAX_CALLS` and `collect --dry-run`
(step 1); the evidence filters and templates as pure functions (step 2, `collector/hf_fields.py`
and `collector/hf_evidence.py`); and the client behind a fake transport (step 3,
`collector/hf_api.py`: the overview, listings, `Link` cursor pagination with a page cap,
`RateLimit` parsing and sticky stops). None of it is wired to a command and nothing has made a
real request: `collect --live` and `run --live` refuse a Hugging Face file with
`huggingface_not_implemented`. This page describes how Repo Signals gains a second collector
source, Hugging Face, inside this repository. The GitHub collector
([collector-design.md](collector-design.md)) keeps working unchanged: `source` defaults to
`github`, so every existing signal file behaves exactly as before. Every Hugging Face detail named
here is either in the "Checked live on 2026-10-26" list in section 10 or still unchecked.

## 1. Goal and non-goals

The goal is the same as the GitHub collector's: turn a signal file into `models.Evidence` items
per company domain, using only **public** data, so the existing people lookup, drafter and sheet
writer can work on them. Hugging Face models, datasets and Spaces are git repositories, so the
product name and its settings still fit.

In scope:

- Public models, datasets and Spaces **owned by an organisation** the user names.
- The public listing and organisation endpoints of the Hugging Face API, read anonymously, with
  an optional token only for higher rate limits (unchecked).

Not in scope, deliberately:

- Matching a company to an organisation automatically. Hugging Face organisation profiles have
  no website field (section 4), so nothing can prove the link; the user confirms it.
- Personal accounts, members, authors, likers or followers.
- Reading free text: the organisation bio, model cards, dataset cards, READMEs, descriptions or
  discussions.
- Private, gated or disabled repositories, and anything visible only with a token.
- Scraping HTML pages, downloading files, or calling inference.
- Sending anything. As today, a person reviews and sends every draft.

## 2. What does not change

- The sheet writer, the duplicate rule, append batching, the header preflight and the read limit
  ([google-sheets.md](google-sheets.md)).
- The drafter, the guard and the suppression list ([drafting.md](drafting.md)). The guard checks
  that the evidence summary and URL appear in the draft, whatever the source.
- The people providers (fake, Hunter, Apollo): they take the target's domain.
- The budgets pattern: every request charged to its own `CallBudget` before it is sent, hard
  bound of 500, a plan refused before any request if it does not fit.
- The gates, the typed "yes", the terminal requirement, dry run as the default, and the doctor.
- The safety guarantees: never sends email, never deletes or overwrites sheet data, the host
  allowlist, no redirects, response and request size caps, code-only errors, the formula guard,
  and the scanner.

## 3. The signal file change

One optional top-level key, `source`, with the values `"github"` (the default) and
`"huggingface"`. A file without `source` is a GitHub file, so existing files are unchanged. A file
has one source; to collect from both, use two files and two runs.

For `source = "huggingface"`, every target needs both an `org` and `confirm_org = true`. There is
no website to match (section 4), so the user's own confirmation is the only acceptable link
between a company and an organisation. A target without them is refused with a code before
anything is requested.

```toml
source = "huggingface"
name = "examplelib-hf"
terms = ["examplelib"]
modified_within_days = 365
kinds = ["model", "dataset", "space"]

[[targets]]
domain = "example.com"
org = "example-org"
confirm_org = true
```

- `kinds` is optional and defaults to all three.
- `modified_within_days` (1 to 3650, default 365) is the window on `lastModified`.
  `pushed_within_days` and `language` are GitHub-only and refused with this source.
- `terms`, `titles`, the target `domain` and the caps on terms and targets are unchanged.
- Domain-only targets are refused: there is no domain match to fall back on.

## 4. Matching a company to an organisation

`GET /api/organizations/{name}/overview` returns `isVerified`, counts of models, datasets and
Spaces, and a free-text bio. It has **no website, blog or domain field**, so the GitHub rule
(the organisation's website must be on the target's domain) cannot be applied.

The rule for Hugging Face:

1. The target names the organisation (`org`) and sets `confirm_org = true`.
2. One overview request checks that the organisation exists. A 404 marks the target unresolved
   and nothing more is requested for it. Users are a separate endpoint, so a personal account
   never resolves as an organisation.
3. `isVerified` is shown in the match line as supporting information only, for example
   `example.com -> example-org (huggingface, confirmed, verified)`. It never includes or
   excludes a target on its own: it says Hugging Face verified the organisation, not which
   company it belongs to.
4. The counts may skip a listing whose count is zero, saving a call.

The bio is never read, kept or printed.

## 5. The signals

For each resolved target, the collector lists the organisation's models, datasets and Spaces
(the `kinds` asked for) and keeps an item only if all of these hold:

- **Org-owned:** the `id` is `ORG/NAME` and its `ORG` part equals the target's organisation.
- **The term literally appears in the name:** the `NAME` part contains one of the signal's terms,
  ignoring case. The listing's own search ignores case but is not a substring match (it seems to
  match from the start of hyphen-separated name parts), so the collector lists by author and
  checks names itself rather than relying on `search`.
- **Recent:** `lastModified` is within `modified_within_days` of today (UTC). The listing is sorted
  by `lastModified` descending, so paging stops at the first page that ends outside the window.
- **Public:** `private` is false. Gated and disabled items are dropped too (see section 11).
- Only allowlisted fields are kept (section 9). Downloads, likes, tags and descriptions are not
  evidence and are discarded.

Each drop is counted under a code, as the GitHub filters do.

## 6. Evidence output

Each accepted item becomes one `models.Evidence(company_domain, summary, url, kind)`, built only
from the listing data. The URL is built from the item's `id` and a fixed prefix; it is never
requested, probed or followed.

| kind | URL | summary template |
|---|---|---|
| `hf-model` | `https://huggingface.co/{org}/{name}` | `The public Hugging Face model {org}/{name} matches the term {term}, last modified on {date}.` |
| `hf-dataset` | `https://huggingface.co/datasets/{org}/{name}` | `The public Hugging Face dataset {org}/{name} matches the term {term}, last modified on {date}.` |
| `hf-space` | `https://huggingface.co/spaces/{org}/{name}` | `The public Hugging Face Space {org}/{name} matches the term {term}, last modified on {date}.` |

`{date}` is the `lastModified` date as `YYYY-MM-DD` (the API sends ISO 8601 UTC with milliseconds
and a trailing Z). The same rules as today apply to every summary: one printable line, at most 300
characters, no URL and no `@` in the summary, every inserted value checked (the `org/name` shape,
the term being one of the signal's terms, the date parsing), and a hit that fails a check is
dropped, never patched. At most 10 Evidence items per target, deduplicated by URL.

## 7. Request plan and budget

Per resolved target, at most:

- 1 overview request;
- then 1 listing per kind (up to 3), each with `author`, `sort=lastModified`, `direction=-1`, a page
  size of at most 500 and `expand` for the allowlisted fields (models need `expand` for `author`,
  `lastModified` and `gated`);
- with at most 2 pages per listing, following the `Link` header's `rel="next"` cursor.

So the upper bound is 1 + 3 x 2 = 7 requests per target. The plan counts this bound before
anything is requested and refuses the run (code only) if it does not fit.

- A separate budget, `REPO_SIGNALS_HF_MAX_CALLS` (1 to 500, default 25), caps Hugging Face
  requests. It is charged before each request, like `REPO_SIGNALS_MAX_CALLS` for GitHub.
- The plan line names it, for example
  `max calls: huggingface 14 of 25; provider at most 2 of 25; sheets 6 for the header check and
  one append, ...`.
- The plan is also refused if it would pass a share of the anonymous rate limit (500 requests per
  300-second window), keeping a margin.
- An optional `REPO_SIGNALS_HF_TOKEN`, sent only in a header, may raise limits (unchecked).

## 8. Errors and sticky stops

Every failure is a code-only error, never a URL, header or body.

| response | code | effect |
|---|---|---|
| overview 404 | `org_not_found` | this target is unresolved; nothing more is requested for it |
| listing 200 with an empty list | none | no evidence for that kind; an empty listing never proves the organisation exists, which is why the overview comes first |
| any 401 | `unauthorised` | sticky stop: an unknown or private repository returns 401, and with a token it may mean the token was rejected |
| 429 | `rate_limited` | sticky stop for the run; the reset time is read from the `RateLimit` header's `t=` seconds when present |
| `RateLimit` shows 0 remaining | none | the next request is refused before it is sent |
| 5xx | `server_error` | this request fails; the run continues |
| a page of exactly the page size with the page cap reached, or a full 1,000 page | `listing_capped` | possibly incomplete: the result is marked partial |
| JSON in another shape | `unexpected_shape` | this request fails |
| a redirect | `redirect_refused` | never followed (renamed repositories are unchecked) |

Limits above 1,000 are silently capped at 1,000, so the collector never asks for more than it can
check, and treats a full page as possibly incomplete.

## 9. Privacy

- **Allowlisted fields only:** from the overview, `name` and `isVerified` (and the counts); from
  listings, `id`, `author` where present, `lastModified`, `private`, `gated` and `disabled`.
  Everything else is discarded at once and never reaches Evidence, a log or the output.
- **No free text:** the organisation bio, model and dataset cards, descriptions, READMEs, tags and
  discussions are never read into Evidence.
- **No personal accounts:** only organisations resolve (the users endpoint is separate and never
  called), and items whose owner is not the target's organisation are dropped.
- The evidence names the repository, never a person, and says "matches the term", never "uses".

## 10. Checked and unchecked API details

Checked live on 2026-10-26 (anonymous, read-only, org `huggingface`):

- [x] `GET /api/organizations/{name}/overview` returns 200 with `isVerified`, counts (models,
      datasets, Spaces) and a free-text bio, and no website, blog or domain field. An unknown
      organisation returns 404. Users are a separate endpoint.
- [x] `GET /api/models`, `/api/datasets` and `/api/spaces` accept `author`, `search`,
      `sort=lastModified`, `direction=-1` and `limit`; anonymous reads work. Dates are ISO 8601
      UTC with milliseconds and a trailing Z.
- [x] Default fields differ: models lack `author`, `gated` and `lastModified` unless requested
      with `expand`; datasets carry `author`, `lastModified` and `gated`; Spaces lack `author` and
      `downloads`. `private` was false on every anonymous result.
- [x] Search ignores case but is not a substring match (it seems to match from the start of
      hyphen-separated name parts), so the tool must still check the term against the name
      itself.
- [x] Pagination is a `Link` header with `rel="next"` and an opaque cursor; a limit above 1,000
      is silently capped at 1,000.
- [x] A listing for an unknown author returns an empty 200; an unknown or private repository
      returns 401.
- [x] The anonymous rate limit is 500 requests per 300-second fixed window, shared across
      endpoints, reported in the `RateLimit` and `RateLimit-Policy` headers.
- [x] Public URL shapes: `huggingface.co/ORG/NAME` (model), `/datasets/ORG/NAME`,
      `/spaces/ORG/NAME`.

Still unchecked, so check each of these before relying on it:

- [ ] the 429 body and `Retry-After`
- [ ] gated and private behaviour
- [ ] the token quota and scope
- [ ] redirects for renamed repositories
- [ ] user-owned repositories
- [ ] the exact search tokenisation
- [ ] Hugging Face's terms for automated access and for outreach use

## 11. Open questions

1. **Gated items:** drop them (the default above), or keep them as public evidence of existence?
2. **Organisation name shape:** which characters Hugging Face allows in an organisation name, so
   the signal file can check it before any request.
3. **Large organisations:** is 2 pages of 500 per kind enough, or should `search` be used to
   narrow very large organisations, accepting that it can miss terms inside a word?
4. **The window key:** `modified_within_days` for Hugging Face, or one neutral key for both sources?
5. **isVerified false:** include a confirmed but unverified organisation (the default above), or
   require verified?
6. **The draft wording:** the intro template says "public GitHub repositories". Hugging Face
   evidence needs neutral wording. That is a template text change only; the drafter and the
   guard stay as they are.
7. **Terms of use:** confirm Hugging Face's terms allow this automated access and the outreach use
   before the first live run.

## 12. Build plan (small reviewed PRs, dry run first)

Each PR is offline-tested with fakes and keeps dry run as the default. The GitHub path stays
green throughout.

1. **Signal file and plan.** The `source` key (default `github`), the Hugging Face target rules
   (`org` and `confirm_org = true` required, `kinds`, `modified_within_days`), the request plan
   with page caps, `collect --dry-run` output and the doctor's counts. No network code.
2. **Evidence and filters.** Pure functions from synthetic listing records to Evidence: the field
   allowlist, owner check, name check, window, public-only, URL building and the three templates.
3. **The client behind a fake transport.** One constants block with the checked and unchecked
   lists, the host allowlist, charging before each request, the overview and listings, `Link`
   pagination with the page cap, `RateLimit` parsing and the sticky stops. Unreachable from the
   command line.
4. **Matching.** The overview check, `confirm_org`, the `isVerified` note and the report lines.
5. **Orchestration.** `collect` picks the client by the signal's source; Hugging Face results go
   through the same report, drop counts and partial status.
6. **The live switch.** A one-request organisation probe (the Hugging Face equivalent of
   `probe-org`) and the gated `collect --live`, then a live check that moves items from the
   unchecked list to the checked list.
7. **Run wiring and docs.** `REPO_SIGNALS_HF_MAX_CALLS`, the optional token, the gates and plan
   line, the intro template wording, the setup guide, troubleshooting codes and the changelog.
