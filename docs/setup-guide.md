# Setup guide: from a clean machine to a first live run

This guide takes you from nothing installed to one small live run that writes a few rows to a
throwaway Google Sheet. Follow the steps in order. Every shell block can be pasted into zsh as it
is. Where a block needs something only you know, it asks for it with `read`, so nothing in a block
needs editing.

What Repo Signals never does:

- **It never sends email.** It writes drafts into the sheet. A person reads each one and sends it,
  or not, from their own mail client.
- **It never deletes sheet data.** It never deletes, clears or overwrites anything in the sheet.
  It only appends rows, and it skips a person who is already there. A sheet that is not empty,
  or that holds someone else's columns, is refused by design.

What each provider returns today:

- **Hunter** returns email addresses, with a first name, a last name and a position.
- **Apollo** returns first names and titles, with no email. Its title search is approximate: a
  search for one title can return adjacent roles. Review the titles in the sheet before you
  draft anything.

For the reasoning behind each step, see [deployment.md](deployment.md) and [run.md](run.md). If
something fails, look up the code in [troubleshooting.md](troubleshooting.md).

## 1. What you need

- Python 3.12 or newer
- git
- openssl (used to sign the Google token)
- a Google account
- a Hunter or Apollo API key from your own account (see [hunter.md](hunter.md) and
  [apollo.md](apollo.md))
- optional: a GitHub token. It only raises GitHub's rate limits.

Check the tools:

```sh
python3 --version
git --version
openssl version
```

## 2. Install

Clone the repository and move into its folder, then create a virtual environment and install
into it. The environment lives in `.venv` inside the repository folder, which this repository's
`.gitignore` already ignores, so it is never committed:

```sh
git clone https://github.com/honeycombagents-ai/repo-signals.git
cd repo-signals
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
repo-signals --help
```

Run every step from here on in the same terminal window. Later blocks use variables set earlier,
such as `CONFIG_DIR` and the answers you type at the prompts, and a new window would not have
them.

## 3. Create the config folder with init

`init` works offline and never asks for a secret. Point it at a folder **outside any git work
tree**. It refuses to write inside one, so the folder can never be committed by accident.

```sh
CONFIG_DIR="$HOME/repo-signals-config"
repo-signals init --dir "$CONFIG_DIR"
ls -la "$CONFIG_DIR"
```

Type `yes` when it asks. It creates `signal.toml`, `env.example`, `sender.example`,
`suppression.txt` and a `.gitignore`, with the folder at mode 0700 and each file at mode 0600.

## 4. A throwaway Google Cloud project and service account

Do this in the Google Cloud console, in a browser.

1. Create a **new, throwaway project** just for this. Do not reuse a project that holds anything
   else.
2. Enable the **Google Sheets API** for it (APIs & Services, Library, Google Sheets API).
3. Create a **service account** (IAM & Admin, Service accounts). Give it **no project roles**:
   skip the optional "grant this service account access" step. It needs no roles to edit a
   sheet that is shared with it.
4. Open the service account, then Keys, Add key, Create new key, JSON. The key file downloads.
   Treat it like a password.

Move the key out of Downloads and lock it down. At the prompt, type the key's full path or drag
the downloaded file into the terminal window, then press Return. A path that starts with `~` is
fine: the line after the prompt expands it to your home folder. If the path holds spaces, type it
without backslashes.

```sh
mkdir -p ~/.config/repo-signals
chmod 700 ~/.config/repo-signals
read -r "KEY_DOWNLOAD?Path of the downloaded key: "
KEY_DOWNLOAD="${KEY_DOWNLOAD/#\~/$HOME}"
mv "$KEY_DOWNLOAD" ~/.config/repo-signals/service-account.json
chmod 600 ~/.config/repo-signals/service-account.json
ls -l ~/.config/repo-signals/service-account.json
```

Check that nothing is left behind in Downloads. Never put the key inside a repository.

## 5. An empty sheet, shared with the service account

