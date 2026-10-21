"""The "org not visible" 422: split from bad_query. Offline: a FAKE transport only."""
import json
import re
import traceback
import unittest
from datetime import date
from pathlib import Path
from typing import NamedTuple
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import _src  # noqa: F401
from repo_signals.budget import CallBudget
from repo_signals.collector import github_api as api
from repo_signals.collector.github_api import ALLOWED_HOSTS, GitHubClient
from repo_signals.collector.github_fields import WEB_HOST
from repo_signals.collector.matching import ERROR, MATCHED, match_all
from repo_signals.collector.orchestrate import PARTIAL, SkippedSearch, collect, summarize
from repo_signals.collector.signal import parse_signal
from repo_signals.http import HttpClient, RawResponse
from repo_signals.providers import ProviderError

ROOT = Path(__file__).resolve().parent.parent
MESSAGE = api.ORG_NOT_VISIBLE_MESSAGE
TODAY = date(2026, 10, 21)


def raw(data, status=422, text=None):
    body = text if text is not None else json.dumps(data).encode()
    return RawResponse(status, {"X-RateLimit-Remaining": "50"}, body)


def not_visible_body(**extra):
    body = {"message": "Validation Failed",
            "errors": [{"message": MESSAGE, "resource": "Search", "field": "q", "code": "invalid",
                        "hint": "SENTINELHINT"}],
            "documentation_url": "https://example.com/SENTINELDOC"}
    body.update(extra)
    return body


ORG = {"login": "example-org", "type": "Organization", "blog": "https://example.com",
       "is_verified": True}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        self.calls.append(url)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def everything(e):
    return "".join(traceback.format_exception(e)) + repr(e) + str(e.args)


