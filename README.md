# Repo Signals

**Status: scaffold, not usable yet.** This repository has no product code yet: only the license,
the security policy and a scanner that keeps secrets and stray identifiers out of the tree.

## What it is

Repo Signals finds companies by their public repository activity. It works with GitHub.

For each company it finds, it:

1. looks up people through **your own** contact-provider key,
2. fills a Google Sheet with the companies, the people and the evidence, and
3. drafts emails grounded in that evidence.

A human reviews every draft and sends it. Repo Signals itself never sends anything.

## Status and quick start

**Early. Dry run only.** There is no live mode yet and nothing makes a network call. What exists
today:

- the people-provider interface,
- a built-in `fake` provider that returns synthetic people on `example.invalid`,
- a hard call budget,
- settings read from environment variables, and
- an offline `doctor` command.

Hunter.io and Apollo.io adapters exist behind the call budget. Both run as dry runs, and live
use is not reachable from the command line yet (see [docs/hunter.md](docs/hunter.md) and
[docs/apollo.md](docs/apollo.md)). The Apollo adapter searches only: email reveal is not
implemented.

A Google Sheet writer appends people for review. It runs as a dry run by default, and it never
deletes, clears or overwrites anything (see [docs/google-sheets.md](docs/google-sheets.md)).

```sh
python3 -m pip install -e .       # Python 3.12 or newer
repo-signals doctor               # offline checks: PASS, WARN or FAIL per line
```

Without installing, you can run it from the repo root:

```sh
PYTHONPATH=src python3 -m repo_signals doctor
```

Settings come from environment variables only:

| variable | meaning | default |
|---|---|---|
| `REPO_SIGNALS_PROVIDER` | people provider name | `fake` |
| `REPO_SIGNALS_MAX_CALLS` | provider call cap, 1 to 500 | `25` |
| `REPO_SIGNALS_SHEET_ID` | Google Sheet id | unset (WARN) |
| `REPO_SIGNALS_GITHUB_TOKEN` | GitHub token | unset (WARN) |
| `REPO_SIGNALS_GOOGLE_CREDENTIALS` | path to a Google service-account JSON key file | unset (WARN) |

`doctor` names variables but never prints their values. `doctor --online` is accepted but not
implemented yet.

To add your own provider, see [docs/adapters.md](docs/adapters.md).

## Safety defaults

- **Dry run first.** A dry run makes no paid calls and writes nothing.
- **Hard call caps.** Every provider has a per-run limit. You can raise a limit but not remove it.
- **Keys only from environment variables.** No key is read from a config file or the command
  line, and keys are never logged.
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
