# Writing results to a Google Sheet

`SheetWriter` (`src/repo_signals/sheets/writer.py`) appends people to a Google Sheet so a person
can review them. It is a **dry run** by default. It never deletes or clears anything, and it never
overwrites someone else's data.

Live writing is reachable from the command line only through the gated `repo-signals run --live`
(see [run.md](run.md)).

## Setup

1. **Create a Google Cloud project**, or pick an existing one, in the Google Cloud console.
2. **Enable the Google Sheets API** for that project (APIs & Services, Library, Google Sheets API).
3. **Create a service account** (IAM & Admin, Service accounts). It needs no project roles to
   edit a sheet that is shared with it.
4. **Create a JSON key** for the service account (Keys, Add key, JSON) and download it. Treat it
   like a password:

   ```sh
   mkdir -p ~/.config/repo-signals
   mv ~/Downloads/your-key.json ~/.config/repo-signals/service-account.json
   chmod 600 ~/.config/repo-signals/service-account.json
   ```

   Never commit this file. The repository's `.gitignore` ignores `credentials*.json`, but a key
   kept outside the repository is safer.
5. **Share the sheet with the service account.** Open the sheet, choose Share, and add the
   service account's email address (the `client_email` value in the key file) as an **Editor**.
   If you skip this step, writes fail with `sheet_not_found` or `auth_failed`.
6. **Set the environment variables:**

   ```sh
   export REPO_SIGNALS_SHEET_ID=...            # the long id in the sheet's address
   export REPO_SIGNALS_GOOGLE_CREDENTIALS=~/.config/repo-signals/service-account.json
   repo-signals doctor
   ```

   `REPO_SIGNALS_GOOGLE_CREDENTIALS` is a **file path**. Putting the JSON itself in the variable
   is refused.

`doctor` stays offline. It reports, by name only:

- whether `REPO_SIGNALS_GOOGLE_CREDENTIALS` is set;
- if it is set, whether the file passes the same check the run uses to load it: it exists, parses
  as JSON with the expected keys (`type`, `client_email`, `private_key`), is a service-account
  key, and has a well-formed `client_email` and a `private_key` shaped like a key. A problem is
  a FAIL naming the field and the code (`credentials_unreadable` or `credentials_malformed`),
  never a value. It also says whether other users can read the file;
- whether `REPO_SIGNALS_SHEET_ID` is set;
- whether `openssl` is on `PATH`.

It never prints a value from the environment or the file.

## Dry run first

Dry run is the default. A dry run makes **no network call** and writes nothing. It prints what
it would write: the row count and the first 3 rows, with each email masked (for example
`a***@example.invalid`).

```
sheets: dry run, would write 2 row(s) (0 duplicate(s) in this batch skipped; the sheet itself is not read in a dry run); nothing written
name | email | title | company_domain | source | confidence | signal_evidence
Avery Alder | a***@example.invalid | CTO | acme.example.invalid | fake | 0.8 | pushed 12 commits to example/repo
Blake Birch | b***@example.invalid | VP Engineering | acme.example.invalid | fake | 0.7 | opened 3 issues
```

A live write needs **all** of these. Without them it stops with a code-only error:

- dry run off,
- `REPO_SIGNALS_SHEET_ID` (otherwise `sheet_id_missing`),
- `REPO_SIGNALS_GOOGLE_CREDENTIALS` (otherwise `credentials_missing`), and
- a `CallBudget` (otherwise `budget_missing`).

```python
from repo_signals.budget import CallBudget
from repo_signals.config import Settings
from repo_signals.sheets.writer import SheetWriter

settings = Settings(dry_run=False, sheet_id="...", google_credentials="/path/to/key.json")
writer = SheetWriter(settings, budget=CallBudget(10))
writer.write([(person, "pushed 12 commits to example/repo")])
```

## The columns the tool owns

The tool owns these columns, in this order, and no others:

| column | written by the tool |
|---|---|
| `name`, `email`, `title`, `company_domain`, `source`, `confidence` | from the people provider |
| `signal_evidence` | why this person was found |
| `email_draft` | empty, or an initial draft written by `repo-signals run`, **only when the row is new**. For a person to edit. |
| `reviewed` | **empty**. For a person to fill in. |
| `run_id`, `written_at` | which run wrote the row, and when (UTC) |

You can add your own columns to the right of `written_at`. The tool never writes them.

## What it never does

