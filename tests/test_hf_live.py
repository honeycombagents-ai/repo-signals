"""Build step 6a of the Hugging Face source: collect --live for a Hugging Face file and
hf-probe-org. Offline: a FAKE transport behind http.HttpClient stands in for Hugging Face, and
sockets are patched to fail. Nothing here makes a real request."""
import io
import json
import socket
import unittest
from datetime import date
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import _src  # noqa: F401
import test_collector_live as gh
from repo_signals import config, run_gates
from repo_signals.budget import CallBudget
from repo_signals.cli import main
from repo_signals.collector import hf_api, live
from repo_signals.collector.hf_api import ALLOWED_HOSTS, HuggingFaceClient, next_cursor
from repo_signals.http import HttpClient, RawResponse
from repo_signals.providers.base import ProviderError

TODAY = date(2026, 10, 27)
RATE = {"RateLimit": '"api";r=480;t=200', "RateLimit-Policy": '"fixed window";"api";q=500;w=300'}
OVERVIEW = {"name": "example-org", "fullname": "SENTINELFULL", "details": "SENTINELBIO see "
            "www.example.com", "isVerified": True, "numModels": 3, "numDatasets": 0,
            "numSpaces": 2, "numUsers": 7}
HF_TEXT = ('source = "huggingface"\nname = "synthetic-hf"\nterms = ["examplelib"]\n\n'
           '[[targets]]\ndomain = "example.com"\norg = "example-org"\nconfirm_org = true\n')


def model(name, **over):
    record = {"id": f"example-org/{name}", "author": "example-org",
              "lastModified": "2026-10-20T08:30:00.000Z", "private": False, "gated": False,
              "disabled": False, "description": "SENTINELDESC", "cardData": {"x": "SENTINEL"}}
    record.update(over)
    return record


def space(name, **over):
    record = {"id": f"example-org/{name}", "author": "example-org",
              "lastModified": "2026-10-21T08:30:00.000Z", "private": False, "disabled": False}
    record.update(over)
    return record


class FakeHF:
    """Routes by path; records every request. ``fail`` maps a path to a status to answer."""

    def __init__(self, overview=OVERVIEW, listings=None, fail=None):
        self.overview = overview
        self.listings = {"/api/models": [model("examplelib-7b"), model("other-model")],
                         "/api/datasets": [], "/api/spaces": [space("examplelib-demo")]}
        self.listings.update(listings or {})
        self.fail = dict(fail or {})
        self.calls = []

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        parts = urlsplit(url)
        path = unquote(parts.path)
        self.calls.append({"url": url, "path": path, "query": parse_qs(parts.query),
                           "headers": dict(headers), "method": method})
        if path in self.fail:
            return RawResponse(self.fail[path], dict(RATE), b'{"error": "SENTINEL"}')
        if path.endswith("/overview"):
            if self.overview is None:
                return RawResponse(404, dict(RATE), b'{"error": "SENTINEL"}')
            return RawResponse(200, dict(RATE), json.dumps(self.overview).encode())
        return RawResponse(200, dict(RATE), json.dumps(self.listings[path]).encode())