class Client(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("time.sleep", side_effect=AssertionError("slept"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, *responses):
        self.transport = FakeTransport(*responses)
        self.budget = CallBudget(10)
        return GitHubClient(HttpClient(ALLOWED_HOSTS, transport=self.transport), self.budget,
                            "SENTINELTOKEN" + "q" * 20, dry_run=False)

    def code(self, response):
        c = self.client(response)
        with self.assertRaises(ProviderError) as cm:
            c.search_repos("examplelib", "example-org")
        text = everything(cm.exception)
        self.assertNotIn("SENTINEL", text.upper())
        self.assertNotIn("cannot be searched", text)
        self.assertNotIn("Validation", text)
        return cm.exception.code

    def test_exact_message_gives_org_not_visible(self):
        self.assertEqual(self.code(raw(not_visible_body())), "org_not_visible")
        other_first = not_visible_body(errors=[{"message": "SENTINEL other"},
                                               {"message": MESSAGE}])
        self.assertEqual(self.code(raw(other_first)), "org_not_visible")

    def test_anything_else_is_bad_query(self):
        cases = [not_visible_body(errors=[{"message": "SENTINEL different"}]),
                 not_visible_body(errors=[{"message": "Prefix " + MESSAGE}]),
                 not_visible_body(errors=[{"message": MESSAGE + " SENTINEL"}]),
                 not_visible_body(errors=[{"message": MESSAGE.upper()}]),
                 not_visible_body(errors=[{"message": MESSAGE[:-1]}]),
                 not_visible_body(errors=[]),
                 not_visible_body(errors={"message": MESSAGE}),
                 not_visible_body(errors=[MESSAGE]),
                 not_visible_body(errors=[{"msg": MESSAGE}]),
                 not_visible_body(errors=[{"message": [MESSAGE]}]),
                 {"message": MESSAGE},
                 {"message": "Validation Failed", "SENTINEL": MESSAGE},
                 [{"message": MESSAGE}], MESSAGE, None, 5]
        for body in cases:
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.code(raw(body)), "bad_query")
        for text in (b"{not json SENTINEL", b"", b"<html>" + MESSAGE.encode() + b"</html>"):
            with self.subTest(text=text[:20]):
                self.assertEqual(self.code(raw(None, text=text)), "bad_query")

    def test_budget_consumed_once(self):
        c = self.client(raw(not_visible_body()))
        with self.assertRaises(ProviderError):
            c.search_repos("examplelib", "example-org")
        self.assertEqual((self.budget.used, len(self.transport.calls)), (1, 1))

    def test_client_not_stopped(self):
        c = self.client(raw(not_visible_body()), raw(ORG, status=200))
        with self.assertRaises(ProviderError) as cm:
            c.search_repos("examplelib", "example-org")
        self.assertEqual(cm.exception.code, "org_not_visible")
        self.assertEqual(c.get_org("example-org")["login"], "example-org")
        self.assertEqual(len(self.transport.calls), 2)

    def test_other_statuses_unchanged(self):
        for status, code in ((404, "not_found"), (500, "server_error"), (400, "unexpected_status")):
            with self.subTest(status=status):
                self.assertEqual(self.code(raw(not_visible_body(), status=status)), code)

    def test_constant_in_the_unverified_block(self):
        src = (ROOT / "src/repo_signals/collector/github_api.py").read_text()
        block = src.split("# ---", 2)[1]
        self.assertIn("ORG_NOT_VISIBLE_MESSAGE = (", block)
        self.assertIn("verify against GitHub's current documentation before first live use",
                      block)


# ---- orchestration and matching treat it as a per-target problem ----

class Found(NamedTuple):
    items: tuple


def repo(login, name):
    return {"name": name, "full_name": f"{login}/{name}",
            "html_url": f"https://{WEB_HOST}/{login}/{name}", "language": "Python",
            "pushed_at": "2026-09-30T12:00:00Z", "fork": False, "archived": False,
            "private": False, "owner": {"login": login, "type": "Organization"}}


def signal(targets):
    text = 'name = "synthetic"\nterms = ["examplelib"]\n'
    for domain, org_login in targets:
        text += f'\n[[targets]]\ndomain = "{domain}"\n'
        if org_login:
            text += f'org = "{org_login}"\n'
    return parse_signal(text)


class FakeClient:
    def __init__(self, orgs, repos, searches=None):
        self.orgs, self.repos, self.searches = orgs, repos, dict(searches or {})
        self.calls = []

    def get_org(self, login):
        self.calls.append(("get_org", login))
        return self.orgs[login]

    def search_orgs(self, name):
        self.calls.append(("search_orgs", name))
        value = self.searches.get(name, ())
        if isinstance(value, BaseException):
            raise value
        return Found(tuple(value))

    def search_repos(self, term, org, language=None, pushed_after=None):
        self.calls.append(("search_repos", org))
        value = self.repos.get(org, [])
        if isinstance(value, BaseException):
            raise value
        return Found(tuple(value))


TARGETS = [("example.com", "example-org"), ("example.invalid", "example-labs")]
ORGS = {"example-org": ORG, "example-labs": {**ORG, "login": "example-labs",
                                             "blog": "https://example.invalid"}}


class Orchestration(unittest.TestCase):
    def test_next_target_proceeds_and_run_is_partial(self):
        client = FakeClient(ORGS, {"example-org": ProviderError("org_not_visible"),
                                   "example-labs": [repo("example-labs", "examplelib-py")]})
        result = collect(signal(TARGETS), client, None, 25, TODAY)
        self.assertEqual([c for c in client.calls if c[0] == "search_repos"],
                         [("search_repos", "example-org"), ("search_repos", "example-labs")])
        self.assertEqual((result.run_report.status, result.run_report.code), (PARTIAL, None))
        self.assertEqual(result.run_report.skipped_searches,
                         (SkippedSearch("example.com", 1, "org_not_visible"),))
        self.assertEqual(len(result.evidence_by_domain["example.invalid"]), 1)
        self.assertIn("search skipped: example.com term 1: org_not_visible", summarize(result))

    def test_end_to_end_through_the_real_client(self):
        def route(url, headers, timeout, max_bytes, method="GET", body=None):
            path = unquote(urlsplit(url).path)
            route.calls.append(path)
            if path.startswith("/orgs/"):
                return raw(ORGS[path[len("/orgs/"):]], status=200)
            q = parse_qs(urlsplit(url).query)["q"][0]
            if "org:example-org" in q.split(" "):
                return raw(not_visible_body())
            return raw({"items": [repo("example-labs", "examplelib-py")], "total_count": 1},
                       status=200)

        route.calls = []
        client = GitHubClient(HttpClient(ALLOWED_HOSTS, transport=route), CallBudget(10),
                              dry_run=False)
        result = collect(signal(TARGETS), client, None, 25, TODAY)
        self.assertEqual(len(route.calls), 4)
        self.assertEqual(result.run_report.skipped_searches,
                         (SkippedSearch("example.com", 1, "org_not_visible"),))
        self.assertEqual(len(result.evidence_by_domain["example.invalid"]), 1)
        for line in summarize(result):
            self.assertNotIn("SENTINEL", line.upper())
            self.assertNotIn("cannot be searched", line)


class Matching(unittest.TestCase):
    def test_per_target_status(self):
        sig = signal([("example.com", None), ("example.invalid", "example-labs")])
        client = FakeClient(ORGS, {}, {"example": ProviderError("org_not_visible")})
        report = match_all(sig, client)
        self.assertEqual([(r.status, r.code) for r in report.results],
                         [(ERROR, "org_not_visible"), (MATCHED, None)])
        self.assertEqual(report.lines[0], "example.com: not matched (org_not_visible)")


class Source(unittest.TestCase):
    def test_forbidden_keyword_absent(self):
        word = re.compile("lam" + "bda", re.IGNORECASE)
        for name in ("src/repo_signals/collector/github_api.py", "tests/test_org_not_visible.py",
                     "docs/github-client.md", "docs/live-collection.md"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))


if __name__ == "__main__":
    unittest.main()
