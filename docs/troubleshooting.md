# Troubleshooting

Every failure in Repo Signals is a short code. A code never contains a key, a token, a URL, a
sheet id or anything read from a response or a file, so you can paste it anywhere when you ask
for help. Some codes carry a position or a variable name after a colon, for example
`sender_missing: REPO_SIGNALS_POSTAL_ADDRESS` or `plan_over_budget: 7 planned, 3 allowed`.

In the table, `N` stands for a number (a position in a file, or seconds) and `FIELD` for a
template placeholder name such as `company_domain`. A run that stops on a code prints the stage
too, for example `run: partial (people: budget_exhausted)`.

For a first setup, see [setup-guide.md](setup-guide.md).

## Start here

These are the codes most often seen in a first run, and what each one usually means then.
`header_mismatch`, `token_rejected`, `signing_failed`, `sheet_not_found`, `auth_failed`, and the
key-file and network codes can now appear right after the typed "yes", from the sheet preflight,
before any GitHub request or people lookup: the run then ends with
`run: refused (sheet preflight: CODE)`. `header_missing` still appears only at the sheet stage,
because the preflight reads only the first row.

- [`header_missing`](#header_missing): the sheet is not empty. It has data but no header row. Use
  a new, empty sheet.
- [`header_mismatch`](#header_mismatch): the sheet has someone else's header in its first row.
  Use a new, empty sheet.
- [`not_found`](#not_found): a GitHub organisation login in the signal file is wrong. A sheet
  that is not shared with the service account shows up as `sheet_not_found` or `auth_failed`
  instead.
- [`token_rejected`](#token_rejected): GitHub refused your token (expired, revoked or mistyped),
  or Google refused the service-account token (the key was deleted, or your clock is wrong).
- [`signing_failed`](#signing_failed): openssl could not sign with the key file. The file is
  incomplete or was edited: download a fresh key.
- [`org_not_visible`](#org_not_visible): GitHub will not search that organisation. The login is
  wrong, or the token belongs to an account that cannot see it, often a second `gh` account.
- [`rate_limited`](#rate_limited): you hit GitHub's rate limit. Wait, or set
  `REPO_SIGNALS_GITHUB_TOKEN` for higher limits.

## Error codes

| code | what it means | what to check |
|---|---|---|
| `dry_run` | A live GitHub or Hugging Face request was attempted while dry run is on. | Set `REPO_SIGNALS_DRY_RUN=false` in the shell where you run the live command, and only there. |
| `budget_exhausted` | The call budget for this service ran out. The request was not sent. | Raise the cap for the stage the run names: `REPO_SIGNALS_MAX_CALLS` (GitHub), `REPO_SIGNALS_HF_MAX_CALLS` (Hugging Face), `REPO_SIGNALS_PROVIDER_MAX_CALLS` (people) or `REPO_SIGNALS_SHEETS_MAX_CALLS` (Google). Each is at most 500. |
| `rate_limited` | The service said to slow down: GitHub answered 403 or 429, or reported 0 requests left; Hugging Face answered 429, or its `RateLimit` header showed 0 left; a provider or Google answered 429 with no usable `Retry-After`. The tool never waits or retries. | Wait and run again later. For GitHub, set `REPO_SIGNALS_GITHUB_TOKEN` for higher limits. `probe-org` shows the remaining count and reset time. <a id="rate_limited"></a> |
| `retry_after_N` | A provider or Google answered 429 and asked you to wait N seconds (0 to 60). | Wait at least N seconds before running again. |
| `token_rejected` | GitHub answered 401 for your token, or Google's token endpoint answered 400. | GitHub: the token is expired, revoked or mistyped; make a new one and set it with `read -rs`. Google: the key was deleted in the console, the key file is from another project, or your computer's clock is wrong. <a id="token_rejected"></a> |
| `not_found` | GitHub answered 404, usually for an organisation login that does not exist. | The `org` spelling in the signal file. `repo-signals probe-org` checks one login with one request. <a id="not_found"></a> |
| `org_not_visible` | GitHub refused to search inside that organisation (a 422 with the "cannot be searched" message). | The `org` name in the signal file, and whether your token can see that org. Check which account the token belongs to. <a id="org_not_visible"></a> |
| `bad_query` | GitHub refused a search with a 422 for any other reason, or Hugging Face refused a listing with a 400 or 422. | The term and `language` in the signal file. For Hugging Face, report it: the listing parameters are fixed. |
| `server_error` | The service answered with a 5xx error. | Nothing on your side. Try again later. |
| `unexpected_status` | The service answered with a status the tool does not expect. | Try again later. If it repeats, the API may have changed: see the unverified constants in the provider's doc. |
| `org_not_found` | Hugging Face answered 404 for the organisation overview: no organisation by that name. In `collect --live` the target is unresolved and the run continues; `hf-probe-org` shows it as the outcome. | The `org` value in the signal file: the `NAME` in huggingface.co/NAME. `repo-signals hf-probe-org` checks one name with one request. |
| `unauthorised` | Hugging Face answered 401. Anonymously it answers this for an unknown or private repository, so the two cannot be told apart. The client stops for the rest of the run, which ends as partial. | The organisation and repository names. |
| `bad_link` | A Hugging Face `Link` header for the next page pointed somewhere unexpected (another host or path, or a cursor that is not a short token of safe characters). It is never followed, and that listing is reported as incomplete. | Report it if it repeats: the API may have changed. |
| `listing_capped` | A Hugging Face listing still had more pages after the page cap (2 pages of up to 500 items), so the result may be incomplete. The records already read are kept and the run ends as partial. | Use fewer kinds or narrower terms, or check the organisation by hand. |
| `bad_expand` | The Hugging Face client was asked to expand a field outside its allowlist for that kind (Spaces do not accept `gated`). | Report it as a bug. |
| `bad_limit` | The Hugging Face client was given a page size that is not a positive whole number. | Report it as a bug. |
| `bad_cursor` | The Hugging Face client was given a page cursor that is not a short token of safe characters. | Report it as a bug. |
| `bad_max_pages` | The Hugging Face client was asked for more pages than the plan allows (at most 2). | Report it as a bug. |
| `unexpected_shape` | A 2xx answer was JSON, but not in the shape the tool expects. | The API may have changed. Check the unverified-details checklist in [hunter.md](hunter.md), [apollo.md](apollo.md), [google-sheets.md](google-sheets.md), [github-client.md](github-client.md) or [huggingface-design.md](huggingface-design.md). |
| `client_error` | A client failed in a way that carried no code. Only this code is printed, never the error text. | Run again. If it repeats, report it as a bug. |
| `malformed_json` | A 2xx answer was not JSON. | A proxy, VPN or captive portal answering in place of the service. |
| `timeout` | The request took longer than the timeout. | Your network connection. Try again. |
| `tls_error` | The secure connection failed. | A proxy or security tool intercepting HTTPS, or a wrong system clock. |
| `network_error` | The tool could not reach the service. | Your network connection, DNS and firewall. |
| `redirect_refused` | The service answered with a redirect. Redirects are never followed. | The endpoint may have moved. Check the provider's doc. |
| `response_too_large` | The answer was larger than 1 MB. | When reading a sheet's existing rows: the sheet has grown past the read limit (roughly 750 to 800 typical rows, see [google-sheets.md](google-sheets.md#the-read-limit)), and every later write fails here until you move older rows out of the sheet by hand. Otherwise nothing to set; report it if it repeats. |
| `insecure_scheme` | A request was about to go out over something other than https. | This is a safety stop. Report it as a bug. |
| `host_not_allowed` | A request was about to go to a host that is not on the allowlist. | This is a safety stop. Check for local edits to a constants block. |
| `bad_url` | A request URL was malformed. | Report it as a bug, or check local edits to a constants block. |
| `bad_method` | The HTTP client was asked for a method it does not support. | Report it as a bug. |
| `bad_header` | A request header held a character that is not allowed. | Re-set the key or token with `read -rs`: a pasted value may hold a stray character. |
| `bad_params` | The query parameters for a request were not valid. | Report it as a bug. |
| `bad_payload` | A request body could not be encoded. | Report it as a bug. |
| `payload_too_large` | A request body was over the 64 KB request limit. It was refused before sending, and no call was charged for it. | New rows are split into appends of at most 60 KB, so a sheet write should not hit this. If it does, report it as a bug. |
| `row_too_large` | One new row is so large that an append holding only that row would pass the 64 KB request limit. The write stopped there: appends before that row stay written, and that row and the rows after it were not written. No call was charged for it. | The provider's fields for that person (an unusually long name or title), and very long sender details or evidence in the draft. The run prints how many rows were written and how many were not. |
| `bad_transport_response` | A custom transport returned something that is not a response. | Only happens with your own transport in code or tests. |
| `bad_date` | The GitHub client was given a malformed "pushed after" date. | Report it as a bug. |
| `bad_org` | An organisation login does not have a valid shape. | The `org` value in the signal file. |
| `bad_name` | The name searched for a domain-only target is not valid. | Add an `org` to that target in the signal file. |
| `bad_term` | A search term is not valid. | The `terms` in the signal file: 2 to 60 characters, letters, digits and `. _ + -`. |
| `bad_language` | A language value is not valid, either in a search or on a repository. | The `language` in the signal file: one word, at most 32 characters. |
| `plan_over_budget` | The planned GitHub requests are more than `REPO_SIGNALS_MAX_CALLS`. Nothing was requested. | Raise `REPO_SIGNALS_MAX_CALLS`, or use fewer targets or terms. `collect --dry-run` shows the plan. |
| `plan_over_search_rate` | The planned searches are over the ceiling of 30 per minute. Nothing was requested. | Use fewer targets or terms per run. |
| `plan_over_hf_rate` | A Hugging Face plan is over its ceiling of 400 requests per 300 seconds (a margin below the anonymous limit). Nothing was requested. | Use fewer targets or kinds per run. |
| `signal_unreadable` | The signal file could not be read. | The path in `REPO_SIGNALS_SIGNAL_FILE` and the file's permissions. |
| `signal_path_malformed` | `REPO_SIGNALS_SIGNAL_FILE` is not a well-formed path. | Set it to the file's path, not its contents. |
| `signal_too_large` | The signal file is over 64 KB. | Split it into smaller files. |
| `signal_not_utf8` | The signal file is not UTF-8 text. | Save it as UTF-8. |
| `signal_not_toml` | The signal file is not valid TOML. | Quotes, brackets and `=` signs. See [signal-file.md](signal-file.md). |
| `signal_unknown_key` | The file has a top-level key the tool does not know. | Spelling. `filenames` is not part of v1. |
| `signal_bad_schema` | `schema` is set to something other than 1. | Remove it or set `schema = 1`. |
| `signal_missing_name` | The file has no `name`. | Add `name = "..."`. |
| `signal_bad_name` | `name` breaks the rules. | Lowercase letters, digits and `-`, starting with a letter, at most 48 characters. |
| `signal_bad_language` | `language` breaks the rules. | One word: letters, digits and `+ # . -`, at most 32 characters. |
| `signal_bad_pushed_within_days` | `pushed_within_days` is not a whole number from 1 to 3650. | Use a plain number, not a string. |
| `signal_bad_source` | `source` is not `"github"` or `"huggingface"`. | Use one of the two, or leave `source` out for GitHub. |
| `signal_github_refuses_KEY` | A GitHub file (no `source`, or `source = "github"`) has a Hugging Face key: `kinds` or `modified_within_days`. | Remove the key, or set `source = "huggingface"`. |
| `signal_hf_refuses_KEY` | A Hugging Face file has a GitHub key: `pushed_within_days` or `language`. | Remove the key; use `modified_within_days` for the window. |
| `signal_bad_kinds` | `kinds` is not a non-empty list of `model`, `dataset` and `space`. | For example `kinds = ["model", "dataset"]`, or leave it out for all three. |
| `signal_duplicate_kind` | A kind is listed twice in `kinds`. | List each kind once. |
| `signal_bad_modified_within_days` | `modified_within_days` is not a whole number from 1 to 3650. | Use a plain number, for example `365`. |
| `signal_missing_terms` | The file has no `terms`. | Add `terms = ["..."]`. |
| `signal_bad_terms` | `terms` is not a list of strings. | Use square brackets and quoted terms. |
| `signal_no_terms` | `terms` is empty. | Add at least one term. |
| `signal_too_many_terms` | There are more than 5 terms. | Use at most 5. |
| `signal_bad_term_N` | Term number N breaks the rules. | 2 to 60 characters: letters, digits and `. _ + -`, no spaces, quotes or colons. |
| `signal_duplicate_term` | Two terms are the same, ignoring case. | Remove the duplicate. |
| `signal_bad_titles` | `titles` is not a list of strings. | Use square brackets and quoted titles. |
| `signal_too_many_titles` | There are more than 5 titles. | Use at most 5. |
| `signal_bad_title_N` | Title number N breaks the rules. | Letters, digits, single spaces and `& . / + -`, at most 60 characters. |
| `signal_duplicate_title` | Two titles are the same, ignoring case. | Remove the duplicate. |
| `signal_missing_targets` | The file has no `[[targets]]`. | Add at least one target. |
| `signal_bad_targets` | `targets` is not a list of tables. | Write each target as a `[[targets]]` block. |
| `signal_no_targets` | `targets` is empty. | Add at least one target. |
| `signal_too_many_targets` | There are more than 50 targets. | Split them across files. |
| `signal_target_N_not_a_table` | Target number N is not a table. | Write it as a `[[targets]]` block. |
| `signal_target_N_unknown_key` | Target number N has a key the tool does not know. | Only `domain`, `org` and `confirm_org` are allowed. |
| `signal_target_N_bad_domain` | Target number N has a missing or malformed `domain`. | A hostname with at least one dot, for example `example.com`. |
| `signal_target_N_bad_org` | Target number N has a malformed `org`, for example a domain such as `example.com`. Nothing was requested. | Use the organisation name, the `NAME` in `github.com/NAME`: letters, digits and single inner hyphens, at most 39 characters. |
| `signal_target_N_bad_confirm_org` | `confirm_org` on target N is not `true` or `false`. | Use `true` or `false` without quotes. |
| `signal_target_N_confirm_org_without_org` | Target N has `confirm_org` but no `org`. | Add the `org`, or remove `confirm_org`. |
| `signal_target_N_hf_needs_org` | Hugging Face target N has no `org`. A domain-only target cannot be matched, because Hugging Face organisation profiles have no website field. Nothing was requested. | Add the organisation name as `org`, with `confirm_org = true`. |
| `signal_target_N_hf_needs_confirm_org` | Hugging Face target N has an `org` but not `confirm_org = true`. Your confirmation is the only link between a company and a Hugging Face organisation. | Set `confirm_org = true` only if you know the organisation belongs to the company. |
| `huggingface_not_implemented` | The signal file has `source = "huggingface"`, and `run --live` does not support Hugging Face yet: it refuses with nothing requested. `collect --dry-run` and `collect --live` work. | Use `collect --live` for a Hugging Face file, or a GitHub signal file for `run --live`. |
| `signal_duplicate_domain` | Two targets have the same domain, ignoring case. | Remove the duplicate. |
| `signal_duplicate_org` | Two targets have the same org, ignoring case. | Remove the duplicate. |
| `missing_key` | The provider's API key variable is unset or blank. | `HUNTER_API_KEY` or `APOLLO_API_KEY`, set in this shell with `read -rs`. |
| `bad_key` | The API key holds spaces or control characters. | Re-set it with `read -rs` and paste only the key. |
| `auth_failed` | A provider or Google answered 401 or 403. | Provider: the key is wrong, revoked or for another account. Google: the sheet is not shared with the service account email as Editor. |
| `plan_exhausted` | The provider says your plan or credits are used up (402, or an exhausted-plan error code). | Your plan and credits in the provider's dashboard. |
| `bad_request` | A provider answered 400 or 422, or the Sheets API answered 400. | Provider: the signal file's titles. Sheets: the first tab must be named `Sheet1`. |
| `bad_response` | The people provider returned something other than a list of people within the limit. | Only happens with your own adapter. See [adapters.md](adapters.md). |
| `sheet_id_missing` | `REPO_SIGNALS_SHEET_ID` is not set. | Set it to the long id in the sheet's address. |
| `credentials_missing` | `REPO_SIGNALS_GOOGLE_CREDENTIALS` is not set. | Set it to the key file's path. |
| `credentials_unreadable` | The key file could not be read. | The path, and that the file still exists after you moved it out of Downloads. |
| `credentials_malformed` | The key file is not a service-account JSON key, or its `client_email` or `private_key` is malformed. `doctor` names the field, and `run --live` refuses before the typed "yes". | Download a new JSON key for the service account. |
| `budget_missing` | The sheet writer was started live without a call budget. | Only happens in your own code: pass a `CallBudget`. |
| `openssl_not_found` | `openssl` is not on `PATH`. `run --live` refuses before the typed "yes" (it is listed as a failing gate), and `doctor` warns. | Install openssl, or check `PATH` in the shell you run from. `openssl version` should work. |
| `signing_failed` | openssl could not sign the token request. | The key file is complete and unedited. Download a fresh key if in doubt. <a id="signing_failed"></a> |
| `signing_timeout` | openssl took too long to sign. | Try again. Check that nothing is blocking openssl. |
| `sheet_not_found` | Google answered 404 for the sheet. | The sheet id, and that the sheet is shared with the service account email. |
| `header_mismatch` | The sheet's first row is not the tool's header row. Nothing was written. | Use a new, empty sheet, or a sheet only this tool has written to. <a id="header_mismatch"></a> |
| `header_missing` | The sheet has data but no header row. Nothing was written. | Use a new, empty sheet. <a id="header_missing"></a> |
| `method_not_allowed` | The sheet client was asked for a method it never uses. | Report it as a bug. |
| `bad_rows` | Rows for the sheet had the wrong number of columns. | Report it as a bug. |
| `bad_range` | A sheet range was outside the tool's columns. | Report it as a bug. |
| `protected_cells` | A write would have touched `email_draft` or `reviewed` below the header. It was refused. | This is a safety stop. Report it as a bug. |
| `sender_missing` | A sender variable is not set. The variable name follows the code. | Set the named variable. All five are needed. See [drafting.md](drafting.md). |
| `sender_malformed` | A sender variable has a bad value. The variable name follows the code. | Too many lines or a control character in that value. |
| `no_name` | The person has no usable first name, so no draft was written. | Nothing to fix: the row is still written without a draft. |
| `no_evidence` | There is no evidence for this company, so it gets no lookup or draft. | Nothing to fix. Check the collect summary for drops. |
| `bad_evidence` | Evidence was malformed, so it was dropped or no draft was written. | Report it as a bug if it repeats. |
| `bad_draft` | A drafter returned something that is not a draft. | Only happens with your own drafter. |
| `subject_malformed` | The draft subject was empty or held control characters. | Your template's subject line. |
| `body_malformed` | The draft body was empty or held control characters. | Your template's body. |
| `subject_too_long` | The draft subject was too long. | Shorten your template's subject. |
| `body_too_long` | The draft body was too long. | Shorten your template's body. |
| `ungrounded` | The draft did not cite the company's evidence. | Your template must use `{evidence_url}` and `{evidence_summary}`. |
| `evidence_url_missing` | The draft did not include the evidence URL. | Keep `{evidence_url}` in your template. |
| `evidence_summary_missing` | The draft did not include the evidence summary. | Keep `{evidence_summary}` in your template. |
| `postal_address_missing` | The draft did not include your postal address. | Keep `{postal_address}` in your template. |
| `optout_missing` | The draft did not include your opt-out text. | Keep `{optout_text}` in your template. |
| `url_in_subject` | The draft subject held a URL. | Remove links from your template's subject. |
| `foreign_url` | The draft held a URL that is neither evidence nor your opt-out link. | Remove other links from your template and opt-out text. |
| `relationship_claim` | The draft claimed a relationship the evidence does not show. | Reword your template. See [drafting.md](drafting.md). |
| `subject_ungrounded` | The draft subject did not mention the company or the evidence. | Keep `{company_domain}` in your template's subject. |
| `unknown_placeholder` | A template used a placeholder the tool does not know. | The placeholder list in [drafting.md](drafting.md). |
| `template_malformed` | A template file is not in the expected form. | First line `Subject: ...`, then a blank line, then the body. UTF-8, no stray braces. |
| `subject_placeholder_not_allowed` | The template subject used a placeholder that is not allowed there. | The subject placeholders in [drafting.md](drafting.md). |
| `template_missing_required_placeholder` | The template body lacks a required placeholder. | The required placeholders in [drafting.md](drafting.md). |
| `unknown_template` | No built-in template has that name. | The template name. |
| `template_unreadable` | A template file could not be read. | The path and the file's permissions. |
| `template_too_large` | A template file is too large. | Shorten it. |
| `missing_FIELD` | A value needed for placeholder FIELD was empty. | Usually a sender variable or a person with no title. |
| `newline_in_FIELD` | A one-line value for FIELD held a line break. | The sender variable behind FIELD. |
| `control_character_in_FIELD` | A value for FIELD held a control character. | The sender variable behind FIELD. |
| `suppression_unreadable` | The suppression file could not be read. | The path in `REPO_SIGNALS_SUPPRESSION_FILE` and the file's permissions. |
| `suppression_path_malformed` | `REPO_SIGNALS_SUPPRESSION_FILE` is not a well-formed path. | Set it to the file's path. |
| `suppression_too_large` | The suppression file is over 1 MB. | Trim it. |
| `suppression_malformed` | The suppression file is not plain UTF-8 text with normal line endings. | Save it as UTF-8 with Unix or Windows line endings. |
| `suppression_malformed_line_N` | Line N is neither an email address nor a domain. | That line. Comments must start with `#`. |

## Drops and outcomes

These are not failures. They are counted in the run summary and explain why a repository, a
target or a person was left out.

| code | what it means | what to check |
|---|---|---|
| `pushed_in_future` | A repository's last push date is after today's UTC date, so it was dropped. | Your computer's date and time. Dates are compared in UTC. |
| `outside_window` | The repository was last pushed before `pushed_within_days`. | Raise `pushed_within_days` in the signal file if you want older activity. |
| `language_mismatch` | The repository's main language is not the signal's `language`. | Remove `language` from the signal file to accept any language. |
| `owner_not_org` | The repository belongs to a person, not an organisation. It is never used. | Nothing to fix. |
| `fork` | The repository is a fork. | Nothing to fix. |
| `archived` | The repository is archived. | Nothing to fix. |
| `private` | The repository is private. | Nothing to fix. |
| `bad_full_name` | The repository's name did not have the expected shape. | Nothing to fix. |
| `url_malformed` | The repository's link was malformed. | Nothing to fix. |
| `url_not_https` | The repository's link was not https. | Nothing to fix. |
| `url_other_host` | The repository's link was not on GitHub's web host. | Nothing to fix. |
| `url_extra_path` | The repository's link had extra parts. | Nothing to fix. |
| `url_full_name_mismatch` | The repository's link did not match its name. | Nothing to fix. |
| `bad_fact` | A repository record was malformed. | Report it as a bug if it repeats. |
| `bad_kind` | An evidence kind was not one the tool knows. | Report it as a bug. |
| `term_not_in_signal` | A match used a term that is not in the signal file. | Report it as a bug. |
| `bad_pushed_date` | A repository's push date was malformed or in the future. | Your computer's date and time. |
| `summary_too_long` | The evidence summary would have been too long. | Nothing to fix. |
| `summary_not_printable` | The evidence summary held characters that cannot be printed. | Nothing to fix. |
| `summary_has_at` | The evidence summary held an `@`. It is dropped so no address leaks into a draft. | Nothing to fix. |
| `summary_has_url` | The evidence summary held something that looks like a link. | Nothing to fix. |
| `not_an_org` | The `org` in the signal file is a personal account, not an organisation. | The `org` value. |
| `no_website` | The org lists no website, so it cannot be matched to the domain. The target is excluded: nothing is searched or looked up for it, and the run can end with `sheet: nothing to write`. | If you are sure it is the right org, add `confirm_org = true` to the target. |
| `website_mismatch` | The org's website is on another domain. The target is excluded: nothing is searched or looked up for it, and the run can end with `sheet: nothing to write`. | The `org` value, or add `confirm_org = true` if you know the org belongs to the company. |
| `no_search_name` | No search name could be made from a domain-only target. | Add an `org` to the target. |
| `ambiguous` | More than one org matched a domain-only target. | Name the right `org` in the signal file. |
| `unmatched` | No org matched a domain-only target. | Name the `org` in the signal file. |
| `not_attempted` | The target or lookup was skipped because an earlier stop (a rate limit or the budget) came first. | The code that stopped the run. |
| `provider_error` | A people lookup failed in a way that was not a provider error code, so that domain was skipped. | Only happens with your own adapter. See [adapters.md](adapters.md). |
| `suppressed` | The person or company is on your suppression list. No draft and no row. | Your suppression file, if this is a surprise. |

## People-stage drop counts

After each people lookup that returns, the run prints one line per domain when the provider left
people out, for example `people dropped for example.com: filtered_by_title 2, missing_email 1`.
These are **counts, not errors**: the lookup worked. The line never shows a name, a title or an
email.

| code | what it means | what to try |
|---|---|---|
| `missing_name` | The provider returned a person with no name. | Nothing to fix. The person cannot be addressed, so there is no row. |
| `missing_title` | The provider returned a person with no title or position. | Nothing to fix. Without a title there is nothing to check the person against. |
| `filtered_by_title` | Hunter returned a person whose position does not contain any of your `titles`. Hunter has no title parameter, so this match runs in the tool: a case-insensitive substring of the position, so `CTO` will not match "Chief Technology Officer". It runs on only the first 3 people Hunter returns, so a title can drop everyone even when matching people exist; `doctor` and the `run --live` plan warn about this. | Use the words that appear in the positions, or add both forms, for example `titles = ["CTO", "Chief Technology Officer"]`. Remove `titles` to keep everyone. |
| `missing_email` | Hunter returned a person without a usable email address. | Nothing to fix. Hunter rows need an email; try again later or with another domain. |

Apollo does not filter titles in the tool: it filters on its side, approximately, so a search for
one title can return adjacent roles. Review the titles in the sheet before drafting.

## Exit codes

For `repo-signals run` (see [run.md](run.md)):

| exit code | meaning |
|---|---|
| 0 | complete, or a finished dry run |
| 1 | declined at the prompt: nothing was requested |
| 2 | refused before starting: nothing was requested |
| 3 | partial: a stage failed or hit its budget, and later stages worked on what existed |
| 4 | refused by a stage, for example a sheet whose headers are not the tool's |

`repo-signals collect --live` uses the same numbers (see [live-collection.md](live-collection.md)).
`repo-signals probe-org` and `repo-signals hf-probe-org` exit 0 for `ok`, 1 if the request
failed and 2 if they refused.

## Common mistakes

- **Dry run is still on.** Dry run is the default. A live command refuses until
  `REPO_SIGNALS_DRY_RUN=false` is set in the same shell. `doctor` says which.
- **The sheet is not empty.** Use a new, empty sheet. A sheet with other data is refused with
  `header_mismatch` or `header_missing`.
- **The sheet is not shared with the service account.** Share it with the `client_email` from the
  key file as an Editor, or writes fail with `sheet_not_found` or `auth_failed`.
- **Link sharing is on.** The rows hold personal data. Set General access to Restricted.
- **An empty key from a read prompt.** If you pressed Return before pasting, the variable is
  set but empty, and the provider fails with `missing_key`. Check the length, never the value:

  ```sh
  echo "key length: ${#HUNTER_API_KEY}"
  ```

  A length of 0 means you should run the `read -rs` line again.
- **The first tab was renamed.** The tool writes to `Sheet1`.
- **A domain typed as the organisation name.** `org` is the `NAME` in `github.com/NAME`, not a
  domain. A domain is refused with `signal_target_N_bad_org` in the signal file, and `probe-org`
  refuses it with `the login is not a valid organisation login shape; nothing requested`.
- **The key is still in Downloads.** Move it to `~/.config/repo-signals`, set mode 600, and check
  that no copy is left behind.
- **A second GitHub account is the active gh login.** If you fill `REPO_SIGNALS_GITHUB_TOKEN`
  from `gh auth token`, it uses whichever account is active. Check which one first, and switch if
  it is not the account that can see the org:

  ```sh
  gh auth status
  gh auth switch
  ```

  The wrong account shows up as `org_not_visible` or `not_found`.