class Case(gh.Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("a socket"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **over):
        env = {config.DRY_RUN: "false", config.SIGNAL_FILE: self.write("hf.toml", HF_TEXT),
               config.HF_MAX_CALLS: "10"}
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def hf_live(self, env, answer="yes\n", transport=None, stdin=None, out=None):
        self.transport = transport if transport is not None else FakeHF()
        self.out = out if out is not None else gh.TTYOut()
        self.err = io.StringIO()
        stdin = stdin if stdin is not None else gh.TTYIn(answer)
        code = live.run_live(env, stdin=stdin, out=self.out, err=self.err,
                             http=HttpClient(ALLOWED_HOSTS, transport=self.transport), today=TODAY)
        self.text = self.out.getvalue() + self.err.getvalue()
        self.assertNotIn("SENTINEL", self.text.upper())
        return code

    def after_prompt(self):
        return self.out.getvalue().split("these Hugging Face requests: ", 1)[1].splitlines()


PLAN = ["signal: synthetic-hf", "targets: 1 (1 planned, 0 skipped as suppressed)", "terms: 1",
        "source: huggingface; kinds: model, dataset, space",
        "planned requests: at most 7 (1 overview per target, at most 2 listing pages per kind); "
        "budget 10 (REPO_SIGNALS_HF_MAX_CALLS); rate ceiling 400 per 300 seconds"]


class Refusals(Case):
    def assertRefused(self, env, needle, **kw):
        with mock.patch.object(live, "HuggingFaceClient", side_effect=AssertionError("built")):
            code = self.hf_live(env, **kw)
        self.assertEqual(code, 2, self.text)
        self.assertEqual(self.transport.calls, [])
        self.assertIn("collect: refused:", self.err.getvalue())
        self.assertIn(needle, self.err.getvalue())
        self.assertNotIn("Type", self.out.getvalue())

    def test_each_missing_condition(self):
        self.assertRefused(self.env(**{config.DRY_RUN: None}), "dry run is on")
        self.assertRefused(self.env(**{config.DRY_RUN: "true"}), "dry run is on")
        self.assertRefused(self.env(**{config.SIGNAL_FILE: None}),
                           "REPO_SIGNALS_SIGNAL_FILE is not set")
        self.assertRefused(self.env(**{config.HF_MAX_CALLS: "3"}),
                           "plan_over_budget: 7 planned, 3 allowed")
        self.assertRefused(self.env(), "stdin and stdout must be a terminal",
                           stdin=io.StringIO("yes\n"))
        self.assertRefused(self.env(), "stdin and stdout must be a terminal", out=io.StringIO())
        supp = self.write("s.txt", "SENTINEL bad line\n")
        self.assertRefused(self.env(**{config.SUPPRESSION_FILE: supp}),
                           "the suppression list is not usable")

    def test_only_exact_yes(self):
        for answer in ("y\n", "YES\n", "yes \n", "\n", "", "no\n", KeyboardInterrupt()):
            with self.subTest(answer=repr(answer)):
                with mock.patch.object(live, "HuggingFaceClient",
                                       side_effect=AssertionError("built")):
                    code = self.hf_live(self.env(), answer=answer)
                self.assertEqual(code, 1)
                self.assertEqual(self.transport.calls, [])
                self.assertIn("collect: not confirmed; nothing requested", self.err.getvalue())
                self.assertIn('Type "yes" to make these Hugging Face requests: ',
                              self.out.getvalue())


class Order(Case):
    def test_plan_is_printed_before_any_client_is_built(self):
        seen = []

        def build(*args, **kwargs):
            seen.append(self.out.getvalue())
            return HuggingFaceClient(*args, **kwargs)

        self.out = gh.TTYOut()
        with mock.patch.object(live, "HuggingFaceClient", side_effect=build):
            self.assertEqual(self.hf_live(self.env(), out=self.out), 0, self.text)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].splitlines()[:5], PLAN)
        self.assertTrue(seen[0].endswith('Type "yes" to make these Hugging Face requests: '))

    def test_client_built_only_after_yes(self):
        events = []
        stdin = gh.TTYIn("yes\n")
        stdin.events = events

        def build(*args, **kwargs):
            events.append("client built")
            return HuggingFaceClient(*args, **kwargs)

        with mock.patch.object(live, "HuggingFaceClient", side_effect=build) as built:
            self.assertEqual(self.hf_live(self.env(), stdin=stdin), 0, self.text)
        self.assertEqual(events, ["prompt answered", "client built"])
        self.assertEqual(built.call_args.args[1].max_calls, 10)
        self.assertIs(built.call_args.kwargs["dry_run"], False)


