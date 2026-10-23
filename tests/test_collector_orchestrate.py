"""Collector PR 5 tests: orchestration. Offline and synthetic only: a fake client stands in for
GitHub, and every record is made up in the ASSUMED shape, with personal and free-text fields
(SENTINEL) that must never reach Evidence or a report."""
import ast
import io
import os
import subprocess
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import _src  # noqa: F401
from repo_signals.collector.github_fields import WEB_HOST
from repo_signals.collector.orchestrate import (COMPLETE, PARTIAL, REFUSED, SkippedSearch,
                                                collect, evidence_lookup_from, summarize)
from repo_signals.collector.signal import parse_signal
from repo_signals.config import Settings
from repo_signals.drafting.base import Sender
from repo_signals.drafting.sheet import fill_drafts
from repo_signals.drafting.suppression import parse_suppression
from repo_signals.drafting.template import TemplateDrafter
from repo_signals.models import Evidence
from repo_signals.providers import ProviderError

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 21)
PUSHED_AFTER = TODAY - timedelta(days=365)
EMAIL = "sentinel.person" + "@example.invalid"


class Found(NamedTuple):
    items: tuple


def item(login, name, **over):
    record = {"id": 1, "name": name, "full_name": f"{login}/{name}",
              "html_url": f"https://{WEB_HOST}/{login}/{name}", "language": "Python",
              "pushed_at": "2026-09-30T12:00:00Z", "fork": False, "archived": False,
              "private": False, "description": "SENTINELDESC examplelib gadget",
              "topics": ["examplelib", "gadget", "SENTINELTOPIC"],
              "owner": {"login": login, "type": "Organization", "name": "SENTINELOWNER",
                        "email": EMAIL},
              "contributors": [{"login": "SENTINELCONTRIB"}]}
    record.update(over)
    return record


def org(login, blog, verified=None):
    return {"login": login, "type": "Organization", "blog": blog, "is_verified": verified,
            "description": "SENTINELORGDESC"}


class FakeClient:
    def __init__(self, orgs=None, repos=None, searches=None):
        self.orgs = dict(orgs or {})
        self.repos = dict(repos or {})
        self.searches = dict(searches or {})
        self.calls = []

    def get_org(self, login):
        self.calls.append(("get_org", login))
        value = self.orgs.get(login, ProviderError("not_found"))
        if isinstance(value, BaseException):
            raise value
        return value

    def search_orgs(self, name):
        self.calls.append(("search_orgs", name))
        return Found(tuple({"login": x, "type": "Organization"}
                           for x in self.searches.get(name, ())))

    def search_repos(self, term, org_login, language=None, pushed_after=None):
        self.calls.append(("search_repos", term, org_login, language, pushed_after))
        value = self.repos.get((term, org_login), [])
        if isinstance(value, BaseException):
            raise value
        return Found(tuple(value))


def signal(targets, terms=("examplelib", "gadget"), extra=""):
    quoted = ", ".join(f'"{t}"' for t in terms)
    text = f'name = "synthetic"\nterms = [{quoted}]\n{extra}\n'
    for domain, org_login, confirm in targets:
        text += f'\n[[targets]]\ndomain = "{domain}"\n'
        if org_login:
            text += f'org = "{org_login}"\n'
        if confirm:
            text += "confirm_org = true\n"
    return parse_signal(text)


TWO = [("example.com", "example-org", False), ("example.invalid", "example-labs", False)]
ORGS = {"example-org": org("example-org", "https://example.com"),
        "example-labs": org("example-labs", "https://www.example.invalid")}


def run(sig, client, suppression=None, cap=25):
    return collect(sig, client, suppression, cap, TODAY)


def clean(test, lines):
    for line in lines:
        test.assertNotIn("SENTINEL", line.upper())
        test.assertNotIn("://", line)
        test.assertNotIn("@", line)


