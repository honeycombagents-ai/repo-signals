"""Build step 6b of the Hugging Face source: run --live for a Hugging Face signal file. Offline:
fake Hugging Face and Google transports and the fake people provider, with sockets patched to
fail. Nothing here makes a real request or sends anything."""
import io
import socket
import unittest
from datetime import date
from unittest import mock

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_hf_live as hf_fx
import test_run as run_fx
import test_sheets as sheets_fx
from repo_signals import config, run_gates
from repo_signals import run as run_mod
from repo_signals.collector import hf_api
from repo_signals.collector.github_api import ALLOWED_HOSTS as GITHUB_HOSTS
from repo_signals.collector.github_fields import WEB_HOST
from repo_signals.collector.hf_api import HuggingFaceClient
from repo_signals.drafting.base import Sender
from repo_signals.drafting.template import TemplateDrafter
from repo_signals.http import HttpClient
from repo_signals.models import Evidence, Person
from repo_signals.sheets.client import build_http
from repo_signals.sheets.layout import COLUMNS

TODAY = date(2026, 10, 27)
NEUTRAL = "turned up in an automated search of public repositories:"
MODEL_SUMMARY = ("The public Hugging Face model example-org/examplelib-7b matches the term "
                 "examplelib, last modified on 2026-10-20.")
MODEL_URL = "https://huggingface.co/example-org/examplelib-7b"
PLAN_LINE = ("max calls: huggingface 7 of 10 (overviews and listing pages); provider at most 1 of "
             "5; sheets 6 for the header check and one append, plus 1 per extra append as needed, "
             "at most 10; rate ceiling 400 per 300 seconds")