class LiveRun(Case):
    def test_happy_run_prints_evidence_and_counts_and_ends_complete(self):
        self.assertEqual(self.hf_live(self.env()), 0, self.text)
        self.assertEqual(self.out.getvalue().splitlines()[:5], PLAN)
        self.assertEqual(self.after_prompt(), [
            "collect: complete",
            "planned requests: 7; requests made: 3 (overviews 1, listing pages 2)",
            "matching: matched 1",
            "example.com -> example-org (huggingface, confirmed, verified)",
            "evidence: example.com: 2", "kept: 2", "dropped 1: term_not_in_name",
            "evidence for example.com:",
            "  summary: The public Hugging Face model example-org/examplelib-7b matches the term "
            "examplelib, last modified on 2026-10-20.",
            "  url: https://huggingface.co/example-org/examplelib-7b",
            "evidence for example.com:",
            "  summary: The public Hugging Face Space example-org/examplelib-demo matches the term "
            "examplelib, last modified on 2026-10-21.",
            "  url: https://huggingface.co/spaces/example-org/examplelib-demo"])
        report = " ".join(self.after_prompt()[:7])
        for github_word in ("GitHub", "search", "core", "matching 1"):
            self.assertNotIn(github_word, report)
        self.assertEqual([c["path"] for c in self.transport.calls],
                         ["/api/organizations/example-org/overview", "/api/models", "/api/spaces"])
        for call in self.transport.calls:
            self.assertEqual(call["method"], "GET")
            self.assertEqual(urlsplit(call["url"]).netloc, "huggingface.co")
            self.assertNotIn("Authorization", call["headers"])
        self.assertNotIn("gated", self.transport.calls[2]["query"]["expand"])

    def test_a_stop_code_ends_partial_with_the_code_only(self):
        t = FakeHF(overview={**OVERVIEW, "numDatasets": 4}, fail={"/api/datasets": 429})
        self.assertEqual(self.hf_live(self.env(), transport=t), 3, self.text)
        lines = self.after_prompt()
        self.assertEqual(lines[0], "collect: partial (rate_limited)")
        self.assertIn("listing incomplete: example.com dataset: rate_limited", lines)
        self.assertIn("listing incomplete: example.com space: not_attempted", lines)
        self.assertIn("evidence: example.com: 1", lines)
        self.assertEqual(len(t.calls), 3)

    def test_an_overview_stop_ends_partial_before_any_listing(self):
        t = FakeHF(fail={"/api/organizations/example-org/overview": 401})
        self.assertEqual(self.hf_live(self.env(), transport=t), 3, self.text)
        lines = self.after_prompt()
        self.assertEqual(lines[0], "collect: partial (unauthorised)")
        self.assertIn("matching stopped (unauthorised): 1 target(s) not attempted", lines)
        self.assertEqual(len(t.calls), 1)

    def test_an_unknown_org_is_unresolved(self):
        t = FakeHF(overview=None)
        self.assertEqual(self.hf_live(self.env(), transport=t), 0, self.text)
        self.assertIn("example.com: org example-org not found on Hugging Face (unresolved)",
                      self.after_prompt())
        self.assertEqual(len(t.calls), 1)