- **It never deletes or clears anything.** The client has no delete, clear or structural call.
  The HTTP client has no DELETE or PATCH method.
- **It never overwrites someone's data.** On each write it reads the header row first:
  - An empty sheet gets the headers.
  - A sheet whose first 11 headers differ from the table above is refused with
    `header_mismatch`, and nothing is written.
  - A sheet with data but no header row is refused with `header_missing`.
- **It never duplicates a person.** A person already in the sheet is skipped and the existing
  row is left alone. A match means the same email, ignoring case. When the new person has no
  email, a match means the same name, title and company_domain, ignoring case and surrounding
  spaces. Apollo rows have a first name and no email, so the title keeps two people with the
  same first name at one company apart. The other side of this rule: if a person's title
  changes between runs, the new title produces a second row. The first row is never changed.
- **It never touches `email_draft` or `reviewed` after appending.** An initial draft can only be
  part of a new row. Rows are only ever appended,
  and the client refuses any update that would touch those columns below the header row.
- **It does not let a cell become a formula.** A value from external data that starts with `=`,
  `+`, `-` or `@` (or with a tab or carriage return) gets a leading `'`, unless it is a plain
  number. Values are sent with the RAW input option, so the sheet stores them as text and
  evaluates no formula. A quoted value shows its leading `'`.

## The call budget

Every request is charged to the `CallBudget` **before** it is sent. That includes the token
request. A call that would pass the cap never reaches the network and raises
`budget_exhausted`. A request body is measured before anything is charged: a body over the
64 KB request limit is refused with `payload_too_large`, with no call charged and nothing sent.

A live write costs:

1. a token,
2. reading the header row,
3. reading the existing rows (to find duplicates),
4. writing the header row, on an empty sheet only, and
5. one append for each 60 KB of new rows.

So a write of up to about 45 typical rows (about 1.3 KB each) costs 4 or 5 calls, and each
further 60 KB of rows adds one append. `REPO_SIGNALS_SHEETS_MAX_CALLS` (default 10) stays the
hard limit: if it runs out part-way, the appends already sent stay written.

## Appends of at most 60 KB

The HTTP client refuses any request body over 64 KB (`MAX_REQUEST_BYTES` in
`src/repo_signals/http.py`). New rows are therefore sent in order, split into appends whose
encoded body is at most 60 KB, a margin below that limit. Each append is a separate call,
charged to the budget before it is sent. The header check and the duplicate check run once,
before the first append, and the header is written at most once.

If an append fails (for example `budget_exhausted` or `server_error`), the appends before it
stay written and nothing after it is sent. The run prints the code and how many new rows were
written and how many were not. Running again is safe: the rows already written are found as
duplicates and skipped, and only the rest are appended.

A single row larger than 60 KB that still fits within 64 KB goes in an append by itself. A
single row whose own append body would pass 64 KB (an unusually long name, title or draft) stops
the write with `row_too_large` before any call is charged for it. Rows in earlier appends
stay written; that row and the rows after it are not written.

## The read limit

Finding duplicates means reading every existing row in one request, and every response is
capped at 1 MB. A typical row is about 1.3 KB, so a sheet of roughly 750 to 800 typical rows
(fewer if the rows are long) makes that read fail with `response_too_large`. From then on every
write to that sheet fails at the same step, and nothing is appended. This is a known limit: the
tool never deletes or moves rows, so the sheet then needs trimming by hand (move older rows to
another sheet or tab), or point `REPO_SIGNALS_SHEET_ID` at a new sheet.

## Auth and signing

The key file is read when a token is needed and is not kept in memory. A JWT for the
spreadsheets scope is signed with RS256 and exchanged for an access token. The token lives in
memory only and is never written or logged.

The standard library cannot sign RS256, so signing goes through a `Signer`: any object with
`sign(message: bytes, private_key_pem: str) -> bytes` that returns an RSASSA-PKCS1-v1_5 SHA-256
signature.

The built-in `OpenSSLSigner` runs `openssl dgst -sha256 -sign <temp file>` with the message on
stdin. The private key goes into a temp file created with mode 0600, and the file is deleted in
a `finally` block, even when signing fails. The key is never on the command line, in an
environment variable or in a log.

Note that the private key does touch the disk: for each signature it is written to a new temp
file with mode 0600 in your temp folder (`TMPDIR`), and the file is deleted as soon as openssl
returns. A crash, a forced kill or a power cut during signing can leave that file behind, so a
key file named `repo-signals-*.pem` in your temp folder should be deleted. A Signer that does
not use openssl avoids this.

