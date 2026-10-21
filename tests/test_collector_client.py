"""Collector PR 3 tests: the GitHub client. Offline and synthetic only: a fake transport behind
http.HttpClient stands in for GitHub, and every response is made up in the ASSUMED shape."""
import io
import json
import os
import re
import subprocess
import sys
import traceback
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import _src  # noqa: F401
from repo_signals import __version__
from repo_signals.budget import CallBudget
from repo_signals.collector import github_api as api
from repo_signals.collector.github_api import (ALLOWED_HOSTS, API_BASE, API_HOST, GitHubClient,
                                               RateLimit, RateLimitedError, SearchResult)
from repo_signals.collector.github_fields import WEB_HOST, parse_repo
from repo_signals.http import HttpClient, RawResponse
from repo_signals.providers import ProviderError

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "SENTINELTOKEN" + "x" * 24
EMAIL = "sentinel.person" + "@example.invalid"
RATE = {"X-RateLimit-Remaining": "58", "X-RateLimit-Reset": "1767225600"}


def ok(data, status=200, headers=None):
    return RawResponse(status, dict(RATE if headers is None else headers),
                       json.dumps(data).encode())


def repo_item(name="widget", owner="example-org", **over):
    item = {"id": 1, "name": name, "full_name": f"{owner}/{name}",
            "html_url": f"https://{WEB_HOST}/{owner}/{name}", "language": "Python",
            "pushed_at": "2026-09-30T12:00:00Z", "fork": False, "archived": False,
            "private": False, "description": "SENTINELDESC", "topics": ["SENTINELTOPIC"],
            "owner": {"login": owner, "type": "Organization", "name": "SENTINELOWNERNAME",
                      "email": EMAIL},
            "contributors": [{"login": "SENTINELCONTRIB"}], "score": 1.0}
    item.update(over)
    return item


def repos(n=1, total=None, **over):
    return {"total_count": n if total is None else total, "incomplete_results": False,
            "items": [repo_item(name=f"widget{i}", **over) for i in range(n)]}


ORG = {"login": "example-org", "type": "Organization", "blog": "https://example.com",
       "is_verified": True, "name": "SENTINELORGNAME", "email": EMAIL,
       "description": "SENTINELDESC", "members": [{"login": "SENTINELMEMBER"}]}


class FakeTransport:
    def __init__(self, *responses, on_call=None):
        self.responses = list(responses)
        self.calls = []
        self.on_call = on_call

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        self.calls.append({"url": url, "headers": dict(headers), "method": method, "body": body})
        if self.on_call:
            self.on_call()
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r


def everything(e):
    return "".join(traceback.format_exception(e)) + repr(e) + str(e.args)


class Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("time.sleep", side_effect=AssertionError("slept"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, *responses, budget=None, token=TOKEN, on_call=None):
        self.transport = FakeTransport(*(responses or (ok(ORG),)), on_call=on_call)
        self.budget = budget if budget is not None else CallBudget(20)
        http = HttpClient(ALLOWED_HOSTS, transport=self.transport)
        return GitHubClient(http, self.budget, token, dry_run=False)

    def raises(self, code, fn, *args):
        with self.assertRaises(ProviderError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.code, code)
        text = everything(cm.exception)
        self.assertNotIn("SENTINEL", text.upper())
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("://", text)
        self.assertNotIn("example-org", text)
        return cm.exception

    def query(self, n=0):
        return parse_qs(urlsplit(self.transport.calls[n]["url"]).query)


# ---- budget, token, headers ----

class Budget(Case):
    def test_charged_before_each_call(self):
        seen = []
        budget = CallBudget(5)

        def record():
            seen.append(budget.used)

        c = self.client(ok(ORG), ok({"items": []}), ok(repos()), budget=budget, on_call=record)
        c.get_org("example-org")
        c.search_orgs("example")
        c.search_repos("examplelib", "example-org")
        self.assertEqual(seen, [1, 2, 3])

    def test_over_the_cap_never_reaches_the_transport(self):
        c = self.client(budget=CallBudget(1))
        c.get_org("example-org")
        self.raises("budget_exhausted", c.get_org, "example-org")
        self.raises("budget_exhausted", c.search_repos, "examplelib", "example-org")
        self.assertEqual(len(self.transport.calls), 1)


class TokenAndHeaders(Case):
    def test_token_only_in_the_authorization_header(self):
        c = self.client(ok(ORG), ok(repos()))
        c.get_org("example-org")
        c.search_repos("examplelib", "example-org")
        for call in self.transport.calls:
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertNotIn(TOKEN, call["url"])
            self.assertNotIn("SENTINEL", call["url"])
            self.assertIsNone(call["body"])
            self.assertEqual(call["method"], "GET")
            others = {k: v for k, v in call["headers"].items() if k != "Authorization"}
            self.assertNotIn(TOKEN, repr(others))
        self.assertNotIn(TOKEN, repr(c) + str(c) + repr(vars(c).get("rate_limit")))
        self.raises("not_found", self.client(ok({}, status=404)).get_org, "example-org")

    def test_no_token_no_authorization_header(self):
        c = self.client(token=None)
        c.get_org("example-org")
        self.assertNotIn("Authorization", self.transport.calls[0]["headers"])

    def test_bad_token_refused_without_echo(self):
        for bad in ("SENTINELshort", "SENTINEL with spaces and more text", "SENTINEL\n" * 5, 5):
            with self.subTest(n=len(str(bad))), self.assertRaises(ValueError) as cm:
                GitHubClient(token=bad)
            self.assertNotIn("SENTINEL", str(cm.exception))

    def test_user_agent_version_and_accept(self):
        c = self.client()
        c.get_org("example-org")
        headers = self.transport.calls[0]["headers"]
        self.assertEqual(headers["User-Agent"],
                         f"repo-signals/{__version__} (+https://honeycombagents.ai)")
        self.assertEqual(headers["X-GitHub-Api-Version"], api.API_VERSION)
        self.assertEqual(headers["Accept"], api.MEDIA_TYPE)
        self.assertRegex(api.API_VERSION, r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


# ---- validation and query building ----

class Validation(Case):
    def test_bad_input_rejected_before_any_request(self):
        c = self.client()
        cases = [(c.get_org, ("-SENTINEL",), "bad_org"), (c.get_org, ("x" * 40,), "bad_org"),
                 (c.get_org, ("SENTINEL org",), "bad_org"), (c.get_org, ("a/b",), "bad_org"),
                 (c.get_org, ("../SENTINEL",), "bad_org"), (c.get_org, (None,), "bad_org"),
                 (c.search_orgs, ("SENTINEL type:user",), "bad_name"),
                 (c.search_orgs, ("",), "bad_name"),
                 (c.search_repos, ("a", "example-org"), "bad_term"),
                 (c.search_repos, ("a" * 61, "example-org"), "bad_term"),
                 (c.search_repos, ("SENTINEL term", "example-org"), "bad_term"),
                 (c.search_repos, ("SENTINEL:org", "example-org"), "bad_term"),
                 (c.search_repos, ("-SENTINEL", "example-org"), "bad_term"),
                 (c.search_repos, (None, "example-org"), "bad_term"),
                 (c.search_repos, ("examplelib", "SENTINEL org"), "bad_org"),
                 (c.search_repos, ("examplelib", None), "bad_org"),
                 (c.search_repos, ("examplelib", "example-org", "SENTINEL lang"), "bad_language"),
                 (c.search_repos, ("examplelib", "example-org", "x" * 33), "bad_language"),
                 (c.search_repos, ("examplelib", "example-org", None, "2026/01/01"), "bad_date"),
                 (c.search_repos, ("examplelib", "example-org", None, "2026-02-30"), "bad_date"),
                 (c.search_repos, ("examplelib", "example-org", None, 20260101), "bad_date"),
                 (c.search_repos, ("examplelib", "example-org", None,
                                   datetime(2026, 1, 1)), "bad_date")]
        for fn, args, code in cases:
            with self.subTest(fn=fn.__name__, args=args):
                self.raises(code, fn, *args)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.budget.used, 0)

    def test_queries_go_through_params(self):
        c = self.client(ok(ORG), ok({"items": []}), ok(repos()))
        c.get_org("example-org")
        c.search_orgs("example")
        c.search_repos("c++lib", "example-org", "C#", date(2025, 10, 21))
        urls = [call["url"] for call in self.transport.calls]
        self.assertEqual(urls[0], API_BASE + "/orgs/example-org")
        self.assertEqual(urlsplit(urls[1])._replace(query="").geturl(),
                         API_BASE + api.SEARCH_ORGS_PATH)
        self.assertEqual(self.query(1), {"q": ["example type:org"], "per_page": ["3"]})
        self.assertEqual(urlsplit(urls[2])._replace(query="").geturl(),
                         API_BASE + api.SEARCH_REPOS_PATH)
        self.assertEqual(self.query(2), {
            "q": ["c++lib in:name,description,topics org:example-org fork:false archived:false "
                  "language:C# pushed:>=2025-10-21"],
            "per_page": ["5"]})
        for url in urls:
            self.assertEqual(urlsplit(url).netloc, API_HOST)
            self.assertNotIn(" ", url)
            self.assertNotIn("#", url)

    def test_pushed_after_accepts_a_date_or_a_string(self):
        c = self.client(ok(repos()))
        c.search_repos("examplelib", "example-org", None, "2025-01-31")
        self.assertIn("pushed:>=2025-01-31", self.query()["q"][0])

    def test_forks_and_archived_excluded_in_every_search(self):
        combos = [(None, None), ("Python", None), (None, "2025-01-01"), ("Go", date(2025, 6, 1))]
        c = self.client(ok(repos()))
        for language, pushed in combos:
            c.search_repos("examplelib", "example-org", language, pushed)
        for n in range(len(combos)):
            q = self.query(n)["q"][0].split(" ")
            self.assertIn("fork:false", q)
            self.assertIn("archived:false", q)
            self.assertEqual(q[0], "examplelib")

    def test_per_target_cap_and_no_pagination(self):
        link = {**RATE, "Link": '<https://' + API_HOST + '/search/repositories?page=2>; rel="next"'}
        c = self.client(ok(repos(8, total=1234), headers=link))
        result = c.search_repos("examplelib", "example-org")
        self.assertEqual(len(result.items), 5)
        self.assertEqual((result.total_count, result.incomplete), (1234, False))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertNotIn("page", self.query())
        self.assertEqual(self.query()["per_page"], ["5"])
        c = self.client(ok({"items": [{"login": f"org{i}", "type": "Organization"}
                                      for i in range(9)], "total_count": 9}))
        self.assertEqual(len(c.search_orgs("example").items), 3)
        self.assertEqual(len(self.transport.calls), 1)


# ---- rate limits ----

class RateLimits(Case):
    def test_headers_read_as_integers_only(self):
        c = self.client()
        c.get_org("example-org")
        self.assertEqual(c.rate_limit, RateLimit(58, 1767225600))
        for headers, want in (({}, RateLimit(None, None)),
                              ({"X-RateLimit-Remaining": "SENTINEL", "X-RateLimit-Reset": "1.5"},
                               RateLimit(None, None)),
                              ({"X-RateLimit-Remaining": " 7 "}, RateLimit(7, None))):
            with self.subTest(headers=headers):
                c = self.client(ok(ORG, headers=headers))
                c.get_org("example-org")
                self.assertEqual(c.rate_limit, want)
                self.assertNotIn("SENTINEL", repr(c.rate_limit))

    def test_403_and_429_stop_all_further_calls(self):
        for status in (403, 429):
            with self.subTest(status=status):
                c = self.client(ok({"message": "SENTINEL"}, status=status,
                                   headers={**RATE, "Retry-After": "30"}), ok(ORG))
                e = self.raises("rate_limited", c.get_org, "example-org")
                self.assertIsInstance(e, RateLimitedError)
                self.assertEqual(e.reset, 1767225600)
                self.assertEqual(str(e), "rate_limited: reset 1767225600")
                self.raises("rate_limited", c.get_org, "example-org")
                self.raises("rate_limited", c.search_repos, "examplelib", "example-org")
                self.raises("rate_limited", c.search_orgs, "example")
                self.assertEqual(len(self.transport.calls), 1)
                self.assertEqual(self.budget.used, 1)

    def test_remaining_zero_stops_the_next_call(self):
        zero = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "99"}
        c = self.client(ok(ORG, headers=zero), ok(ORG))
        self.assertEqual(c.get_org("example-org")["login"], "example-org")
        e = self.raises("rate_limited", c.get_org, "example-org")
        self.assertEqual(e.reset, 99)
        self.assertEqual(len(self.transport.calls), 1)

    def test_rate_limited_without_reset(self):
        c = self.client(ok({}, status=429, headers={}))
        e = self.raises("rate_limited", c.get_org, "example-org")
        self.assertIsNone(e.reset)
        self.assertEqual(str(e), "rate_limited")