class Probe(Case):
    def probe(self, org, env, transport=None):
        self.transport = transport if transport is not None else FakeHF()
        out, err = io.StringIO(), io.StringIO()
        code = live.run_hf_probe(org, env, out=out, err=err,
                                 http=HttpClient(ALLOWED_HOSTS, transport=self.transport))
        self.out, self.err = out.getvalue(), err.getvalue()
        for text in (self.out, self.err):
            self.assertNotIn("SENTINEL", text.upper())
            self.assertNotIn("://", text)
            self.assertNotIn("www.", text)
        return code

    def test_prints_only_the_allowed_fields_and_never_the_bio(self):
        with mock.patch.object(live, "HuggingFaceClient", wraps=HuggingFaceClient) as built:
            self.assertEqual(self.probe("example-org", self.env()), 0, self.err)
        self.assertEqual(self.out.splitlines(), [
            "hf-probe-org: login shape ok", "is_verified: yes", "models: 3", "datasets: 0",
            "spaces: 2", "outcome: ok"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["path"], "/api/organizations/example-org/overview")
        self.assertEqual(built.call_args.args[1].max_calls, 1)
        self.assertNotIn("Authorization", self.transport.calls[0]["headers"])

    def test_request_errors(self):
        self.assertEqual(self.probe("example-org", self.env(), FakeHF(overview=None)), 1)
        self.assertEqual(self.out.splitlines(),
                         ["hf-probe-org: login shape ok", "outcome: org_not_found"])
        t = FakeHF(fail={"/api/organizations/example-org/overview": 429})
        self.assertEqual(self.probe("example-org", self.env(), t), 1)
        self.assertIn("outcome: rate_limited", self.out)
        t = FakeHF(overview={**OVERVIEW, "isVerified": "SENTINEL"})
        self.assertEqual(self.probe("example-org", self.env(), t), 1)
        self.assertIn("outcome: unexpected_shape", self.out)

    def test_refuses_a_bad_org_shape_or_dry_run_before_any_request(self):
        cases = [(name, self.env()) for name in ("-bad", "example_org", "example.org", "x" * 40,
                                                 "SENTINEL org", "", "../example-org")]
        cases += [("example-org", self.env(**{config.DRY_RUN: None})),
                  ("example-org", self.env(**{config.DRY_RUN: "true"}))]
        for name, env in cases:
            with self.subTest(name=name[:12], dry=env.get(config.DRY_RUN)):
                with mock.patch.object(live, "HuggingFaceClient",
                                       side_effect=AssertionError("built")):
                    self.assertEqual(self.probe(name, env), 2)
                self.assertEqual(self.transport.calls, [])
                self.assertIn("hf-probe-org: refused:", self.err)
                self.assertEqual(self.out, "")

    def test_cli_dispatch(self):
        with mock.patch.object(live, "run_hf_probe", return_value=0) as probe:
            self.assertEqual(main(["hf-probe-org", "example-org"]), 0)
        self.assertEqual(probe.call_args.args[0], "example-org")


class Unchanged(Case):
    def test_run_gates_no_longer_refuse_a_hugging_face_file(self):
        gates = run_gates.check_gates(self.env())
        self.assertNotIn("huggingface_not_implemented", gates.failures)

    def test_github_collect_live_never_builds_the_hugging_face_client(self):
        with mock.patch.object(live, "HuggingFaceClient", side_effect=AssertionError("built")):
            runner = gh.LiveRun("test_complete_run_prints_summary_and_evidence")
            result = unittest.TestResult()
            runner.run(result)
        self.assertEqual((result.failures, result.errors), ([], []))

    def test_cursor_limit_is_1024(self):
        def link(cursor):
            return (f"<https://huggingface.co/api/models?author=example-org&cursor={cursor}>; "
                    'rel="next"')

        self.assertEqual(next_cursor({"link": link("x" * 1024)}, "/api/models"), "x" * 1024)
        with self.assertRaises(ProviderError) as cm:
            next_cursor({"link": link("x" * 1025)}, "/api/models")
        self.assertEqual(cm.exception.code, "bad_link")
        self.assertTrue(hf_api._CURSOR_RE.fullmatch("x" * 1024))
        self.assertIsNone(hf_api._CURSOR_RE.fullmatch("x" * 1025))
        transport = FakeHF()
        client = HuggingFaceClient(HttpClient(ALLOWED_HOSTS, transport=transport), CallBudget(2),
                                   dry_run=False)
        client.list_repos("model", "example-org", cursor="x" * 1024)
        with self.assertRaises(ProviderError) as cm:
            client.list_repos("model", "example-org", cursor="x" * 1025)
        self.assertEqual(cm.exception.code, "bad_cursor")
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
