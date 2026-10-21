# Matching companies to GitHub organisations

`src/repo_signals/collector/matching.py` decides which GitHub organisation, if any, each signal
target stands for. The rule is **never guess silently**: a target that cannot be matched with
confidence is reported and left out. The design is in
[collector-design.md](collector-design.md), section 3.

Matching takes any client with `get_org(login)` and `search_orgs(name)`, such as the collector's
GitHub client ([github-client.md](github-client.md)). It contains no HTTP code, and the tests
use a fake client. It is not reachable from the command line yet.

## Method A: you name the org

```toml
[[targets]]
domain = "example.com"
org = "example-org"
```

1. The org is looked up. If it does not exist, the target is `not_found`. If it is not an
   organisation (a user account, for example), the target is `not_an_org`.
2. If the org's website is on exactly `example.com` or `www.example.com`, the target is
   matched with confidence `user`.
3. If the org lists **no website**, there is nothing to check the domain against, so the target
   is **excluded** (`website_mismatch`, code `no_website`) unless the target sets
   `confirm_org = true`. Then it is matched with confidence `user` and the report says
   `confirmed`.
4. If the website is on **any other domain**, the target is **excluded** as `website_mismatch`,
   unless the target sets `confirm_org = true`. Then it is matched with confidence `user` and the
   report says `confirmed`.

## Method B: you give only a domain

1. The organisation search uses the label left of the top-level domain: `example` for
   `example.com` or `shop.example.com`.
2. At most 3 candidate organisations are looked up.
3. A candidate passes only if its website is on exactly the target domain (or `www.` plus it)
   **and** GitHub marks it as verified.
4. Exactly one passing candidate gives a match with confidence `verified-domain`. Two or more
   give `ambiguous`.
5. A candidate whose website matches but which is not verified gives confidence `website-only`.
   It is reported for you to confirm, and **never included**.
6. Candidates that only matched by name give `unmatched` with confidence `name-only`. Their logins
   (at most 3) are listed as suggestions. **A name match never produces Evidence.**

## The website check

The org's website value is parsed strictly:

- The scheme is optional, and only `http` or `https` is accepted.
- The host is compared in lowercase, and it must be exactly the domain or `www.` plus the domain.
- Userinfo (`user@`), a port, a backslash, whitespace or control characters inside the value, a
  trailing dot, or a host that is not a plain hostname all fail.
- A host that only ends with the domain does not match. For example, `evilexample.com`,
  `sub.example.com` and `example.com.evil.test` do not match `example.com`. Nor does
  `evil.test/example.com`, whose host is `evil.test`.

## What is included

| status | confidence | included |
|---|---|---|
| `matched` | `user` | yes |
| `matched` | `verified-domain` | yes |
| `matched` | `website-only` | no, report only |
| `website_mismatch` (website on another domain, or no website) | `user` | no, until `confirm_org = true` |
| `ambiguous` | | no |
| `unmatched` | `name-only` or none | no |
| `not_an_org`, `not_found` | | no |
| `rate_limited`, `error` | | no |

`MatchReport.usable` holds only the included results.

## Errors and rate limits

A client error never aborts the batch. It becomes that target's status: `not_found`,
`rate_limited`, or `error` with a code (for example `server_error`, or `client_error` for
anything unexpected). A candidate that cannot be looked up in method B is skipped, so it cannot be
accepted.

**A rate limit stops matching for every later target.** Those targets are reported as
`not attempted (rate limited earlier in this run)`, and no further call is made.

## The report

One line per target, in order. A line holds only domains, org logins, confidence labels and codes.
It never contains website text, descriptions or error messages. Synthetic examples:

```
example.com -> example-org (user)
a.example.com -> example-labs (user, website_mismatch: excluded; set confirm_org = true to include)
g.example.com -> example-new (user, no website listed: excluded; set confirm_org = true to include)
b.example.com -> example-hq (user, confirmed)
c.example.com: example-user is not an organisation
d.example.com: org example-gone not found
shop.example.invalid: ambiguous (2 candidate orgs: cand1, cand2; add org = "..." to confirm)
example.invalid: unmatched (no organisation passed the domain check); suggestions: example-org
e.example.com: not matched (rate_limited)
f.example.com: not attempted (rate limited earlier in this run)
```

The report also counts the targets by status.
