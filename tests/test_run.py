"""``repo-signals run`` tests. Offline only: a fake GitHub transport, a fake Google transport and
fake people providers. Nothing here makes a real request or sends anything."""
import ast
import builtins
import io
import json
import os
import re
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_sheets as sheets_fx
from repo_signals import config, doctor
from repo_signals import run as run_mod
from repo_signals.budget import CallBudget
from repo_signals.cli import main
from repo_signals.collector.github_api import ALLOWED_HOSTS
from repo_signals.drafting.base import Sender
from repo_signals.drafting.suppression import parse_suppression
from repo_signals.http import HttpClient, RawResponse
from repo_signals.models import Evidence, Person
from repo_signals.providers import ProviderError, Registry
from repo_signals.providers.fake import FakeProvider
from repo_signals.sheets.client import build_http
from repo_signals.sheets.layout import COLUMNS

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 21)
FIXED = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
TOKEN = gh_fx.TOKEN
SENDER_ENV = {config.SENDER_NAME: "Avery Alder", config.SENDER_ROLE: "Engineer",
              config.ORG_NAME: "Example Org", config.POSTAL_ADDRESS: "SENTINELPOSTAL 1 Street",
              config.OPTOUT_TEXT: "SENTINELOPTOUT reply stop to opt out."}
ORGS = {"example-org": gh_fx.ORG,
        "example-labs": {"login": "example-labs", "type": "Organization",
                         "blog": "https://example.invalid", "is_verified": True}}
REPOS = {("examplelib", "example-org"): [gh_fx.repo("example-org", "examplelib-core")],
         ("examplelib", "example-labs"): [gh_fx.repo("example-labs", "examplelib-py")]}
I = {c: i for i, c in enumerate(COLUMNS)}


def fixed_clock():
    return FIXED


def fake_people(domain, titles=()):
    return FakeProvider().find_people(domain, titles, 3)