# ---- errors ----

class Errors(Case):
    def test_status_mapping(self):
        body = {"message": "SENTINEL example-org " + TOKEN, "documentation_url": "https://x"}
        for status, code in ((404, "not_found"), (422, "bad_query"), (500, "server_error"),
                             (502, "server_error"), (599, "server_error"),
                             (418, "unexpected_status"), (400, "unexpected_status")):
            with self.subTest(status=status):
                c = self.client(ok(body, status=status))
                self.raises(code, c.get_org, "example-org")
                self.assertEqual(len(self.transport.calls), 1)  # never retried

    def test_token_rejected_stops_the_client(self):
        c = self.client(ok({}, status=401), ok(ORG))
        self.raises("token_rejected", c.get_org, "example-org")
        self.raises("token_rejected", c.get_org, "example-org")
        self.assertEqual(len(self.transport.calls), 1)

    def test_transport_and_shape_errors(self):
        cases = [(ProviderError("timeout"), "timeout"),
                 (OSError("https://" + API_HOST + "/orgs/example-org " + TOKEN), "network_error"),
                 (RawResponse(200, {}, b"{SENTINEL"), "malformed_json"),
                 (RawResponse(301, {"Location": "https://example.com/"}, b""), "redirect_refused"),
                 (ok(["SENTINEL"]), "unexpected_shape"),
                 (ok({"login": "-bad", "type": "Organization"}), "unexpected_shape"),
                 (ok({"type": "Organization"}), "unexpected_shape"),
                 (ok({**ORG, "is_verified": "yes"}), "unexpected_shape")]
        for response, code in cases:
            with self.subTest(code=code):
                self.raises(code, self.client(response).get_org, "example-org")
        for data in ({}, {"items": "SENTINEL"}, ["x"]):
            with self.subTest(data=data):
                self.raises("unexpected_shape", self.client(ok(data)).search_repos, "examplelib",
                            "example-org")


