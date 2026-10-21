# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Contact: TBD: monitored alias on honeycombagents.ai

Please include what you found, how to reproduce it, and which version or commit is affected.

## Secrets

Never commit keys, tokens or credentials. Repo Signals reads keys only from environment
variables. `scripts/scan.py` runs in CI and fails the build if it finds a secret-shaped string.
