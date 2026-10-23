"""Collector PR 4 tests: organisation matching and the report. Offline and synthetic only: a
fake client stands in for GitHub, so no HTTP code is involved."""
import ast
import re
import unittest
from pathlib import Path
from typing import NamedTuple

import _src  # noqa: F401
from repo_signals.collector.matching import (AMBIGUOUS, ERROR, MATCHED, MAX_SUGGESTIONS,
                                             NAME_ONLY, NOT_AN_ORG, NOT_FOUND, RATE_LIMITED,
                                             UNMATCHED, USER, VERIFIED_DOMAIN, WEBSITE_MISMATCH,
                                             WEBSITE_ONLY, MatchResult, blog_host, domain_matches,
                                             match_all, match_target, search_name)
from repo_signals.collector.signal import Target, parse_signal
from repo_signals.providers import ProviderError

ROOT = Path(__file__).resolve().parent.parent
LOGIN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


class Found(NamedTuple):
    items: tuple


def org(login, blog=None, verified=None, kind="Organization"):
    return {"login": login, "type": kind, "blog": blog, "is_verified": verified,
            "description": "SENTINELDESC", "name": "SENTINELNAME"}


class FakeClient:
    def __init__(self, orgs=None, searches=None):
        self.orgs = dict(orgs or {})
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
        value = self.searches.get(name, ())
        if isinstance(value, BaseException):
            raise value
        return Found(tuple({"login": x, "type": "Organization", "email": "SENTINEL"}
                           if isinstance(x, str) else x for x in value))


def target(domain="example.com", org_login=None, confirm=False):
    return Target(domain, org_login, confirm)


def no_sentinel(test, text):
    test.assertNotIn("SENTINEL", text.upper())


# ---- the website check ----

class BlogHost(unittest.TestCase):
    def test_accepted_forms(self):
        for blog, host in (("example.com", "example.com"), ("https://example.com", "example.com"),
                           ("http://EXAMPLE.com/about?x=1", "example.com"),
                           (" https://www.example.com/ ", "www.example.com")):
            with self.subTest(blog=blog):
                self.assertEqual(blog_host(blog), host)

    def test_tricks_rejected(self):
        for blog in ("https://user" + "@example.com", "https://user:pw" + "@example.com",
                     "https://example.com:8443", "https://example.com:443",
                     "https://evil.test\\@example.com", "ftp://example.com",
                     "javascript:alert(1)", "https://example.com.", "https://exa mple.com",
                     "https://", "", None, 5, "https://[::1]/", "https://exa\tmple.com"):
            with self.subTest(blog=blog):
                self.assertIsNone(blog_host(blog))

    def test_only_exact_domain_or_www(self):
        self.assertTrue(domain_matches("https://example.com", "example.com"))
        self.assertTrue(domain_matches("www.example.com", "example.com"))
        self.assertTrue(domain_matches("https://WWW.Example.COM/x", "example.com"))
        for blog in ("https://evilexample.com", "https://example.com.evil.test",
                     "https://sub.example.com", "https://www.evilexample.com",
                     "https://wwwexample.com", "https://evil.test/example.com",
                     "https://example.com" + "@evil.test", "https://www.www.example.com",
                     "https://example.invalid"):
            with self.subTest(blog=blog):
                self.assertFalse(domain_matches(blog, "example.com"))

    def test_search_name(self):
        self.assertEqual(search_name("example.com"), "example")
        self.assertEqual(search_name("shop.example.invalid"), "example")


# ---- method A ----