1. Create a **new, empty** Google Sheet. Do not use a sheet that has anything in it: a sheet
   with other data is refused, by design, so nothing of yours can be overwritten.
2. Leave the first tab named **Sheet1**. The tool writes to the tab named `Sheet1`, so do not
   rename it.
3. Print the service account's email address:

   ```sh
   python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["client_email"])' ~/.config/repo-signals/service-account.json
   ```

4. In the sheet, choose Share and add that address as an **Editor**.
5. Keep **link sharing off**: under General access, choose Restricted. The rows hold personal
   data, so only people who need them should see the sheet.

The sheet id is the long string in the sheet's address, between `/d/` and `/edit`. You will paste
it in the next step.

## 6. Set the variables

Secrets go into your shell only, with `read -rs`, so they are not echoed or saved in your shell
history. Each `read` shows a prompt and waits for you to paste a value and press Return. After
each secret, the block prints its **length**, never its value. A length of 0 means the prompt got
no input: run the block again.

The Google settings. Paste the sheet id at the prompt:

```sh
export REPO_SIGNALS_GOOGLE_CREDENTIALS="$HOME/.config/repo-signals/service-account.json"
read -r "REPO_SIGNALS_SHEET_ID?Sheet id: "
export REPO_SIGNALS_SHEET_ID
```

The people provider. For Hunter:

```sh
export REPO_SIGNALS_PROVIDER=hunter
read -rs "HUNTER_API_KEY?Hunter API key: "
echo
export HUNTER_API_KEY
echo "key length: ${#HUNTER_API_KEY}"
```

Or for Apollo:

```sh
export REPO_SIGNALS_PROVIDER=apollo
read -rs "APOLLO_API_KEY?Apollo API key: "
echo
export APOLLO_API_KEY
echo "key length: ${#APOLLO_API_KEY}"
```

Optionally, a GitHub token:

```sh
read -rs "REPO_SIGNALS_GITHUB_TOKEN?GitHub token: "
echo
export REPO_SIGNALS_GITHUB_TOKEN
echo "token length: ${#REPO_SIGNALS_GITHUB_TOKEN}"
```

Tiny budgets for the first run:

```sh
export REPO_SIGNALS_MAX_CALLS=3
export REPO_SIGNALS_PROVIDER_MAX_CALLS=1
export REPO_SIGNALS_SHEETS_MAX_CALLS=6
```

`REPO_SIGNALS_SHEETS_MAX_CALLS=6` is exactly enough for a first run on an empty sheet: a token
and a header read before anything is collected, then the header read again, the existing rows,
the header write and one append. The second run in step 13 needs fewer (4: no header write and,
with nothing new, no append), but `run --live` still requires at least 6. Use 8 if you want
headroom, for example for a second append.

## 7. A signal file and a suppression file

For the first run, use one target with a named GitHub organisation, one term and one job title.
The block asks for the company's domain, its GitHub organisation name, the term to search for and
the title to look up, then writes a small signal file next to the example one:

```sh
read -r "TARGET_DOMAIN?Company domain: "
read -r "TARGET_ORG?GitHub organisation name (the NAME in github.com/NAME, not a domain): "
read -r "SEARCH_TERM?Search term: "
read -r "JOB_TITLE?Job title: "
cat > "$CONFIG_DIR/first-run.toml" <<EOF
name = "first-run"
terms = ["$SEARCH_TERM"]
titles = ["$JOB_TITLE"]

[[targets]]
domain = "$TARGET_DOMAIN"
org = "$TARGET_ORG"
EOF
chmod 600 "$CONFIG_DIR/first-run.toml"
export REPO_SIGNALS_SIGNAL_FILE="$CONFIG_DIR/first-run.toml"
```

The values you type must not contain double quotes or backslashes, or the file will not parse;
the tool then refuses it with a code and nothing is requested. The organisation name is the
`NAME` in `github.com/NAME`, for example `example-org`. A domain such as `example.com` is refused
there with `signal_target_1_bad_org` (`repo-signals doctor` shows it), and nothing is requested.

