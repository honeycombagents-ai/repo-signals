# Repo Signals

**Status: early release, dry run by default.** Hunter's domain search was checked live on
2026-10-22. Apollo's people search was checked live the same day, but what its `has_email` flag
means and its error ids are not checked live. Other API details are listed as unchecked in
[docs/hunter.md](docs/hunter.md) and [docs/apollo.md](docs/apollo.md).

What changed in each release: [CHANGELOG.md](CHANGELOG.md).

## What it is

Repo Signals looks at the public repository activity of companies you choose. It works with
GitHub.

You list the target companies in a signal file, and Repo Signals finds matching public GitHub
repositories for them. The company domains in the signal file are sent to the people provider you
choose, under your own key.

For each company with matching activity, it:

1. looks up people through **your own** contact-provider key,
2. fills a Google Sheet with the companies, the people and the evidence, and
3. drafts emails grounded in that evidence.

A human reviews every draft and sends it. Repo Signals itself never sends anything.

## Who this is for

Repo Signals is built so that someone who already uses Hunter or Apollo can deploy it quickly.

- **Hunter** is the provider that returns email addresses today.
- **Apollo** works for first names and titles: its search hides last names and returns only a
  `has_email` flag. Getting Apollo emails needs an opt-in enrichment step, and that step is not
  built.
- **Another enrichment provider?** Write a small adapter for it. See
  [docs/adapters.md](docs/adapters.md).

## What it does

Nothing makes a network call unless you turn dry run off, and every live command is gated
(`collect --live` and `run --live` also need a typed "yes").

- **collect**: turns a signal file into GitHub evidence. `collect --dry-run` validates the file and
  prints the plan with no network call. See [docs/signal-file.md](docs/signal-file.md) and
  [docs/live-collection.md](docs/live-collection.md).
- **people**: looks up people through Hunter.io or Apollo.io behind a hard call budget. Both are
  dry runs by default, and live use is reachable only through the gated `run --live`. The built-in
  `fake` provider returns synthetic people on `example.invalid`. See [docs/hunter.md](docs/hunter.md)
  and [docs/apollo.md](docs/apollo.md).
- **draft**: fills the sheet's `email_draft` column from plain-text templates, grounded on public
  evidence, with your postal address and opt-out text. A suppression list is honoured. Nothing is
  ever sent. See [docs/drafting.md](docs/drafting.md).
- **sheet**: appends people to a Google Sheet for review. Dry run by default, and it never deletes,
  clears or overwrites anything. See [docs/google-sheets.md](docs/google-sheets.md).
- **run**: chains collect, people, draft and sheet, with a separate call budget for each service.
  Dry run by default. Nothing is ever sent by email. See [docs/run.md](docs/run.md).
- **init**: creates starter config files, offline, and never asks for a secret. See
  [docs/deployment.md](docs/deployment.md).
- **doctor**: checks your settings offline and prints PASS, WARN or FAIL per line. See
  [docs/deployment.md](docs/deployment.md).

The Apollo adapter searches only: email reveal is not implemented. Apollo's people search
returns first names and titles, and no email addresses. In search-only mode Apollo rows therefore
normally have no email. Emails need a separate, credit-spending enrichment call, which is not
implemented.

## Quick start

```sh
python3 -m pip install -e .                    # Python 3.12 or newer
repo-signals init --dir ~/repo-signals-config  # starter files, offline
repo-signals doctor                            # offline checks: PASS, WARN or FAIL per line
repo-signals run                               # dry run: no network, nothing written
```

Without installing, you can run it from the repo root:

```sh
PYTHONPATH=src python3 -m repo_signals doctor
```

Before your first live run, work through the
[throwaway-first checklist](docs/deployment.md#the-throwaway-first-checklist) in
[docs/deployment.md](docs/deployment.md).

Step by step from a clean machine: [docs/setup-guide.md](docs/setup-guide.md). Error codes and drop counts: [docs/troubleshooting.md](docs/troubleshooting.md).

Settings come from environment variables only:

| variable | meaning | default |
|---|---|---|
| `REPO_SIGNALS_PROVIDER` | people provider name | `fake` |
| `REPO_SIGNALS_MAX_CALLS` | GitHub call cap, 1 to 500 | `25` |
| `REPO_SIGNALS_PROVIDER_MAX_CALLS` | people-provider call cap for `run`, 1 to 500 | `25` |
| `REPO_SIGNALS_SHEETS_MAX_CALLS` | Google Sheets call cap for `run`, 1 to 500 | `10` |
| `REPO_SIGNALS_SHEET_ID` | Google Sheet id | unset (WARN) |
| `REPO_SIGNALS_GITHUB_TOKEN` | GitHub token, optional: only raises rate limits, sent only in a header | unset (WARN) |
| `REPO_SIGNALS_GOOGLE_CREDENTIALS` | path to a Google service-account JSON key file | unset (WARN) |
| `REPO_SIGNALS_SENDER_NAME`, `_SENDER_ROLE`, `_ORG_NAME`, `_POSTAL_ADDRESS`, `_OPTOUT_TEXT` | sender details put in every draft | unset (WARN; drafting refuses) |
| `REPO_SIGNALS_SUPPRESSION_FILE` | path to a suppression list (one email or domain per line) | unset (WARN) |
| `REPO_SIGNALS_SIGNAL_FILE` | path to the collector's signal file (TOML) | unset (WARN) |
| `REPO_SIGNALS_DRY_RUN` | `true`, or exactly `false` to allow `probe-org` and `collect --live` | `true` |

`doctor` names variables but never prints their values. `doctor --online` is accepted but not
implemented yet.

To add your own provider, see [docs/adapters.md](docs/adapters.md).

## Safety defaults

- **Dry run first.** A dry run makes no paid calls and writes nothing.
- **Hard call caps.** Every provider has a per-run limit. You can raise a limit but not remove it.
- **Keys from the environment.** Keys come from environment variables, or from a key
  file whose path comes from an environment variable. No key is read from the command line, and
  keys are never logged.
- **It never sends email.** It only writes drafts for a person to review.

## Your responsibilities

- **Provider terms are yours to read and follow.** This applies to contact providers such as
  Hunter.io and Apollo.io and to every other service you connect. Repo Signals claims no
  compliance with any provider's terms.
- **Email laws apply to you.** Laws such as CAN-SPAM and the GDPR govern how you contact people
  and how you handle their data.
- **This is not legal advice.**
- **Public data can still be personal data.** GitHub data is public, but it is still personal
  data when it names people. Treat it that way.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Repo Signals and honeycombagents.ai are names of honeycombagents.ai; the license grants no
trademark rights.

---

Repo Signals is created by [honeycombagents.ai](https://honeycombagents.ai).