class HappyPath(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient(ORGS, {
            ("examplelib", "example-org"): [item("example-org", "examplelib-core"),
                                            item("example-org", "widget-ExampleLib")],
            ("gadget", "example-org"): [item("example-org", "gadget-ui")],
            ("examplelib", "example-labs"): [item("example-labs", "ExampleLib-py")],
            ("gadget", "example-labs"): []})
        self.result = run(signal(TWO), self.client)

    def test_exact_calls(self):
        self.assertEqual(self.client.calls, [
            ("get_org", "example-org"), ("get_org", "example-labs"),
            ("search_repos", "examplelib", "example-org", None, PUSHED_AFTER),
            ("search_repos", "gadget", "example-org", None, PUSHED_AFTER),
            ("search_repos", "examplelib", "example-labs", None, PUSHED_AFTER),
            ("search_repos", "gadget", "example-labs", None, PUSHED_AFTER)])

    def test_evidence_and_status(self):
        r = self.result
        self.assertEqual(r.run_report.status, COMPLETE)
        self.assertEqual((r.run_report.planned_requests, r.run_report.searches_made), (6, 4))
        self.assertEqual({d: len(e) for d, e in r.evidence_by_domain.items()},
                         {"example.com": 3, "example.invalid": 1})
        for items in r.evidence_by_domain.values():
            for e in items:
                self.assertIsInstance(e, Evidence)
                self.assertEqual(e.kind, "repo")
                self.assertNotIn("SENTINEL", e.summary.upper())
        first = r.evidence_by_domain["example.com"][0]
        self.assertEqual(first.summary, "The public repository example-org/examplelib-core "
                                        "matches the term examplelib and its primary language "
                                        "on GitHub is Python, last pushed on 2026-09-30.")

    def test_summary_lines(self):
        lines = summarize(self.result)
        self.assertEqual(lines, [
            "collect: complete", "planned requests: 6; requests made: 6 (matching 2, searches 4)",
            "matching: matched 2",
            "example.com -> example-org (user)", "example.invalid -> example-labs (user)",
            "evidence: example.com: 3", "evidence: example.invalid: 1", "kept: 4"])
        clean(self, lines)

    def test_language_is_passed(self):
        client = FakeClient(ORGS)
        run(signal(TWO[:1], terms=("examplelib",), extra='language = "Go"'), client)
        self.assertEqual(client.calls[-1], ("search_repos", "examplelib", "example-org", "Go",
                                            PUSHED_AFTER))


class Rules(unittest.TestCase):
    def collect_items(self, items, terms=("examplelib",)):
        client = FakeClient(ORGS, {("examplelib", "example-org"): items})
        return run(signal(TWO[:1], terms=terms), client)

    def test_term_must_be_in_the_name(self):
        r = self.collect_items([
            item("example-org", "widget"),                                  # description only
            item("example-org", "toolkit", description="nothing", topics=["examplelib"]),
            item("example-org", "EXAMPLELIB-cli"), item("example-org", "my-examplelib")])
        names = [e.url.rsplit("/", 1)[1] for e in r.evidence_by_domain["example.com"]]
        self.assertEqual(names, ["EXAMPLELIB-cli", "my-examplelib"])
        lines = summarize(r)
        clean(self, lines)  # the dropped items' descriptions carry SENTINEL text
        self.assertIn("dropped 2: term_not_in_name", lines)
        self.assertEqual(r.filter_report.dropped["term_not_in_name"], 2)

    def test_existing_filters_apply(self):
        r = self.collect_items([
            item("example-org", "examplelib-fork", fork=True),
            item("example-org", "examplelib-old", archived=True),
            item("example-org", "examplelib-secret", private=True),
            item("example-org", "examplelib-stale", pushed_at="2025-01-01T00:00:00Z"),
            item("SENTINELUSER", "examplelib-x",
                 owner={"login": "SENTINELUSER", "type": "User"}),
            {"name": "SENTINEL broken"}, item("example-org", "examplelib-ok")])
        self.assertEqual(len(r.evidence_by_domain["example.com"]), 1)
        self.assertEqual(dict(r.filter_report.dropped), {
            "fork": 1, "archived": 1, "private": 1, "outside_window": 1, "owner_not_org": 1,
            "malformed_record": 1})
        clean(self, summarize(r))

    def test_cap_of_ten_and_url_dedup(self):
        items = [item("example-org", f"examplelib-{i}") for i in range(13)]
        r = self.collect_items(items)
        self.assertEqual(len(r.evidence_by_domain["example.com"]), 10)
        self.assertEqual(r.filter_report.dropped["over_target_cap"], 3)
        both = item("example-org", "examplelib-gadget")
        client = FakeClient(ORGS, {("examplelib", "example-org"): [both, both],
                                   ("gadget", "example-org"): [both]})
        r = run(signal(TWO[:1]), client)
        self.assertEqual(len(r.evidence_by_domain["example.com"]), 1)
        self.assertEqual(r.filter_report.dropped["duplicate_url"], 2)
        urls = [e.url for e in r.evidence_by_domain["example.com"]]
        self.assertEqual(len(urls), len(set(urls)))


class Stops(unittest.TestCase):
    def searches(self, client):
        return [c for c in client.calls if c[0] == "search_repos"]

    def test_rate_limited_mid_run_stops_and_is_partial(self):
        for code in ("rate_limited", "budget_exhausted"):
            with self.subTest(code=code):
                client = FakeClient(ORGS, {
                    ("examplelib", "example-org"): [item("example-org", "examplelib-core")],
                    ("gadget", "example-org"): ProviderError(code)})
                r = run(signal(TWO), client)
                self.assertEqual(len(self.searches(client)), 2)
                self.assertEqual((r.run_report.status, r.run_report.code), (PARTIAL, code))
                self.assertEqual(r.run_report.skipped_searches, (
                    SkippedSearch("example.com", 2, code),
                    SkippedSearch("example.invalid", 1, "not_attempted"),
                    SkippedSearch("example.invalid", 2, "not_attempted")))
                self.assertEqual(len(r.evidence_by_domain["example.com"]), 1)
                lines = summarize(r)
                self.assertEqual(lines[0], f"collect: partial ({code})")
                self.assertIn(f"search skipped: example.com term 2: {code}", lines)

    def test_rate_limited_during_matching_means_no_searches(self):
        client = FakeClient({"example-org": ProviderError("rate_limited"),
                             "example-labs": ORGS["example-labs"]})
        r = run(signal(TWO), client)
        self.assertEqual(self.searches(client), [])
        self.assertEqual((r.run_report.status, r.run_report.code), (PARTIAL, "rate_limited"))
        self.assertEqual(client.calls, [("get_org", "example-org")])

    def test_budget_exhausted_during_matching_stops_searches(self):
        client = FakeClient({"example-org": ORGS["example-org"],
                             "example-labs": ProviderError("budget_exhausted")})
        r = run(signal(TWO), client)
        self.assertEqual(self.searches(client), [])
        self.assertEqual(r.run_report.code, "budget_exhausted")
        self.assertEqual(len(r.run_report.skipped_searches), 2)

    def test_other_errors_continue(self):
        client = FakeClient(ORGS, {
            ("examplelib", "example-org"): ProviderError("server_error"),
            ("gadget", "example-org"): RuntimeError("SENTINEL https://example.com/x"),
            ("examplelib", "example-labs"): [item("example-labs", "examplelib-py")]})
        r = run(signal(TWO), client)
        self.assertEqual(len(self.searches(client)), 4)
        self.assertEqual((r.run_report.status, r.run_report.code), (PARTIAL, None))
        self.assertEqual(r.run_report.skipped_searches, (
            SkippedSearch("example.com", 1, "server_error"),
            SkippedSearch("example.com", 2, "client_error")))
        self.assertEqual(len(r.evidence_by_domain["example.invalid"]), 1)
        self.assertEqual(summarize(r)[0], "collect: partial")
        clean(self, summarize(r))

    def test_unexpected_search_shape(self):
        class Odd(FakeClient):
            def search_repos(self, *args):
                self.calls.append(("search_repos",) + args)
                return "SENTINEL"

        r = run(signal(TWO[:1], terms=("examplelib",)), Odd(ORGS))
        self.assertEqual(r.run_report.skipped_searches,
                         (SkippedSearch("example.com", 1, "unexpected_shape"),))


class Targets(unittest.TestCase):
    def test_unmatched_ambiguous_and_mismatch_get_no_search(self):
        sig = signal([("example.com", "example-org", False), ("example.invalid", None, False),
                      ("shop.example.com", None, False)], terms=("examplelib",))
        client = FakeClient({
            "example-org": org("example-org", "https://SENTINELBLOG.example.invalid"),
            "cand1": org("cand1", "https://shop.example.com", True),
            "cand2": org("cand2", "https://shop.example.com", True)},
            searches={"example": ["cand1", "cand2"]})
        r = run(sig, client)
        self.assertEqual([c for c in client.calls if c[0] == "search_repos"], [])
        self.assertEqual(r.evidence_by_domain, {})
        self.assertEqual(r.match_report.counts, {"website_mismatch": 1, "unmatched": 1,
                                                 "ambiguous": 1})
        self.assertEqual(r.run_report.status, COMPLETE)
        clean(self, summarize(r))

    def test_confirmed_mismatch_is_searched(self):
        sig = signal([("example.com", "example-org", True)], terms=("examplelib",))
        client = FakeClient({"example-org": org("example-org", "https://example.invalid")})
        run(sig, client)
        self.assertEqual(client.calls[-1][0], "search_repos")

    def test_suppressed_targets_skipped(self):
        client = FakeClient(ORGS)
        r = run(signal(TWO), client, parse_suppression("example.invalid\n"))
        self.assertNotIn(("get_org", "example-labs"), client.calls)
        self.assertEqual({c[2] for c in client.calls if c[0] == "search_repos"}, {"example-org"})
        self.assertEqual(r.run_report.suppressed, ("example.invalid",))
        self.assertIn("skipped (suppressed): example.invalid", summarize(r))


class Refusal(unittest.TestCase):
    def test_over_budget_makes_zero_calls(self):
        client = FakeClient(ORGS)
        r = run(signal(TWO), client, cap=5)
        self.assertEqual(client.calls, [])
        self.assertEqual((r.run_report.status, r.run_report.code), (REFUSED, "plan_over_budget"))
        self.assertEqual(r.evidence_by_domain, {})
        self.assertIsNone(r.match_report)
        self.assertEqual(summarize(r), ["collect: refused: plan_over_budget: 6 planned, "
                                        "5 allowed; nothing requested"])

    def test_over_the_rate_ceiling_makes_zero_calls(self):
        terms = tuple(f"term{i}" for i in range(5))
        targets = [(f"t{i}.example.com", f"org{i}", False) for i in range(7)]
        client = FakeClient()
        r = run(signal(targets, terms=terms), client, cap=500)
        self.assertEqual(client.calls, [])
        self.assertEqual(r.run_report.code, "plan_over_search_rate")
        ok_targets = targets[:6]
        r = run(signal(ok_targets, terms=terms), client, cap=500)
        self.assertNotEqual(r.run_report.status, REFUSED)


class HandOff(unittest.TestCase):
    def test_lookup(self):
        client = FakeClient(ORGS, {("examplelib", "example-org"): [item("example-org",
                                                                        "examplelib-core")]})
        result = run(signal(TWO, terms=("examplelib",)), client)
        lookup = evidence_lookup_from(result)
        self.assertEqual(len(lookup("example.com")), 1)
        self.assertEqual(lookup("EXAMPLE.COM"), lookup("example.com"))
        self.assertIsInstance(lookup("example.com"), tuple)
        self.assertEqual(lookup("example.invalid"), ())
        self.assertEqual(lookup("unknown.example.com"), ())
        self.assertEqual(lookup(None), ())

    def test_fill_drafts_accepts_it(self):
        client = FakeClient(ORGS, {("examplelib", "example-org"): [item("example-org",
                                                                        "examplelib-core")]})
        lookup = evidence_lookup_from(run(signal(TWO, terms=("examplelib",)), client))
        sender = Sender("Avery Alder", "Engineer", "Example Org", "1 Example Street",
                        "Reply stop to opt out.")
        row = ["Blake Birch", "", "CTO", "example.com", "fake", "0.5", "", "", ""]
        out = fill_drafts([row], TemplateDrafter(), lookup, sender=sender, suppression=None,
                          settings=Settings(dry_run=False), environ={}, out=io.StringIO())
        self.assertIn("example-org/examplelib-core matches the term examplelib", out[0][7])


class Source(unittest.TestCase):
    def test_not_wired_to_the_cli(self):
        for name in ("src/repo_signals/cli.py", "src/repo_signals/collector/dry_run.py",
                     "src/repo_signals/doctor.py"):
            self.assertNotIn("orchestrate", (ROOT / name).read_text(), name)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import repo_signals.cli;"
                "print('repo_signals.collector.orchestrate' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code, str(_src.SRC)],
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)
        r = subprocess.run([sys.executable, "-m", "repo_signals", "collect"],
                           env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_src.SRC),
                                "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stderr), (2, "usage: repo-signals collect --dry-run | --live\n"))

    def test_no_http_machinery(self):
        text = (ROOT / "src/repo_signals/collector/orchestrate.py").read_text()
        self.assertNotIn("github_api", text)
        self.assertNotIn("GitHubClient", text)
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                for n in names:
                    self.assertFalse(n.split(".")[0] in ("http", "socket", "ssl", "urllib"), n)


if __name__ == "__main__":
    unittest.main()