class Case(run_fx.Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("a socket"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def hf_env(self, **over):
        path = self.write(f"hf{self.files}.toml", hf_fx.HF_TEXT)
        self.files += 1
        base = {config.SIGNAL_FILE: path, config.GITHUB_TOKEN: None, config.HF_MAX_CALLS: "10"}
        return self.env(**{**base, **over})

    def hf_live(self, env, answer="yes\n", stdin=None, out=None, hf=None, google=None,
                events=None):
        self.events = events if events is not None else []
        self.hf = hf if hf is not None else hf_fx.FakeHF()
        self.google = google if google is not None else sheets_fx.FakeGoogle(header=COLUMNS)
        self.github = gh_fx.FakeGitHub()
        hf_send, google_send = self.hf, self.google

        def hf_transport(*args, **kwargs):
            self.events.append("huggingface")
            return hf_send(*args, **kwargs)

        def google_transport(*args, **kwargs):
            self.events.append("google")
            return google_send(*args, **kwargs)

        self.out = out if out is not None else gh_fx.TTYOut()
        self.err = io.StringIO()
        code = run_mod.run(
            live=True, environ=env, stdin=stdin if stdin is not None else gh_fx.TTYIn(answer),
            out=self.out, err=self.err,
            github_http=HttpClient(GITHUB_HOSTS, transport=self.github),
            hf_http=HttpClient(hf_api.ALLOWED_HOSTS, transport=hf_transport),
            sheets_http=build_http(transport=google_transport), signer=sheets_fx.FakeSigner(),
            today=TODAY, clock=run_fx.fixed_clock, run_id="runtest")
        self.text = self.out.getvalue() + self.err.getvalue()
        self.assert_clean(self.text)
        for sentinel in ("SENTINELBIO", "SENTINELDESC", "SENTINELFULL"):
            self.assertNotIn(sentinel, self.text)
        self.assertEqual(self.github.calls, [])
        return code


class Refusals(Case):
    def test_refuses_without_a_terminal(self):
        for kw in ({"stdin": io.StringIO("yes\n")}, {"out": io.StringIO()}):
            with self.subTest(kw=list(kw)):
                with mock.patch.object(run_mod, "HuggingFaceClient",
                                       side_effect=AssertionError("built")):
                    self.assertEqual(self.hf_live(self.hf_env(), **kw), 2, self.text)
                self.assertIn("stdin and stdout must be a terminal", self.err.getvalue())
                self.assertEqual(self.events, [])

    def test_refuses_without_a_typed_yes(self):
        for answer in ("no\n", "YES\n", "", KeyboardInterrupt()):
            with self.subTest(answer=repr(answer)):
                with mock.patch.object(run_mod, "HuggingFaceClient",
                                       side_effect=AssertionError("built")):
                    self.assertEqual(self.hf_live(self.hf_env(), answer=answer), 1, self.text)
                self.assertIn("run: not confirmed; nothing requested", self.err.getvalue())
                self.assertEqual(self.events, [])

    def test_the_plan_names_the_hugging_face_budget(self):
        self.hf_live(self.hf_env(), answer="no\n")
        lines = self.out.getvalue().splitlines()
        self.assertEqual(lines[:6], [
            "run: live plan", "signal: synthetic-hf",
            "targets: 1 (1 planned, 0 skipped as suppressed)", "terms: 1",
            "source: huggingface; kinds: model, dataset, space", PLAN_LINE])
        self.assertNotIn("github", self.out.getvalue().lower())


class Order(Case):
    def test_client_built_only_after_yes_and_the_preflight(self):
        events = []
        stdin = gh_fx.TTYIn("yes\n")
        stdin.events = events

        def build(*args, **kwargs):
            events.append("client built")
            return HuggingFaceClient(*args, **kwargs)

        with mock.patch.object(run_mod, "HuggingFaceClient", side_effect=build) as built:
            self.assertEqual(self.hf_live(self.hf_env(), stdin=stdin, events=events), 0,
                             self.text)
        self.assertEqual(events[:4], ["prompt answered", "google", "google", "client built"])
        self.assertEqual(built.call_args.args[1].max_calls, 10)
        self.assertIs(built.call_args.kwargs["dry_run"], False)

    def test_sheet_preflight_runs_before_any_hugging_face_request(self):
        self.assertEqual(self.hf_live(self.hf_env()), 0, self.text)
        first_hf = self.events.index("huggingface")
        self.assertGreaterEqual(first_hf, 2)
        self.assertEqual(set(self.events[:first_hf]), {"google"})

    def test_a_failed_preflight_makes_no_hugging_face_request(self):
        google = sheets_fx.FakeGoogle(header=["their", "columns"])
        self.assertEqual(self.hf_live(self.hf_env(), google=google), 4, self.text)
        self.assertNotIn("huggingface", self.events)
        out = self.out.getvalue()
        self.assertIn("run: refused (sheet preflight: header_mismatch)", out)
        self.assertIn("requests made: huggingface 0 of 10, provider 0 of 5, sheets 2 of 10", out)


class Pipeline(Case):
    def test_happy_run_goes_through_every_stage_with_hugging_face_labels(self):
        self.assertEqual(self.hf_live(self.hf_env()), 0, self.text)
        out = self.out.getvalue()
        after = out.split('Type "yes" to run: ', 1)[1]
        for line in ("sheet preflight: ok (the tool's header)", "collect: complete",
                     "planned requests: 7; requests made: 3 (overviews 1, listing pages 2)",
                     "example.com -> example-org (huggingface, confirmed, verified)",
                     "people: 1 domain(s) looked up, 3 people found", "drafts: 3 drafted",
                     "sheets: appended 3 row(s), skipped 0 duplicate(s)",
                     "requests made: huggingface 3 of 10, provider 1 of 5, sheets 5 of 10"):
            self.assertIn(line, after.splitlines())
        self.assertTrue(after.rstrip().endswith("run: complete; nothing sent by email"))
        self.assertNotIn("github", after.lower())
        for word in ("matching 1", "searches"):
            self.assertNotIn(word, after)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["company_domain"], "example.com")
            self.assertTrue(row["signal_evidence"].startswith(MODEL_SUMMARY))
            self.assertIn(NEUTRAL, row["email_draft"])
            self.assertIn(MODEL_SUMMARY, row["email_draft"])
            self.assertIn(MODEL_URL, row["email_draft"])
            self.assertNotIn("GitHub", row["email_draft"])
        for call in self.hf.calls:
            self.assertEqual(call["method"], "GET")
            self.assertNotIn("Authorization", call["headers"])

    def test_a_hugging_face_file_needs_no_github_token(self):
        env = self.hf_env()
        self.assertNotIn(config.GITHUB_TOKEN, env)
        self.assertEqual(run_gates.check_gates(env, signer=sheets_fx.FakeSigner()).failures, ())
        self.assertEqual(self.hf_live(env), 0, self.text)

    def test_a_stop_code_ends_partial_with_the_code_only(self):
        hf = hf_fx.FakeHF(fail={"/api/organizations/example-org/overview": 429})
        self.assertEqual(self.hf_live(self.hf_env(), hf=hf), 3, self.text)
        out = self.out.getvalue()
        self.assertIn("collect: partial (rate_limited)", out)
        self.assertIn("matching stopped (rate_limited): 1 target(s) not attempted", out)
        self.assertIn("sheet: nothing to write", out)
        self.assertTrue(out.rstrip().endswith("run: partial (collect: rate_limited)"))
        self.assertEqual(len(self.hf.calls), 1)


class GitHubUnchanged(Case):
    def test_a_github_file_still_needs_its_gates(self):
        env = self.env(**{config.MAX_CALLS: "1"})
        failures = run_gates.check_gates(env, signer=sheets_fx.FakeSigner()).failures
        self.assertEqual(failures, ("github plan within REPO_SIGNALS_MAX_CALLS",))
        targets = [(f"t{i}.example.com", f"org{i}") for i in range(7)]
        text = 'name = "synthetic"\nterms = [' + ", ".join(f'"term{i}"' for i in range(5)) + "]\n"
        for domain, org in targets:
            text += f'\n[[targets]]\ndomain = "{domain}"\norg = "{org}"\n'
        env = self.env(**{config.SIGNAL_FILE: self.write("wide.toml", text),
                          config.MAX_CALLS: "500"})
        failures = run_gates.check_gates(env, signer=sheets_fx.FakeSigner()).failures
        self.assertEqual(failures, ("github plan within the search-rate ceiling",))

    def test_a_github_run_never_builds_the_hugging_face_client(self):
        with mock.patch.object(run_mod, "HuggingFaceClient", side_effect=AssertionError("built")):
            self.assertEqual(self.live(self.env()), 0, self.text)


class Template(unittest.TestCase):
    def test_draft_text_is_source_neutral_and_grounded(self):
        sender = Sender("Avery Alder", "Engineer", "Example Org", "1 Example Street",
                        "Reply stop to opt out.")
        person = Person("Blake Birch", None, "CTO", "example.com", "fake", 0.5)
        for evidence in (Evidence("example.com", MODEL_SUMMARY, MODEL_URL, "hf-model"),
                         Evidence("example.com", "The public repository example-org/examplelib-"
                                  "core matches the term examplelib, last pushed on 2026-09-30.",
                                  f"https://{WEB_HOST}/example-org/examplelib-core", "repo")):
            with self.subTest(kind=evidence.kind):
                draft = TemplateDrafter("intro").draft(person, (evidence,), sender)
                self.assertIn(f"A public signal about example.com {NEUTRAL}\n{evidence.summary}\n"
                              f"Source: {evidence.url}\n", draft.body)
                self.assertNotIn("public GitHub repositories", draft.body)


if __name__ == "__main__":
    unittest.main()