When you run it, the tool looks the organisation up and checks the website listed on its GitHub
profile. If that website is on another domain, or none is listed, the target is excluded: the run
prints `example.com -> example-org (user, website_mismatch: excluded; set confirm_org = true to
include)` (or `no website listed`) and looks nothing up for that company. If you know the
organisation belongs to the company, you can add `confirm_org = true` to the target. It goes in
the `[[targets]]` table, under `org`, and it makes the tool include the target without the website
check. Set it only for an organisation you know belongs to the company. It is optional and not in
the block above; the target is the last table in the file, so this adds it there:

```sh
printf '%s\n' "confirm_org = true" >> "$CONFIG_DIR/first-run.toml"
```

The rules for every field are in [signal-file.md](signal-file.md). Pick the title with your
provider in mind. Hunter matches it as a case-insensitive substring of each position, so `CTO`
will not match "Chief Technology Officer": use the words that appear in the positions. With
Hunter, titles are also filtered among only the first 3 people Hunter returns for each company,
so a title may return nobody even when matching people exist; leave `titles` out to keep
everyone. Apollo filters titles on its own side, approximately, so expect some adjacent roles.

The suppression file lists people and companies that must never get a draft: one email address
or one domain per line. A good first entry is your own company's domain. The block asks for it:

```sh
read -r "OWN_DOMAIN?Your own company's domain: "
printf '%s\n' "$OWN_DOMAIN" >> "$CONFIG_DIR/suppression.txt"
export REPO_SIGNALS_SUPPRESSION_FILE="$CONFIG_DIR/suppression.txt"
```

## 8. Sender details

Every draft carries your name, role, organisation, postal address and opt-out text. Drafting
refuses if any of them is missing. Each `read` takes one line:

```sh
read -r "REPO_SIGNALS_SENDER_NAME?Your name: "
read -r "REPO_SIGNALS_SENDER_ROLE?Your role: "
read -r "REPO_SIGNALS_ORG_NAME?Your organisation: "
read -r "REPO_SIGNALS_POSTAL_ADDRESS?Postal address, one line: "
read -r "REPO_SIGNALS_OPTOUT_TEXT?Opt-out text, one line: "
export REPO_SIGNALS_SENDER_NAME REPO_SIGNALS_SENDER_ROLE REPO_SIGNALS_ORG_NAME
export REPO_SIGNALS_POSTAL_ADDRESS REPO_SIGNALS_OPTOUT_TEXT
```

The opt-out text may hold one link, for example `https://example.com/optout`. See
[drafting.md](drafting.md).

## 9. doctor

```sh
repo-signals doctor
```

`doctor` is offline. It prints PASS, WARN or FAIL per line, and it names variables but never
prints their values. Fix every FAIL. `REPO_SIGNALS_DRY_RUN` is still unset at this point, so dry
run is on, and you should see these lines:

- `PASS dry run is on: no paid calls, nothing written`
- `PASS live collection is not armed (needs dry run off and REPO_SIGNALS_SIGNAL_FILE set)`
- `PASS run --live is not armed; failing gates: REPO_SIGNALS_DRY_RUN`
- `WARN hunter: live use is not enabled until dry run is turned off`, or the same line starting
  with `apollo:` if you chose Apollo
- with Hunter and a title in the signal file, also
  `WARN hunter: titles are filtered among the first 3 people Hunter returns, so a title may return nobody even when matching people exist`

If the failing gates name anything besides `REPO_SIGNALS_DRY_RUN`, fix those first.

## 10. The dry run

```sh
repo-signals run
```

The dry run makes no network call and writes nothing. It runs the pipeline on one synthetic item
and prints the stage counts, the column layout, one sample row and one sample draft. Read the
sample draft: it is what a real draft will look like with your sender details.

## 11. The first live run

Turn dry run off for this shell only, check the organisation with one request, and check that
everything is armed:

```sh
export REPO_SIGNALS_DRY_RUN=false
repo-signals probe-org "$TARGET_ORG"
repo-signals doctor
```

`probe-org` should end with `outcome: ok`, and `doctor` should show `run --live is ARMED`. Then
start the run:

```sh
repo-signals run --live
```

It prints the plan: the target and the term; the call budgets, with the most GitHub and provider
calls the run could make and, for the sheet, a minimum of 6 calls for the header check and one
append, plus one more per extra append, with `REPO_SIGNALS_SHEETS_MAX_CALLS` as the hard limit;
the provider; and the first 4 characters of the sheet id. Read it, then type exactly `yes`.
Anything else stops it with nothing requested.

Dry run stays off in this shell until the clean-up step, so do not stop here: finish by running
the clean-up in step 14.

## 12. Check the sheet

Open the sheet. You should see the header row and a few new rows. Read every row and every draft
before you do anything with them. The `reviewed` column is for you to fill in.

A first live run can also end with nothing written, and that is not an error: the run prints
`sheet: nothing to write` and `run: complete; nothing sent by email`. No sheet append is made (the
`requests made` line shows only the 2 sheet calls of the header check), and the collect and people
lines say why:

- `example.com -> example-org (user, website_mismatch: excluded; set confirm_org = true to
  include)` or `(user, no website listed: excluded; ...)`: the organisation was excluded, so no
  repository was searched and no provider call was made (no credit spent). See `confirm_org` in
  step 7.
- `dropped 1: term_not_in_name` and `people skipped: no_evidence 1`: no repository name in that
  organisation contains your term, so there is no evidence and no provider call (no credit
  spent). Try another term.
- `people dropped for example.com: filtered_by_title 2` with
  `people: 1 domain(s) looked up, 0 people found`: the provider was asked once (that call may
  spend a credit), but nobody matched the title. With Hunter, see the first-3 note in step 7.

If the run printed a line such as `people dropped for example.com: filtered_by_title 2`, the
provider found people that were left out, and the line says why by code only. See
[troubleshooting.md](troubleshooting.md#people-stage-drop-counts). With Apollo, check each
title: the search is approximate, so some rows may be adjacent roles you do not want to contact.

## 13. Run it again to prove there are no duplicates

```sh
repo-signals run --live
```

Type `yes` again. The output should end its sheet step with `appended 0 row(s), skipped N
duplicate(s)`, where N is the number of people already there, and the sheet should have no new
copies of them. A match is the same email or, for a row with no email (every Apollo row), the
same name, title and domain. A person already in the sheet is never updated, so any edit you
made is kept.

## 14. Clean up

Turn dry run back on and clear the variables from your shell:

```sh
unset REPO_SIGNALS_DRY_RUN
unset HUNTER_API_KEY APOLLO_API_KEY REPO_SIGNALS_GITHUB_TOKEN
unset REPO_SIGNALS_SHEET_ID REPO_SIGNALS_GOOGLE_CREDENTIALS REPO_SIGNALS_PROVIDER
unset REPO_SIGNALS_MAX_CALLS REPO_SIGNALS_PROVIDER_MAX_CALLS REPO_SIGNALS_SHEETS_MAX_CALLS
unset REPO_SIGNALS_SIGNAL_FILE REPO_SIGNALS_SUPPRESSION_FILE
unset REPO_SIGNALS_SENDER_NAME REPO_SIGNALS_SENDER_ROLE REPO_SIGNALS_ORG_NAME
unset REPO_SIGNALS_POSTAL_ADDRESS REPO_SIGNALS_OPTOUT_TEXT
unset KEY_DOWNLOAD TARGET_DOMAIN TARGET_ORG SEARCH_TERM JOB_TITLE OWN_DOMAIN
```

Delete the key file:

```sh
rm ~/.config/repo-signals/service-account.json
```

Then, in the Google Cloud console, open the service account, then Keys, and **delete the key**
there too. Deleting the local file does not revoke the key. If the project was only for this
test, you can shut the whole project down.
