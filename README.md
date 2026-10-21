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