class MethodA(unittest.TestCase):
    def match(self, org_value, confirm=False):
        client = FakeClient({"example-org": org_value})
        return match_target(target(org_login="example-org", confirm=confirm), client), client

    def test_found_with_matching_website(self):
        for blog in ("https://example.com", "www.example.com"):
            with self.subTest(blog=blog):
                r, client = self.match(org("example-org", blog))
                self.assertEqual((r.status, r.confidence, r.org_login), (MATCHED, USER, "example-org"))
                self.assertTrue(r.usable)
                self.assertEqual(client.calls, [("get_org", "example-org")])

    def test_no_website_listed(self):
        for blog in (None, "", "  "):
            with self.subTest(blog=blog):
                r, _ = self.match(org("example-org", blog))
                self.assertEqual((r.status, r.code), (WEBSITE_MISMATCH, "no_website"))
                self.assertFalse(r.usable)

    def test_not_found(self):
        r = match_target(target(org_login="example-org"), FakeClient())
        self.assertEqual((r.status, r.code, r.usable), (NOT_FOUND, "not_found", False))

    def test_not_an_org(self):
        r, _ = self.match(org("example-org", "https://example.com", kind="User"))
        self.assertEqual((r.status, r.usable), (NOT_AN_ORG, False))

    def test_mismatching_website_excluded_unless_confirmed(self):
        for blog in ("https://SENTINEL.example.invalid", "https://evilexample.com",
                     "https://user" + "@example.com", "https://example.com:8443"):
            with self.subTest(blog=blog):
                r, _ = self.match(org("example-org", blog))
                self.assertEqual((r.status, r.confidence, r.usable),
                                 (WEBSITE_MISMATCH, USER, False))
                r, _ = self.match(org("example-org", blog), confirm=True)
                self.assertEqual((r.status, r.note, r.usable), (MATCHED, "confirmed", True))

    def test_confirm_does_not_rescue_a_non_org(self):
        r, _ = self.match(org("example-org", "https://evil.test", kind="User"), confirm=True)
        self.assertEqual(r.status, NOT_AN_ORG)

    def test_bad_login_in_response(self):
        r, _ = self.match({"login": "-SENTINEL", "type": "Organization"})
        self.assertEqual((r.status, r.code), (ERROR, "unexpected_shape"))


class NoWebsite(unittest.TestCase):
    """A user-named org that lists no website is excluded until confirm_org = true."""

    BLOGS = (None, "", "   ", "\t")

    def match(self, blog, confirm):
        client = FakeClient({"example-org": org("example-org", blog)})
        return match_target(target(org_login="example-org", confirm=confirm), client)

    def test_excluded_without_confirm(self):
        for blog in self.BLOGS:
            with self.subTest(blog=blog):
                r = self.match(blog, False)
                self.assertEqual((r.status, r.code, r.confidence, r.org_login),
                                 (WEBSITE_MISMATCH, "no_website", USER, "example-org"))
                self.assertIsNone(r.note)
                self.assertFalse(r.usable)

    def test_usable_when_confirmed(self):
        for blog in self.BLOGS:
            with self.subTest(blog=blog):
                r = self.match(blog, True)
                self.assertEqual((r.status, r.confidence, r.note, r.code),
                                 (MATCHED, USER, "confirmed", None))
                self.assertTrue(r.usable)

    def test_report_line(self):
        from repo_signals.collector.matching import report_line
        line = report_line(self.match(None, False))
        self.assertEqual(line, "example.com -> example-org (user, no website listed: excluded; "
                               "set confirm_org = true to include)")
        self.assertEqual(report_line(self.match("", True)), "example.com -> example-org "
                                                            "(user, confirmed)")

    def test_other_website_cases_unchanged(self):
        r = self.match("https://www.example.com", False)
        self.assertEqual((r.status, r.confidence, r.note, r.usable), (MATCHED, USER, None, True))
        r = self.match("https://SENTINELBLOG.example.invalid", False)
        self.assertEqual((r.status, r.code, r.usable), (WEBSITE_MISMATCH, "website_mismatch",
                                                        False))
        from repo_signals.collector.matching import report_line
        line = report_line(r)
        self.assertEqual(line, "example.com -> example-org (user, website_mismatch: excluded; "
                               "set confirm_org = true to include)")
        no_sentinel(self, line)
        r = self.match("https://SENTINELBLOG.example.invalid", True)
        self.assertEqual((r.status, r.note, r.usable), (MATCHED, "confirmed", True))

    def test_batch_report_has_no_blog_text(self):
        s = signal([("example.com", "example-org", False), ("a.example.com", "example-labs", True),
                    ("b.example.com", "example-hq", False)])
        client = FakeClient({"example-org": org("example-org", None),
                             "example-labs": org("example-labs", "  "),
                             "example-hq": org("example-hq", "https://SENTINELBLOG.example.com")})
        report = match_all(s, client)
        self.assertEqual([r.org_login for r in report.usable], ["example-labs"])
        self.assertEqual(report.counts, {WEBSITE_MISMATCH: 2, MATCHED: 1})
        for line in report.lines:
            no_sentinel(self, line)
            self.assertNotIn("://", line)