**You can supply your own Signer**, for example one backed by a cloud KMS or a hardware key:

```python
SheetWriter(settings, budget=CallBudget(10), signer=MySigner())
```

## Errors

Every failure is a `ProviderError` whose message is a short code. It never includes a URL, a
token, a key, a header, a response body, the sheet id or the credentials path. The writer never
sleeps and never retries.

| code | when |
|---|---|
| `auth_failed` | 401 or 403 |
| `sheet_not_found` | 404: a wrong sheet id, or the sheet is not shared with the service account |
| `retry_after_N` | 429 with a `Retry-After` of N seconds, N from 0 to 60. Waiting is up to you. |
| `rate_limited` | 429 with no usable `Retry-After` |
| `server_error` | any 5xx |
| `bad_request`, `token_rejected` | 400 from the Sheets API, or 400 from the token endpoint |
| `timeout`, `malformed_json`, `unexpected_shape` | the request timed out, or the response was not what was expected |
| `header_mismatch`, `header_missing` | the sheet's headers are not the tool's (see above) |
| `credentials_missing`, `credentials_unreadable`, `credentials_malformed` | a problem with the key file |
| `openssl_not_found`, `signing_failed`, `signing_timeout` | from the built-in signer |
| `budget_exhausted`, `budget_missing`, `sheet_id_missing` | see above |
| `payload_too_large` | a request body over 64 KB, refused before it was charged or sent |
| `row_too_large` | one new row too large for any append; see [Appends of at most 60 KB](#appends-of-at-most-60-kb) |
| `response_too_large` | a response over 1 MB; when reading existing rows, see [The read limit](#the-read-limit) |

## Your responsibilities

- Google's terms, and the terms of any provider whose data you write, are yours to read and
  follow.
- The data you write is personal data. Share the sheet only with people who need it.
- This is not legal advice.

## Checked and unchecked API details

The endpoints, JWT claim names and response field names first came from general knowledge. Some
of them were checked live on 2026-10-22: the token exchange, signing with openssl, the header
check, the append and the duplicate skip. The rest are still unchecked. They all sit in one
constants block in `src/repo_signals/sheets/google_api.py`, which says which are checked and,
for every other one, "verify against Google's current documentation before first live use". The
tests use a fake Google built to the same assumptions, so passing tests do not show that the
assumptions are right.

Checked live on 2026-10-22:

- [x] `TOKEN_URL` and `TOKEN_HOST`: the OAuth token endpoint, with a form-encoded POST
- [x] `SCOPE`: the spreadsheets scope string
- [x] `JWT_HEADER` and the claims `CLAIM_ISSUER`, `CLAIM_SCOPE`, `CLAIM_AUDIENCE`,
      `CLAIM_ISSUED_AT`, `CLAIM_EXPIRES_AT`, with `JWT_LIFETIME_SECONDS` at one hour
- [x] `FORM_GRANT_TYPE`, `GRANT_TYPE_JWT_BEARER`, `FORM_ASSERTION`
- [x] `RESP_ACCESS_TOKEN`: the access token field
- [x] the key file fields `CRED_TYPE`, `CRED_TYPE_SERVICE_ACCOUNT`, `CRED_CLIENT_EMAIL`,
      `CRED_PRIVATE_KEY`
- [x] that `openssl dgst -sha256 -sign` gives an RS256 signature for a service-account key
- [x] `SHEETS_HOST` and `VALUES_URL` with a GET: reading the header row and the existing rows,
      with the tab quoted in the A1 range (the default tab is `Sheet1`)
- [x] `RESP_VALUES` for a range that has rows
- [x] `APPEND_SUFFIX` with a POST, `PARAM_VALUE_INPUT_OPTION` = `RAW`,
      `PARAM_INSERT_DATA_OPTION` = `INSERT_ROWS` and `BODY_VALUES`: the append inserts rows
- [x] the duplicate skip: a second run appended no copy of a person already in the sheet

Still unchecked, so check each of these before relying on it:

- [ ] `RESP_EXPIRES_IN` and `RESP_TOKEN_TYPE`: the other token response fields
- [ ] the header write on an empty sheet: `VALUES_URL` with a PUT, `BODY_RANGE` and
      `BODY_MAJOR_DIMENSION`
- [ ] `RESP_VALUES` being absent for an empty range
- [ ] the status codes for an unshared sheet (assumed 404 or 403) and for rate limiting
