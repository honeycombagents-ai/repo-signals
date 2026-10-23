# Contributing

Thank you for helping. Repo Signals is small and strict on purpose, so a few rules keep it that
way.

## Run the checks

From the repository root, with Python 3.12 or newer and nothing else installed:

```sh
python3 -m unittest discover -s tests
python3 scripts/scan.py --history
```

The first runs the test suite. The second scans every tracked file and the whole git history
(all refs, commit messages, identities and tags) for secret-shaped strings and stray identifiers.
Both must pass before you open a pull request.

CI runs the same scan with an extra deny-list kept in a private repository secret. Pull requests
from forks do not get that secret, so a maintainer may run the full scan before merging.

## What a change looks like

- Keep each commit small and give it tests. A change to behaviour comes with tests that fail
  without it.
- Standard library only. Do not add dependencies.
- Plain prose in docs and messages, and no secrets, keys, tokens or real email addresses
  anywhere, including commit messages.

## What must never change

- **Nothing may send email.** Repo Signals writes drafts for a person to review and send.
- **Nothing may delete, clear or overwrite sheet data.** Rows are only appended.
- **No live calls in tests.** Tests use fake transports and synthetic data only (addresses on
  `example.invalid`). A test must never reach GitHub, a people provider or Google.
- Dry run stays the default, and live commands keep their gates and the typed "yes".

## Merging

Maintainers merge pull requests fast-forward, so keep your branch rebased on `main`.

## Security

Please report a security problem privately, as described in [SECURITY.md](SECURITY.md), not in a
public issue.
