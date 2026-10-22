# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Contact: use the Report a vulnerability button on the Security tab of this repository (private vulnerability reporting). This is an early project with no guaranteed response time.

Please include what you found, how to reproduce it, and which version or commit is affected.

## Secrets

Never commit keys, tokens or credentials. Repo Signals reads keys only from environment
variables. `scripts/scan.py` runs in CI and fails the build if it finds a secret-shaped string.