# ---- method B ----

class MethodB(unittest.TestCase):
    def match(self, candidates, orgs, domain="example.com"):
        client = FakeClient(orgs, {search_name(domain): candidates})
        return match_target(target(domain), client), client

    def test_exactly_one_verified(self):
        r, client = self.match(["example-org", "example-labs", "example-hq"], {
            "example-org": org("example-org", "https://example.com", True),
            "example-labs": org("example-labs", "https://example.invalid", True),
            "example-hq": org("example-hq", None, None)})
        self.assertEqual((r.status, r.confidence, r.org_login), (MATCHED, VERIFIED_DOMAIN,
                                                                  "example-org"))
        self.assertTrue(r.usable)
        self.assertEqual(client.calls[0], ("search_orgs", "example"))
        self.assertEqual(len(client.calls), 4)

    def test_www_counts(self):
        r, _ = self.match(["example-org"], {"example-org": org("example-org", "www.example.com",
                                                                True)})
        self.assertEqual(r.confidence, VERIFIED_DOMAIN)

    def test_zero_candidates(self):
        r, _ = self.match([], {})
        self.assertEqual((r.status, r.confidence, r.suggestions, r.usable),
                         (UNMATCHED, None, (), False))

    def test_name_only_never_included(self):
        r, _ = self.match(["example-org", "example-labs"], {
            "example-org": org("example-org", "https://example.invalid", True),
            "example-labs": org("example-labs", None, True)})
        self.assertEqual((r.status, r.confidence, r.org_login), (UNMATCHED, NAME_ONLY, None))
        self.assertEqual(r.suggestions, ("example-org", "example-labs"))
        self.assertFalse(r.usable)

    def test_two_verified_is_ambiguous(self):
        r, _ = self.match(["example-org", "example-labs"], {
            "example-org": org("example-org", "https://example.com", True),
            "example-labs": org("example-labs", "https://www.example.com", True)})
        self.assertEqual((r.status, r.org_login, r.suggestions, r.usable),
                         (AMBIGUOUS, None, ("example-org", "example-labs"), False))

    def test_website_only_is_report_only(self):
        for verified in (False, None, "true", 1):
            with self.subTest(verified=verified):
                r, _ = self.match(["example-org"], {
                    "example-org": org("example-org", "https://example.com", verified)})
                self.assertEqual((r.status, r.confidence, r.usable), (MATCHED, WEBSITE_ONLY, False))
        r, _ = self.match(["example-org", "example-labs"], {
            "example-org": org("example-org", "https://example.com", False),
            "example-labs": org("example-labs", "https://example.com", None)})
        self.assertEqual((r.status, r.usable), (AMBIGUOUS, False))

    def test_verified_beats_website_only(self):
        r, _ = self.match(["example-org", "example-labs"], {
            "example-org": org("example-org", "https://example.com", False),
            "example-labs": org("example-labs", "https://example.com", True)})
        self.assertEqual((r.org_login, r.confidence), ("example-labs", VERIFIED_DOMAIN))

    def test_lookalikes_never_match(self):
        blogs = ("https://evilexample.com", "https://example.com.evil.test",
                 "https://sub.example.com", "https://user" + "@example.com",
                 "https://example.com:8443", "https://evil.test/example.com")
        for blog in blogs:
            with self.subTest(blog=blog):
                r, _ = self.match(["example-org"], {"example-org": org("example-org", blog, True)})
                self.assertEqual((r.status, r.usable), (UNMATCHED, False))

    def test_candidates_filtered_capped_and_shaped(self):
        items = [{"login": "a-user", "type": "User"}, {"login": "-bad", "type": "Organization"},
                 "org1", "org1", "org2", "org3", "org4", "org5"]
        r, client = self.match(items, {})
        self.assertEqual([c for c in client.calls if c[0] == "get_org"],
                         [("get_org", "org1"), ("get_org", "org2"), ("get_org", "org3")])
        self.assertEqual(r.suggestions, ("org1", "org2", "org3"))
        self.assertLessEqual(len(r.suggestions), MAX_SUGGESTIONS)
        for s in r.suggestions:
            self.assertRegex(s, LOGIN_RE)

    def test_ambiguous_suggestions_capped(self):
        many = {f"org{i}": org(f"org{i}", "https://example.com", True) for i in range(5)}
        r, _ = self.match([f"org{i}" for i in range(5)], many)
        self.assertEqual(r.status, AMBIGUOUS)
        self.assertLessEqual(len(r.suggestions), MAX_SUGGESTIONS)

    def test_candidate_errors(self):
        r, _ = self.match(["org1", "org2"], {
            "org1": ProviderError("server_error"),
            "org2": org("org2", "https://example.com", True)})
        self.assertEqual((r.status, r.org_login), (MATCHED, "org2"))
        r, client = self.match(["org1", "org2"], {"org1": ProviderError("rate_limited"),
                                                  "org2": org("org2", "https://example.com",
                                                              True)})
        self.assertEqual((r.status, r.code), (RATE_LIMITED, "rate_limited"))
        self.assertNotIn(("get_org", "org2"), client.calls)
        r, _ = self.match(["org1"], {"org1": org("org9", "https://example.com", True)})
        self.assertEqual(r.status, UNMATCHED)

    def test_search_errors(self):
        client = FakeClient(searches={"example": ProviderError("server_error")})
        r = match_target(target(), client)
        self.assertEqual((r.status, r.code), (ERROR, "server_error"))
        client = FakeClient(searches={"example": ProviderError("rate_limited")})
        self.assertEqual(match_target(target(), client).status, RATE_LIMITED)


