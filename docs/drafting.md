# Drafting emails

Repo Signals can draft a short email for each person it finds. The drafts go into the sheet's
`email_draft` column for a person to read, edit and decide whether to send.

**Nothing is ever sent.** There is no email-sending code in Repo Signals. The drafting package
makes no network call: the default drafter fills in a plain-text template.

## The human review step

A draft is only a starting point. Before anything goes out, a person:

1. reads the draft next to its evidence link,
2. checks that the evidence is accurate and still public,
3. edits or deletes the draft,
4. checks the recipient against your own suppression and opt-out records, and
5. sends it from their own mail client, if they decide to.

The tool never writes `reviewed`. That column is yours.

## How drafts are grounded

Every draft rests on **evidence**: a public repository signal about the recipient's company.

```python
from repo_signals.models import Evidence

Evidence(
    company_domain="acme.example.com",
    summary="The org publishes a repo named widget that uses library gadget.",
    url="https://code.example.com/acme/widget",
    kind="repo",
)
```

`summary` is one short factual sentence. `url` is an https link to the public evidence.

These rules are enforced in code, and the tests cover each one:

- **No evidence, no draft.** A person gets a draft only when there is at least one Evidence
  item for their company domain. Evidence about other companies is ignored.
- **Facts come only from the evidence.** The template inserts the summary verbatim. The guard
  checks that the body quotes the summary of every evidence item the draft is grounded on.
- **The evidence link is always in the body.** `Draft.grounded_on` lists the evidence URLs used,
  and the body must contain each of them.
- **No name, no draft.** A person whose name has no letters gets no draft.
- **No invented relationship.** A draft may not claim a meeting, a conversation or any prior
  contact ("as we discussed", "great to meet you", "following up on our call" and so on),
  unless the evidence itself says so.
- **The subject comes from the company domain or the evidence kind,** never from an invented
  claim.

## The two templates

They live in `src/repo_signals/drafting/templates/`:

- `intro.txt`: a short first note.
- `follow_up.txt`: a shorter, optional second note. It deliberately does not say "as I
  mentioned" or refer to an earlier email, because the tool cannot know what was actually sent.

Both are neutral. They name no company, person or product.

Rendered with synthetic inputs, `intro.txt` gives:

```
Subject: A public signal from acme.example.com

Hi Blake,

I came across this public signal about acme.example.com:
The org publishes a repo named widget that uses library gadget.
Source: https://code.example.com/acme/widget

I am Avery Alder, Engineer at Example Org. I am writing because it may be relevant to your work as CTO. If it is not, no reply is needed.

Avery Alder
Example Org
1 Example Street
Example City

Reply "stop" and we will not write to you again.
```

## Adding your own template

A template is a UTF-8 text file:

```
Subject: <subject line>
<blank line>
<body>
```

Only these placeholders exist: `{first_name}`, `{title}`, `{company_domain}`,
`{evidence_summary}`, `{evidence_url}`, `{sender_name}`, `{sender_role}`, `{org_name}`,
`{postal_address}` and `{optout_text}`.

- The subject may use only `{company_domain}`.
- The body must use `{evidence_summary}`, `{evidence_url}`, `{postal_address}` and
  `{optout_text}`.
- Any other `{` or `}` is an error. There is no escape for literal braces.

Rendering is strict. An unknown placeholder, a missing value, or a newline in a single-line field
stops with a code-only error. Values go in once, as plain text. A value that looks like a
placeholder is not expanded.

```python
from repo_signals.drafting.template import TemplateDrafter

drafter = TemplateDrafter(path="my_template.txt")
```

Every draft from your template still passes the guard described above.

## Sender details: the compliance guard

Every draft must include your postal address and opt-out text. They come from the environment:

| variable | form |
|---|---|
| `REPO_SIGNALS_SENDER_NAME` | one line |
| `REPO_SIGNALS_SENDER_ROLE` | one line |
| `REPO_SIGNALS_ORG_NAME` | one line |
| `REPO_SIGNALS_POSTAL_ADDRESS` | up to 6 lines |
| `REPO_SIGNALS_OPTOUT_TEXT` | up to 6 lines |

If any of them is missing, drafting refuses. The error names the variable, for example
`sender_missing: REPO_SIGNALS_POSTAL_ADDRESS`, and never shows a value. `doctor` warns about each
missing one.

> **Not legal advice.** A postal address and an opt-out line meet some common requirements for
> commercial email, but only some of them. Whether your emails comply with the law is your
> responsibility.

The opt-out text may contain a link, for example `https://example.com/optout`. It is the only
URL allowed in a draft besides the evidence URLs.

## The suppression list

Set `REPO_SIGNALS_SUPPRESSION_FILE` to the path of a text file with one email address or one
domain per line:

```
# people and companies who must never get a draft
<an email address, one per line>
example.invalid
```

- Blank lines and lines starting with `#` are ignored.
- Any other line that is not an email address or a domain is an error. The error names the line
  number, never the text.
- The file is capped at 1 MB.
- A person whose email is listed, or whose email domain or company domain is a listed domain (or
  a subdomain of one), gets **no draft**. Their result is `suppressed`.

`doctor` reports whether the file is set and, if it is, how many emails and domains it parses.
It never prints an entry.

## Filling the sheet column

```python
from repo_signals.drafting.sheet import fill_drafts
from repo_signals.drafting.template import TemplateDrafter

rows = fill_drafts(rows, TemplateDrafter(), {"acme.example.com": (evidence,)})
```

`rows` are lists of cells in the sheet writer's column layout. `evidence_lookup` is a mapping or
a function from company domain to Evidence items.

- A row gets a draft only when its `email_draft` cell is empty: not even a space.
- A **non-empty `email_draft` is never overwritten**, and **`reviewed` is never written**.
- The input rows are not changed. New rows are returned.
- Every drafter is wrapped in the guard, including your own.

**Dry run is the default.** It prints how many drafts would be filled and one sample draft, with
the recipient's email masked (`b***@acme.example.com`). It returns the rows unchanged and writes
nothing.

The sheet writer appends new rows with `email_draft` empty and never updates an existing row.
Wiring filled drafts into the sheet is a later step, and it will keep a person in the loop.

## Optional: drafting with your own model

`src/repo_signals/drafting/llm.py` defines `LlmDrafter`, an **interface only**. Repo Signals ships
no model client, no endpoint and no key handling. To use a model, write a class with `name` and
`draft(person, evidence, sender) -> Draft` that makes the call itself, with your own key:

```python
class MyModelDrafter:
    name = "my-model"

    def draft(self, person, evidence, sender):
        ...  # your own model call
        return Draft(subject=..., body=..., grounded_on=(evidence[0].url,))
```

`GuardedDrafter`, which `fill_drafts` applies to every drafter, checks a model's output with the
same rules as a template's and rejects any draft that:

- does not contain the evidence URL or does not quote its summary;
- is missing the postal address or the opt-out text;
- contains any other URL;
- claims a relationship that is not in the evidence;
- has a subject that does not name the company domain or an evidence kind; or
- is longer than 2,000 characters (or has a subject longer than 120).

What you send to a model provider is personal data. That provider's terms are yours to follow.

## Email law is your responsibility

Email law applies to you, not to this tool. CAN-SPAM in the US, the GDPR and ePrivacy rules in
the EU and UK, CASL in Canada, and others set rules on consent, identification, opt-out and how
you handle personal data. Repo Signals helps you draft. It does not make your outreach lawful.
This is not legal advice.
