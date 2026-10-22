# Changelog

## 0.1.0 (2026-10-22)

The first release. Nothing is ever sent by email: Repo Signals writes drafts into a sheet, and a
person reviews and sends each one. Dry run is the default, and every live command needs dry run
turned off, a terminal and a typed "yes".

### Checked live

These were checked against the real services on 2026-10-22:

- **GitHub collect:** the organisation lookup, the organisation search, the repository search
  and its qualifiers.
- **Hunter domain search (one lookup):** the endpoint, the `X-API-KEY` header, the `domain` and `limit`
  parameters, and the response fields for email, position, first and last name and confidence.
- **Apollo people search (one lookup):** the `api_search` path, the `x-api-key` header, the request body
  (`q_organization_domains_list`, `person_titles`, `per_page`), the `people` key, and the
  `first_name` and `title` fields. Search returns first names and titles and no email
  addresses.
- **Google Sheets:** the token exchange, signing with openssl, the header check, the append and
  the duplicate skip.
- **`repo-signals run`**, end to end with the fake people provider. Hunter and Apollo were each
  checked with a direct lookup, not through `run`.

### Not checked live

- **Hunter:** the seniority and department filters, the credits field, the error ids and the
  `Retry-After` behaviour.
- **Apollo:** what `has_email` means, the largest `per_page`, the error ids and the `Retry-After`
  behaviour.

The full lists are in [docs/hunter.md](docs/hunter.md), [docs/apollo.md](docs/apollo.md),
[docs/google-sheets.md](docs/google-sheets.md) and [docs/github-client.md](docs/github-client.md).

### Not built

- **Apollo email enrichment.** If it is ever added, it must be opt-in and have its own call
  budget (see [docs/apollo.md](docs/apollo.md)).
- **`doctor --online`.** The flag is accepted but does nothing yet.
- **A headless live mode.** This is a deliberate non-goal: a person reads the plan and types
  "yes" before every live run.