class Case(unittest.TestCase):
    def setUp(self):
        self.creds = sheets_fx.Creds()
        self.addCleanup(self.creds.close)
        self.dir = self.creds.dir.name
        patcher = mock.patch("time.sleep", side_effect=AssertionError("slept"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.files = 0

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def signal_file(self, targets=(("example.com", "example-org"), ("example.invalid", "example-labs")),
                    extra=""):
        text = f'name = "synthetic"\nterms = ["examplelib"]\n{extra}\n'
        for domain, org in targets:
            text += f'\n[[targets]]\ndomain = "{domain}"\norg = "{org}"\n'
        self.files += 1
        return self.write(f"signal{self.files}.toml", text)

    def env(self, **over):
        env = {config.DRY_RUN: "false", config.SIGNAL_FILE: self.signal_file(),
               config.GITHUB_TOKEN: TOKEN, config.MAX_CALLS: "10",
               config.PROVIDER_MAX_CALLS: "5", config.SHEETS_MAX_CALLS: "10",
               config.SHEET_ID: sheets_fx.SHEET, config.GOOGLE_CREDENTIALS: self.creds.path,
               config.PROVIDER: "fake", **SENDER_ENV}
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def live(self, env, answer="yes\n", stdin=None, out=None, github=None, google=None,
             provider_factory=None, run_id="runtest"):
        self.github = github if github is not None else gh_fx.FakeGitHub(ORGS, REPOS)
        self.google = google if google is not None else sheets_fx.FakeGoogle(header=COLUMNS)
        self.out = out if out is not None else gh_fx.TTYOut()
        self.err = io.StringIO()
        code = run_mod.run(
            live=True, environ=env, stdin=stdin if stdin is not None else gh_fx.TTYIn(answer),
            out=self.out, err=self.err,
            github_http=HttpClient(ALLOWED_HOSTS, transport=self.github),
            sheets_http=build_http(transport=self.google), signer=sheets_fx.FakeSigner(),
            provider_factory=provider_factory, today=TODAY, clock=fixed_clock, run_id=run_id)
        self.text = self.out.getvalue() + self.err.getvalue()
        self.assert_clean(self.text)
        return code

    def assert_clean(self, text):
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("SENTINELSHEETID", text)
        self.assertNotIn(sheets_fx.SHEET, text)
        self.assertNotIn("SENTINELPOSTAL", text)
        self.assertNotIn(self.creds.path, text)
        for m in re.finditer("@", text):  # every email shown is masked: x***@domain
            self.assertEqual(text[max(0, m.start() - 3):m.start()], "***",
                             text[max(0, m.start() - 20):m.start() + 1])

    def rows(self):
        return [dict(zip(COLUMNS, r)) for r in self.google.rows]

    def calls(self):
        return len(self.github.calls), len(self.google.calls)


# ---- dry run ----

class DryRun(Case):
    def dry(self, env=None):
        out, err = io.StringIO(), io.StringIO()
        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError("write attempted")
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("socket.socket", side_effect=AssertionError("socket")), \
                mock.patch.object(run_mod, "GitHubClient", side_effect=AssertionError("github")), \
                mock.patch.object(run_mod.SheetWriter, "write", side_effect=AssertionError("w")), \
                mock.patch("builtins.open", guarded_open):
            code = run_mod.run(environ=env if env is not None else {}, out=out, err=err)
        self.out_text = out.getvalue()
        return code

    def test_no_network_no_files_one_masked_sample(self):
        before = sorted(os.listdir(self.dir))
        self.assertEqual(self.dry(), 0)
        self.assertEqual(sorted(os.listdir(self.dir)), before)
        text = self.out_text
        self.assertEqual(text.count("sample row: "), 1)
        self.assertEqual(text.count("sample draft:"), 1)
        self.assertIn("columns: " + " | ".join(COLUMNS), text)
        self.assertIn("run: dry run (no network, nothing written)", text)
        self.assertIn("people: 1 domain(s) looked up, 3 people found", text)
        self.assertIn("drafts: 3 drafted", text)
        self.assertIn("requests made: github 0, provider 1 of 25, sheets 0", text)
        self.assertRegex(text, r"To: [a-z]\*\*\*@example\.invalid")
        for person in fake_people("example.invalid"):
            self.assertNotIn(person.email, text)
        self.assert_clean(text)

    def test_dry_run_is_the_default_even_with_live_settings(self):
        self.assertEqual(self.dry(self.env()), 0)
        self.assertIn("sender: from the environment", self.out_text)
        self.assertNotIn("SENTINELPOSTAL", self.out_text.split("sample draft:")[0])

    def test_dry_run_refuses_bad_config(self):
        self.assertEqual(self.dry({config.PROVIDER_MAX_CALLS: "SENTINEL"}), 2)


# ---- gates ----

class Gates(Case):
    def assertRefused(self, env, *names, **kw):
        with mock.patch.object(run_mod, "GitHubClient", side_effect=AssertionError("built")):
            code = self.live(env, **kw)
        self.assertEqual(code, 2, self.text)
        self.assertEqual(self.calls(), (0, 0))
        for name in names:
            self.assertIn(name, self.err.getvalue())
        self.assertNotIn("Type", self.out.getvalue())

    def test_each_gate(self):
        cases = [({config.DRY_RUN: None}, config.DRY_RUN), ({config.DRY_RUN: "true"}, config.DRY_RUN),
                 ({config.SIGNAL_FILE: None}, config.SIGNAL_FILE),
                 ({config.SIGNAL_FILE: self.write("bad.toml", 'name = "SENTINEL"\n')},
                  config.SIGNAL_FILE),
                 ({config.SHEET_ID: None}, config.SHEET_ID),
                 ({config.GOOGLE_CREDENTIALS: None}, config.GOOGLE_CREDENTIALS),
                 ({config.PROVIDER: "hunter"}, "HUNTER_API_KEY"),
                 ({config.PROVIDER: "nosuch"}, config.PROVIDER),
                 ({config.MAX_CALLS: "1"}, "github plan within REPO_SIGNALS_MAX_CALLS"),
                 ({config.PROVIDER_MAX_CALLS: "SENTINEL"}, config.PROVIDER_MAX_CALLS),
                 ({config.SHEETS_MAX_CALLS: "501"}, config.SHEETS_MAX_CALLS),
                 ({config.SUPPRESSION_FILE: self.write("s.txt", "SENTINEL bad\n")},
                  config.SUPPRESSION_FILE)]
        for var in config.SENDER_VARS:
            cases.append(({var: None}, var))
        for over, name in cases:
            with self.subTest(name=name):
                self.assertRefused(self.env(**over), name)

    def test_all_failures_listed_by_name(self):
        env = self.env(**{config.DRY_RUN: None, config.SHEET_ID: None, config.OPTOUT_TEXT: None})
        self.assertRefused(env, "failing gates: REPO_SIGNALS_DRY_RUN, REPO_SIGNALS_OPTOUT_TEXT, "
                                "REPO_SIGNALS_SHEET_ID; nothing requested")

    def test_terminal_required(self):
        self.assertRefused(self.env(), "stdin and stdout must be a terminal",
                           stdin=io.StringIO("yes\n"))
        self.assertRefused(self.env(), "stdin and stdout must be a terminal", out=io.StringIO())


# ---- the prompt ----

class Prompt(Case):
    def test_only_exact_yes(self):
        for answer in ("y\n", "YES\n", "yes \n", "\n", "", "no\n", KeyboardInterrupt()):
            with self.subTest(answer=repr(answer)):
                with mock.patch.object(run_mod, "GitHubClient", side_effect=AssertionError("b")):
                    self.assertEqual(self.live(self.env(), answer=answer), 1)
                self.assertEqual(self.calls(), (0, 0))
                self.assertIn("run: not confirmed; nothing requested", self.err.getvalue())

    def test_plan_lines(self):
        self.live(self.env(), answer="no\n")
        lines = self.out.getvalue().splitlines()
        self.assertEqual(lines[:8], [
            "run: live plan", "signal: synthetic", "targets: 2 (2 planned, 0 skipped as suppressed)",
            "terms: 1",
            "max calls: github 4 of 10; provider at most 2 of 5; sheets at most 5 of 10; search "
            "ceiling 30 per minute (unverified)",
            "provider: fake", "sheet: SENT... (masked)", "nothing is ever sent by email"])

    def test_no_yes_flag(self):
        for argv in (["run", "--live", "--yes"], ["run", "--yes"], ["run", "-y"],
                     ["run", "--live", "--force"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(argv)
        text = (ROOT / "src/repo_signals/run.py").read_text()
        self.assertNotIn("--yes", text)
        self.assertNotIn("--force", text)

    def test_real_clients_only_after_confirmation(self):
        events = []
        stdin = gh_fx.TTYIn("yes\n")
        stdin.events = events
        real_client, real_writer = run_mod.GitHubClient, run_mod.SheetWriter

        def client(*a, **k):
            events.append("github client")
            return real_client(*a, **k)

        def writer(*a, **k):
            events.append("sheet writer")
            return real_writer(*a, **k)

        def provider_factory(name, budget, registry):
            events.append("provider")
            return FakeProvider(), False

        with mock.patch.object(run_mod, "GitHubClient", side_effect=client), \
                mock.patch.object(run_mod, "SheetWriter", side_effect=writer):
            self.assertEqual(self.live(self.env(), stdin=stdin,
                                       provider_factory=provider_factory), 0, self.text)
        self.assertEqual(events, ["prompt answered", "github client", "provider", "sheet writer"])


# ---- the live pipeline ----

class Pipeline(Case):
    def test_complete_run(self):
        self.assertEqual(self.live(self.env()), 0, self.text)
        out = self.out.getvalue()
        self.assertIn("planned requests: 4; requests made: 4 (matching 2, searches 2)", out)
        self.assertIn("people: 2 domain(s) looked up, 6 people found", out)
        self.assertIn("drafts: 6 drafted", out)
        self.assertIn("sheets: appended 6 row(s), skipped 0 duplicate(s)", out)
        self.assertIn("requests made: github 4 of 10, provider 2 of 5, sheets 4 of 10", out)
        self.assertTrue(out.rstrip().endswith("run: complete; nothing sent by email"))
        self.assertEqual(self.github.calls[0]["headers"]["Authorization"], "Bearer " + TOKEN)

    def test_rows_have_the_exact_layout(self):
        self.live(self.env())
        appended = json.loads(self.google.calls[-1]["body"])["values"]
        self.assertTrue(all(len(r) == len(COLUMNS) for r in appended))
        rows = self.rows()
        self.assertEqual(len(rows), 6)
        evidence = {"example.com": "The public repository example-org/examplelib-core matches "
                                   "the term examplelib and its primary language on GitHub is "
                                   "Python, last pushed on 2026-09-30. https://"
                                   f"{gh_fx.WEB_HOST}/example-org/examplelib-core"}
        for row in rows:
            self.assertEqual(row["reviewed"], "")
            self.assertEqual(row["run_id"], "runtest")
            self.assertEqual(row["written_at"], "2026-10-21T12:00:00Z")
            self.assertTrue(row["email_draft"].startswith("Subject: A public signal from "))
            self.assertIn("SENTINELOPTOUT", row["email_draft"])
            if row["company_domain"] == "example.com":
                self.assertEqual(row["signal_evidence"], evidence["example.com"])
                self.assertIn(f"https://{gh_fx.WEB_HOST}/example-org/examplelib-core",
                              row["email_draft"])

    def test_existing_rows_never_updated(self):
        person = fake_people("example.com")[0]
        existing = [person.name, person.email, person.title, "example.com", "fake", "0.5", "e",
                    "Hand edited draft", "TRUE", "old", "2026-01-01T00:00:00Z"]
        google = sheets_fx.FakeGoogle(header=COLUMNS, rows=[existing])
        self.assertEqual(self.live(self.env(), google=google), 0, self.text)
        self.assertEqual(google.rows[0], existing)
        self.assertNotIn("put", google.kinds())
        self.assertIn("skipped 1 duplicate(s)", self.out.getvalue())
        for row in self.rows()[1:]:
            self.assertNotEqual(row["email"], person.email)
            self.assertEqual(row["reviewed"], "")

    def test_formula_guard_on_external_fields(self):
        evil = Person(name="=HYPERLINK(1)", email="=x" + "@example.invalid", title="+cmd",
                      company_domain="example.com", source="@src", confidence=0.5)

        class Evil:
            name, required_env = "evil", ()

            def find_people(self, domain, titles, limit):
                return [evil] if domain == "example.com" else []

        def factory(name, budget, registry):
            return Evil(), False

        self.live(self.env(), provider_factory=factory)
        row = self.rows()[0]
        for col in ("name", "email", "title", "source"):
            self.assertTrue(row[col].startswith("'"), col)

    def test_run_id_random_per_run(self):
        ids = set()
        for _ in range(3):
            self.live(self.env(), run_id=None)
            ids.add(self.rows()[0]["run_id"])
        self.assertEqual(len(ids), 3)
        for run_id in ids:
            self.assertRegex(run_id, r"\A[0-9a-f]{16}\Z")

    def test_titles_reach_the_provider(self):
        seen = []

        def factory(name, budget, registry):
            class Recorder(FakeProvider):
                def find_people(self, domain, titles, limit):
                    seen.append((domain, titles, limit))
                    return super().find_people(domain, titles, limit)
            return Recorder(), False

        env = self.env(**{config.SIGNAL_FILE: self.signal_file(extra='titles = ["CTO"]')})
        self.live(env, provider_factory=factory)
        self.assertEqual(seen, [("example.com", ("CTO",), 3), ("example.invalid", ("CTO",), 3)])


class Budgets(Case):
    def test_provider_cap_stops_only_the_people_stage(self):
        code = self.live(self.env(**{config.PROVIDER_MAX_CALLS: "1"}))
        self.assertEqual(code, 3, self.text)
        out = self.out.getvalue()
        self.assertIn("people skipped: budget_exhausted 1", out)
        self.assertIn("run: partial (people: budget_exhausted)", out)
        self.assertIn("requests made: github 4 of 10, provider 1 of 1, sheets 4 of 10", out)
        self.assertEqual(len(self.rows()), 3)

    def test_sheets_cap_stops_only_the_sheet_stage(self):
        code = self.live(self.env(**{config.SHEETS_MAX_CALLS: "2"}))
        self.assertEqual(code, 3, self.text)
        out = self.out.getvalue()
        self.assertIn("run: partial (sheet: budget_exhausted)", out)
        self.assertIn("requests made: github 4 of 10, provider 2 of 5, sheets 2 of 2", out)
        self.assertEqual(self.rows(), [])

    def test_three_separate_budgets(self):
        made = []
        real = run_mod.CallBudget

        def record(n):
            made.append(n)
            return real(n)

        with mock.patch.object(run_mod, "CallBudget", side_effect=record):
            self.live(self.env(**{config.MAX_CALLS: "7", config.PROVIDER_MAX_CALLS: "6",
                                  config.SHEETS_MAX_CALLS: "9"}))
        self.assertEqual(made, [7, 6, 9])

    def test_github_failure_partial_later_stages_continue(self):
        github = gh_fx.FakeGitHub(ORGS, REPOS, fail={("repos", "examplelib", "example-org"):
                                                     gh_fx.ok({}, status=403)})
        self.assertEqual(self.live(self.env(), github=github), 3, self.text)
        out = self.out.getvalue()
        self.assertIn("run: partial (collect: rate_limited)", out)
        self.assertEqual(len(github.calls), 3)
        self.assertEqual(self.rows(), [])  # no evidence was collected, so nothing to write
        self.assertIn("sheet: nothing to write", out)

    def test_other_github_error_keeps_the_rest(self):
        github = gh_fx.FakeGitHub(ORGS, REPOS, fail={("repos", "examplelib", "example-org"):
                                                     gh_fx.ok({}, status=502)})
        self.assertEqual(self.live(self.env(), github=github), 3)
        self.assertEqual({r["company_domain"] for r in self.rows()}, {"example.invalid"})

    def test_sheet_refusal_exits_4(self):
        google = sheets_fx.FakeGoogle(header=["their", "columns"])
        self.assertEqual(self.live(self.env(), google=google), 4, self.text)
        self.assertIn("run: refused (sheet: header_mismatch)", self.out.getvalue())

    def test_provider_errors_continue(self):
        class Flaky(FakeProvider):
            def find_people(self, domain, titles, limit):
                if domain == "example.com":
                    raise ProviderError("server_error")
                return super().find_people(domain, titles, limit)

        def factory(name, budget, registry):
            return Flaky(), False

        self.assertEqual(self.live(self.env(), provider_factory=factory), 3, self.text)
        self.assertIn("people skipped: server_error 1", self.out.getvalue())
        self.assertIn("run: partial (people: server_error)", self.out.getvalue())
        self.assertEqual({r["company_domain"] for r in self.rows()}, {"example.invalid"})


# ---- the stages on their own ----

EV = (Evidence("example.com", "The public repository example-org/examplelib-core matches the "
                              "term examplelib, last pushed on 2026-09-30.",
               "https://example.invalid/example-org/examplelib-core", "repo"),)
SENDER = Sender("Avery Alder", "Engineer", "Example Org", "1 Example Street",
                "Reply stop to opt out.")


class Stages(unittest.TestCase):
    def test_people_skip_suppressed_and_evidence_less_domains(self):
        calls = []

        class Recorder(FakeProvider):
            def find_people(self, domain, titles, limit):
                calls.append(domain)
                return super().find_people(domain, titles, limit)

        evidence = {"example.com": EV, "example.invalid": (), "shop.example.com": EV,
                    "blocked.example.invalid": EV}
        people, counts, stop = run_mod.people_stage(
            evidence, Recorder(), False, CallBudget(10), (),
            parse_suppression("blocked.example.invalid\nsomeone" + "@shop.example.com\n"))
        self.assertEqual(calls, ["example.com"])
        self.assertEqual(dict(counts), {"looked_up": 1, "no_evidence": 1, "suppressed": 2})
        self.assertIsNone(stop)
        self.assertEqual(len(people), 3)

    def test_self_charging_provider_stop(self):
        class Charging:
            name, required_env = "charging", ()

            def find_people(self, domain, titles, limit):
                raise ProviderError("budget_exhausted")

        people, counts, stop = run_mod.people_stage({"example.com": EV, "example.org.example.com": EV},
                                                    Charging(), True, CallBudget(1), (), None)
        self.assertEqual((stop, dict(counts)), ("budget_exhausted",
                                                {"budget_exhausted": 1, "not_attempted": 1}))

    def test_drafts_skip_nameless_suppressed_and_evidence_less(self):
        people = [(Person("Blake Birch", "blake" + "@example.com", "CTO", "example.com", "fake",
                          0.5), EV),
                  (Person("?", None, "CTO", "example.com", "fake", 0.5), EV),
                  (Person("Casey Cedar", "casey" + "@example.com", "CTO", "example.com", "fake",
                          0.5), EV),
                  (Person("Devon Hazel", None, "CTO", "other.example.com", "fake", 0.5), EV)]
        records, counts = run_mod.draft_stage(people, SENDER,
                                              parse_suppression("casey" + "@example.com\n"))
        self.assertEqual(dict(counts), {"drafted": 1, "no_name": 1, "suppressed": 1,
                                        "no_evidence": 1})
        self.assertEqual([r[0].name for r in records], ["Blake Birch", "?", "Devon Hazel"])
        drafted = records[0][2]
        self.assertIn(EV[0].url, drafted)
        self.assertIn(SENDER.optout_text, drafted)
        self.assertEqual(records[1][2], "")
        self.assertEqual(records[2][2], "")
        self.assertEqual(records[0][1], f"{EV[0].summary} {EV[0].url}")


class WriterDraftOnAppend(unittest.TestCase):
    def test_draft_only_on_new_rows(self):
        creds = sheets_fx.Creds()
        self.addCleanup(creds.close)
        person = Person("Avery Alder", "avery" + "@example.invalid", "CTO", "example.com", "fake",
                        0.5)
        existing = [person.name, person.email, "CTO", "example.com", "fake", "0.5", "e",
                    "human draft", "TRUE", "r0", "t0"]
        google = sheets_fx.FakeGoogle(header=COLUMNS, rows=[existing])
        from repo_signals.sheets.writer import SheetWriter
        writer = SheetWriter(config.Settings(dry_run=False, sheet_id=sheets_fx.SHEET,
                                             google_credentials=creds.path),
                             budget=CallBudget(10), http=build_http(transport=google),
                             signer=sheets_fx.FakeSigner(), out=io.StringIO(),
                             clock=fixed_clock, run_id="r1")
        new = Person("Blake Birch", "blake" + "@example.invalid", "CTO", "example.com", "fake", 0.5)
        result = writer.write([(person, "ev", "=NEW DRAFT"), (new, "ev", "Subject: x\n\nbody")])
        self.assertEqual((result.appended, result.skipped), (1, 1))
        self.assertEqual(google.rows[0], existing)
        self.assertEqual(google.rows[1][I["email_draft"]], "Subject: x\n\nbody")
        self.assertEqual(google.rows[1][I["reviewed"]], "")
        writer.write([(new, "ev", "'=quoted")])
        self.assertEqual(len(google.rows), 2)
        with self.assertRaises(ValueError):
            writer.write([(new, "ev", "draft", "extra")])
        with self.assertRaises(ValueError):
            writer.write([(new, "ev", 5)])
        self.assertEqual(writer.build_row(new, "ev", "t", "=1+1")[I["email_draft"]], "'=1+1")


# ---- doctor, imports, style ----

class DoctorAndSource(Case):
    def doctor(self, env):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        code = doctor.run(environ=env, registry=reg, out=out, which=str)
        self.assert_clean(out.getvalue())
        return code, out.getvalue()

    def test_doctor_lines(self):
        code, out = self.doctor(self.env())
        self.assertIn("WARN run --live is ARMED: every offline gate passes", out)
        self.assertIn("PASS REPO_SIGNALS_PROVIDER_MAX_CALLS is within the hard bound of 500", out)
        code, out = self.doctor(self.env(**{config.SHEET_ID: None, config.SENDER_ROLE: None}))
        self.assertIn("PASS run --live is not armed; failing gates: REPO_SIGNALS_SENDER_ROLE, "
                      "REPO_SIGNALS_SHEET_ID", out)
        code, out = self.doctor(self.env(**{config.SHEETS_MAX_CALLS: "0"}))
        self.assertEqual(code, 1)
        self.assertIn("FAIL REPO_SIGNALS_SHEETS_MAX_CALLS must be a whole number", out)

    def test_no_email_sending_code(self):
        bad_modules = ("smtplib", "imaplib", "poplib", "email.mime", "email.message",
                       "email.utils")
        for path in (ROOT / "src/repo_signals").rglob("*.py"):
            text = path.read_text()
            for word in ("sendmail", "send_message", "SMTP(", "starttls"):
                self.assertNotIn(word, text, path.name)
            for node in ast.walk(ast.parse(text)):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    self.assertFalse(name.startswith(bad_modules), f"{path.name}: {name}")

    def test_cli_dispatch(self):
        with mock.patch.object(run_mod, "run", return_value=0) as run:
            self.assertEqual(main(["run"]), 0)
            self.assertEqual(main(["run", "--live"]), 0)
        self.assertEqual([c.kwargs for c in run.call_args_list], [{"live": False}, {"live": True}])

    def test_forbidden_keyword_absent(self):
        word = re.compile("lam" + "bda", re.IGNORECASE)
        for name in ("src/repo_signals/run.py", "src/repo_signals/run_gates.py",
                     "src/repo_signals/config.py", "src/repo_signals/doctor.py",
                     "src/repo_signals/cli.py", "src/repo_signals/sheets/writer.py",
                     "src/repo_signals/collector/signal.py",
                     "src/repo_signals/collector/orchestrate.py", "tests/test_run.py",
                     "docs/run.md"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))


if __name__ == "__main__":
    unittest.main()
