# Running Repo Signals

Repo Signals runs on your own machine, in your own terminal. This page covers setting it up,
handling secrets, the first live run, costs, and what may run unattended.

## Running locally

You need Python 3.12 or newer. Repo Signals uses only the standard library. Clone the
repository, move into it, and install into a virtual environment (the `.venv` folder is ignored
by the repository's `.gitignore`):

```sh
git clone https://github.com/honeycombagents-ai/repo-signals.git
cd repo-signals
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
repo-signals init --dir ~/repo-signals-config
```

`init` works offline. It prints which variables are set or missing (by name only), shows the files
it would create, and writes them only after you type `yes` at a terminal. It creates:

- `signal.toml`, a commented example signal file that uses `example.com`;
- `env.example`, the name of every variable, with no values;
- `sender.example`, the names of the five sender variables;
- `suppression.txt`, an empty suppression list;
- `.gitignore`, holding `*`, so the folder can never be committed.

The folder gets mode 0700 and each file mode 0600. `init` never overwrites anything: if any of
those files already exists, it writes nothing. It refuses to write inside a git work tree, so
keep the folder outside your repositories. It never asks for a token, a key or a password.

Then check everything and try the dry run:

```sh
repo-signals doctor
repo-signals run
```

## Secrets

Secrets live in your shell and nowhere else.

- Set a secret without echoing it or saving it in your shell history:

  ```sh
  read -rs REPO_SIGNALS_GITHUB_TOKEN; export REPO_SIGNALS_GITHUB_TOKEN
  ```

- Never write a secret into a file inside a repository, including `env.example`.
- Keep key files, such as the Google service-account key, outside every repository, with mode
  600:

  ```sh
  chmod 600 ~/.config/repo-signals/service-account.json
  ```

- `doctor` and every command print variable names, never their values.

## The throwaway-first checklist

Before the first live run:

- [ ] `repo-signals doctor` passes, apart from the warnings you expect
- [ ] `repo-signals run` (the dry run) shows the sample row and draft you expect
- [ ] a **throwaway sheet** with nothing else in it, shared with the service account
- [ ] a signal file with one target and one term
- [ ] tiny budgets: `REPO_SIGNALS_MAX_CALLS=3`, `REPO_SIGNALS_PROVIDER_MAX_CALLS=1`,
      `REPO_SIGNALS_SHEETS_MAX_CALLS=5`
- [ ] `repo-signals probe-org <org>` gives the outcome you expect
- [ ] `REPO_SIGNALS_DRY_RUN=false` only for the run itself, then unset it

Read every row and draft in the throwaway sheet before you point Repo Signals at a real one.

## Budgets and costs

Each service has its own call budget, and no budget can go above 500 calls:

| variable | caps | default |
|---|---|---|
| `REPO_SIGNALS_MAX_CALLS` | GitHub requests | 25 |
| `REPO_SIGNALS_PROVIDER_MAX_CALLS` | people-provider requests | 25 |
| `REPO_SIGNALS_SHEETS_MAX_CALLS` | Google Sheets requests | 10 |

GitHub and Google Sheets calls are free within their rate limits. People-provider calls may cost
money or credits on your plan, so check your provider's pricing and set
`REPO_SIGNALS_PROVIDER_MAX_CALLS` to what you are willing to spend. Every run prints the requests
it actually made against each budget.

## Why run --live needs a terminal and a typed yes

`repo-signals run --live` and `repo-signals collect --live` refuse to start unless stdin and stdout
are a terminal. They then show the plan and wait for you to type exactly `yes`. There is no flag
to skip this.

That is deliberate. It means no cron job, CI job or script can start a live run. A person reads
the plan every time, and a person reviews every row and draft afterwards.

## What CI may run

CI and other automation may run the offline commands:

```sh
python3 -m unittest discover -s tests
python3 scripts/scan.py --history
repo-signals doctor
repo-signals run            # the dry run: no network, nothing written
```

None of these makes a network call, and none needs a secret.

## No headless live mode in v1

A live mode that runs without a terminal and a typed confirmation is a deliberate non-goal for
v1. Adding one would need an explicit design decision covering who approves each run, how budgets
and suppression are enforced without a person watching, and how the results are reviewed.

## Your responsibilities

Email law, the terms of every platform you use (GitHub, your people provider and Google), and
data-protection law (for example the GDPR) apply to you, not to this tool. Repo Signals never
sends email, and a person reviews every draft. Whether your outreach is lawful is your
responsibility. This is not legal advice.