# ---- allowlisted parsing ----

class Allowlists(Case):
    def test_org_profile(self):
        org = self.client(ok(ORG)).get_org("example-org")
        self.assertEqual(org, {"login": "example-org", "type": "Organization",
                               "blog": "https://example.com", "is_verified": True})
        sparse = self.client(ok({"login": "example-org", "type": "Organization"}))
        self.assertEqual(sparse.get_org("example-org")["blog"], None)

    def test_repo_items_keep_only_allowlisted_keys(self):
        result = self.client(ok(repos(3))).search_repos("examplelib", "example-org")
        self.assertIsInstance(result, SearchResult)
        for item in result.items:
            self.assertEqual(set(item), set(api.REPO_KEYS))
            self.assertEqual(set(item["owner"]), {"login", "type"})
            self.assertIsNotNone(parse_repo(item))
        text = repr(result)
        self.assertNotIn("SENTINEL", text.upper())
        self.assertNotIn(EMAIL, text)

    def test_malformed_items_skipped(self):
        data = {"items": [repo_item(), "SENTINEL", {"name": "x"}, repo_item(owner_x=1),
                          {**repo_item(), "owner": "SENTINEL"},
                          {**repo_item(), "name": {"nested": "SENTINEL"}},
                          {**repo_item(), "owner": {"login": 5, "type": "Organization"}}]}
        result = self.client(ok(data)).search_repos("examplelib", "example-org")
        self.assertEqual(len(result.items), 2)
        self.assertNotIn("SENTINEL", repr(result).upper())

    def test_candidates(self):
        data = {"items": [{"login": "example-org", "type": "Organization", "email": EMAIL,
                           "name": "SENTINEL"}, {"login": "-bad"}, "x"], "total_count": 3}
        result = self.client(ok(data)).search_orgs("example")
        self.assertEqual(result.items, ({"login": "example-org", "type": "Organization"},))