# ---- the batch and the report ----

def signal(targets):
    text = 'name = "synthetic"\nterms = ["examplelib"]\n'
    for domain, org_login, confirm in targets:
        text += f'\n[[targets]]\ndomain = "{domain}"\n'
        if org_login:
            text += f'org = "{org_login}"\n'
        if confirm:
            text += "confirm_org = true\n"
    return parse_signal(text)


class Batch(unittest.TestCase):
    def test_report_lines_and_counts(self):
        s = signal([("example.com", "example-org", False), ("a.example.com", "example-labs", False),
                    ("b.example.com", "example-hq", True), ("c.example.com", "example-user", False),
                    ("d.example.com", "example-gone", False), ("shop.example.invalid", None, False)])
        client = FakeClient({
            "example-org": org("example-org", "https://example.com"),
            "example-labs": org("example-labs", "https://SENTINELBLOG.example.invalid"),
            "example-hq": org("example-hq", "https://SENTINELBLOG.example.invalid"),
            "example-user": org("example-user", None, kind="User"),
            "cand1": org("cand1", "https://shop.example.invalid", True),
            "cand2": org("cand2", "https://shop.example.invalid", True)},
            {"example": ["cand1", "cand2"]})
        report = match_all(s, client)
        self.assertEqual(report.lines, (
            "example.com -> example-org (user)",
            "a.example.com -> example-labs (user, website_mismatch: excluded; set confirm_org = "
            "true to include)",
            "b.example.com -> example-hq (user, confirmed)",
            "c.example.com: example-user is not an organisation",
            "d.example.com: org example-gone not found",
            'shop.example.invalid: ambiguous (2 candidate orgs: cand1, cand2; add org = "..." to '
            "confirm)"))
        self.assertEqual(report.counts, {MATCHED: 2, WEBSITE_MISMATCH: 1, NOT_AN_ORG: 1,
                                         NOT_FOUND: 1, AMBIGUOUS: 1})
        self.assertEqual([r.org_login for r in report.usable], ["example-org", "example-hq"])
        for line in report.lines:
            no_sentinel(self, line)

    def test_other_report_lines(self):
        cases = [(MatchResult("example.com", "example-org", VERIFIED_DOMAIN, MATCHED),
                  "example.com -> example-org (verified-domain)"),
                 (MatchResult("example.com", "example-org", WEBSITE_ONLY, MATCHED),
                  'example.com -> example-org (website-only: not included; add org = '
                  '"example-org" to confirm)'),
                 (MatchResult("example.com", "example-org", USER, MATCHED, note="no_website"),
                  "example.com -> example-org (user, no website listed)"),
                 (MatchResult("example.com", None, NAME_ONLY, UNMATCHED, ("a", "b")),
                  "example.com: unmatched (no organisation passed the domain check); "
                  "suggestions: a, b"),
                 (MatchResult("example.com", None, None, UNMATCHED),
                  "example.com: unmatched (no organisation passed the domain check)"),
                 (MatchResult("example.com", None, None, ERROR, code="server_error"),
                  "example.com: not matched (server_error)")]
        from repo_signals.collector.matching import report_line
        for result, line in cases:
            with self.subTest(line=line):
                self.assertEqual(report_line(result), line)

    def test_rate_limit_stops_all_later_targets(self):
        s = signal([("example.com", "org1", False), ("a.example.com", "org2", False),
                    ("b.example.com", "org3", False), ("c.example.com", None, False)])
        client = FakeClient({"org1": org("org1", "https://example.com"),
                             "org2": ProviderError("rate_limited"),
                             "org3": org("org3", "https://b.example.com")})
        report = match_all(s, client)
        self.assertEqual([r.status for r in report.results],
                         [MATCHED, RATE_LIMITED, RATE_LIMITED, RATE_LIMITED])
        self.assertEqual(client.calls, [("get_org", "org1"), ("get_org", "org2")])
        self.assertEqual(report.lines[1], "a.example.com: not matched (rate_limited)")
        self.assertEqual(report.lines[2],
                         "b.example.com: not attempted (rate limited earlier in this run)")
        self.assertEqual(report.counts, {MATCHED: 1, RATE_LIMITED: 3})

    def test_other_errors_do_not_abort_the_batch(self):
        s = signal([("example.com", "org1", False), ("a.example.com", "org2", False),
                    ("b.example.com", "org3", False), ("c.example.com", "org4", False)])
        client = FakeClient({"org1": ProviderError("server_error"),
                             "org2": RuntimeError("SENTINEL https://example.com/x"),
                             "org3": ValueError("SENTINEL"),
                             "org4": org("org4", "https://c.example.com")})
        report = match_all(s, client)
        self.assertEqual([(r.status, r.code) for r in report.results],
                         [(ERROR, "server_error"), (ERROR, "client_error"),
                          (ERROR, "client_error"), (MATCHED, None)])
        self.assertEqual(len(client.calls), 4)
        for line in report.lines:
            no_sentinel(self, line)
            self.assertNotIn("://", line)

    def test_needs_a_signal(self):
        with self.assertRaises(TypeError):
            match_all("signal", FakeClient())


class Source(unittest.TestCase):
    def test_no_http_machinery_imported(self):
        tree = ast.parse((ROOT / "src/repo_signals/collector/matching.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module or "", ("github_api", "http"))
                self.assertFalse((node.module or "").startswith(("http", "urllib.request")))
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotIn(a.name.split(".")[0], ("http", "socket", "ssl"))


if __name__ == "__main__":
    unittest.main()