# ---- dry run and reachability ----

class DryRun(unittest.TestCase):
    def test_default_dry_run_builds_nothing_and_calls_nothing(self):
        with mock.patch.object(api, "HttpClient", side_effect=AssertionError("built")):
            c = GitHubClient(token=TOKEN)
        self.assertTrue(c.dry_run)
        self.assertIsNone(c._http)
        t = FakeTransport(ok(ORG))
        budget = CallBudget(5)
        c = GitHubClient(HttpClient(ALLOWED_HOSTS, transport=t), budget, TOKEN)
        for fn, args in ((c.get_org, ("example-org",)), (c.search_orgs, ("example",)),
                         (c.search_repos, ("examplelib", "example-org"))):
            with self.assertRaises(ProviderError) as cm:
                fn(*args)
            self.assertEqual(cm.exception.code, "dry_run")
        self.assertEqual((t.calls, budget.used), ([], 0))

    def test_live_construction_rules(self):
        with self.assertRaises(ValueError):
            GitHubClient(dry_run=False)
        with self.assertRaises(ValueError):
            GitHubClient(HttpClient(("example.com",)), CallBudget(1), dry_run=False)
        with self.assertRaises(ValueError):
            GitHubClient(HttpClient((API_HOST, "example.com")), CallBudget(1), dry_run=False)
        with self.assertRaises(TypeError):
            GitHubClient(dry_run="no")
        with self.assertRaises(ValueError):
            GitHubClient(user_agent="bad\nagent")
        self.assertEqual(ALLOWED_HOSTS, frozenset({API_HOST}))
        live = GitHubClient(budget=CallBudget(1), dry_run=False)
        self.assertEqual(live._http.allowed_hosts, frozenset({API_HOST}))


class Surface(unittest.TestCase):
    def test_three_methods_and_nothing_else(self):
        public = {n for n in dir(GitHubClient) if not n.startswith("_")}
        self.assertEqual(public, {"get_org", "search_orgs", "search_repos"})
        src = (ROOT / "src/repo_signals/collector/github_api.py").read_text()
        for bad in ("/search/code", "/repos/", "://raw", "/contents", "/commits",
                    "/contributors", "/members", "/html", "page=", '"page"', "text-match",
                    "import time", "time.sleep", "while "):
            self.assertFalse(bad in src, bad)
        self.assertIn("verify against GitHub's current documentation before first live use", src)
        self.assertEqual(src.count("# ---"), 2)

    def test_not_reachable_from_the_cli(self):
        for name in ("src/repo_signals/cli.py", "src/repo_signals/collector/dry_run.py",
                     "src/repo_signals/doctor.py"):
            text = (ROOT / name).read_text()
            self.assertNotIn("github_api", text, name)
            self.assertNotIn("GitHubClient", text, name)
        users = [p.name for p in (ROOT / "src").rglob("*.py")
                 if "GitHubClient" in p.read_text() or "github_api" in p.read_text()]
        self.assertEqual(users, ["github_api.py"])
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import repo_signals.cli;"
                "print('repo_signals.collector.github_api' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code, str(_src.SRC)],
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)

    def test_forbidden_keyword_absent(self):
        word = re.compile("lam" + "bda", re.IGNORECASE)
        for name in ("src/repo_signals/collector/github_api.py", "tests/test_collector_client.py",
                     "docs/github-client.md", "docs/collector-design.md"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))


if __name__ == "__main__":
    unittest.main()
