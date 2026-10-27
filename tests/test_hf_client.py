"""Build step 3 of the Hugging Face source: the client behind a fake transport. No real socket is
ever opened, and the client is not reachable from any command."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import _src
from repo_signals.budget import CallBudget
from repo_signals.collector import hf_api
from repo_signals.collector.hf_api import (API_BASE, API_HOST, LISTING_CAPPED, HuggingFaceClient,
                                           Overview, next_cursor, parse_rate_limit)
from repo_signals.http import HttpClient, RawResponse
from repo_signals.providers.base import ProviderError

ROOT = Path(__file__).resolve().parent.parent
RATE = {"RateLimit": '"api";r=475;t=68', "RateLimit-Policy": '"fixed window";"api";q=500;w=300'}
OVERVIEW = {"_id": "SENTINELID", "name": "example-org", "fullname": "Example Org",
            "details": "SENTINELBIO visit www.example.com",
            "avatarUrl": "https://example.invalid/a",
            "isVerified": True, "plan": "team", "numUsers": 3, "numModels": 18, "numDatasets": 48,
            "numSpaces": 47, "numFollowers": 9}


def raw(data, status=200, headers=None):
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    return RawResponse(status, dict(RATE if headers is None else headers), body)


def link(cursor, path="/api/models", host=API_HOST, extra=""):
    return f'<https://{host}{path}?author=example-org&limit=500&cursor={cursor}{extra}>; rel="next"'


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


def live(*responses, budget=None, on_call=None):
    t = FakeTransport(*(responses or (raw([]),)), on_call=on_call)
    b = budget if budget is not None else CallBudget(10)
    c = HuggingFaceClient(HttpClient((API_HOST,), transport=t), b, dry_run=False)
    return c, t, b


def query(call):
    return parse_qs(urlsplit(call["url"]).query)


class Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("socket.socket", side_effect=AssertionError("a real socket"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def raises(self, code, fn, *args, **kwargs):
        with self.assertRaises(ProviderError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.code, code)
        self.assertNotIn("SENTINEL", repr(cm.exception) + str(cm.exception))
        return cm.exception


class DryRunAndBudget(Case):
    def test_dry_run_by_default_requests_nothing(self):
        c = HuggingFaceClient()
        self.assertTrue(c.dry_run)
        self.raises("dry_run", c.get_org_overview, "example-org")
        self.raises("dry_run", c.list_repos, "model", "example-org")
        self.assertIsNone(c._http)

    def test_charged_before_sending_and_over_cap_never_sent(self):
        seen = []
        budget = CallBudget(1)
        c, t, _ = live(raw(OVERVIEW), budget=budget, on_call=lambda: seen.append(budget.used))
        c.get_org_overview("example-org")
        self.assertEqual(seen, [1])
        self.raises("budget_exhausted", c.list_repos, "model", "example-org")
        self.assertEqual(len(t.calls), 1)

    def test_live_needs_a_budget_and_the_one_host(self):
        with self.assertRaises(ValueError):
            HuggingFaceClient(HttpClient((API_HOST,)), None, dry_run=False)
        with self.assertRaises(ValueError):
            HuggingFaceClient(HttpClient(("example.com",)), CallBudget(1), dry_run=False)
        self.assertEqual(hf_api.ALLOWED_HOSTS, frozenset({"huggingface.co"}))


class OverviewAndListing(Case):
    def test_overview_keeps_only_its_allowlist(self):
        c, t, _ = live(raw(OVERVIEW))
        overview = c.get_org_overview("example-org")
        self.assertEqual(overview, Overview("example-org", True, 18, 48, 47))
        self.assertNotIn("SENTINEL", repr(overview))
        call = t.calls[0]
        self.assertEqual(call["url"], API_BASE + "/api/organizations/example-org/overview")
        self.assertEqual(call["method"], "GET")
        self.assertNotIn("Authorization", call["headers"])

    def test_malformed_overview_is_unexpected_shape(self):
        for bad in ({**OVERVIEW, "isVerified": "yes"}, {**OVERVIEW, "numModels": -1},
                    {**OVERVIEW, "numSpaces": True}, {**OVERVIEW, "name": "bad name"},
                    {k: v for k, v in OVERVIEW.items() if k != "numDatasets"}, [OVERVIEW]):
            with self.subTest(bad=str(bad)[:40]):
                c, _, _ = live(raw(bad))
                self.raises("unexpected_shape", c.get_org_overview, "example-org")

    def test_listing_request_parameters_and_page_size_cap(self):
        c, t, _ = live(raw([{"id": "example-org/examplelib", "description": "SENTINEL"}]))
        page = c.list_repos("dataset", "example-org", ("author", "lastModified"), limit=1000)
        self.assertEqual(page.records,
                         ({"id": "example-org/examplelib", "description": "SENTINEL"},))
        self.assertIsNone(page.next_cursor)
        call = t.calls[0]
        self.assertTrue(call["url"].startswith(API_BASE + "/api/datasets?"))
        self.assertEqual(query(call), {"author": ["example-org"], "sort": ["lastModified"],
                                       "direction": ["-1"], "limit": ["500"],
                                       "expand": ["author", "lastModified"]})
        self.assertEqual(call["method"], "GET")
        for kind, path in (("model", "/api/models"), ("space", "/api/spaces")):
            c, t, _ = live(raw([]))
            c.list_repos(kind, "example-org")
            self.assertTrue(t.calls[0]["url"].startswith(API_BASE + path + "?"))

    def test_bad_arguments_never_reach_the_transport(self):
        c, t, _ = live(raw([]))
        for code, kwargs in (("bad_kind", {"kind": "weights", "org": "example-org"}),
                             ("bad_org", {"kind": "model", "org": "example.com"}),
                             ("bad_expand", {"kind": "model", "org": "example-org",
                                             "expand": ("cardData",)}),
                             ("bad_expand", {"kind": "model", "org": "example-org",
                                             "expand": ["author"]}),
                             ("bad_limit", {"kind": "model", "org": "example-org", "limit": 0}),
                             ("bad_cursor", {"kind": "model", "org": "example-org",
                                             "cursor": "a b"})):
            with self.subTest(code=code, kwargs=kwargs):
                self.raises(code, c.list_repos, **kwargs)
        self.raises("bad_org", c.get_org_overview, "../example-org")
        self.assertEqual(t.calls, [])


class Errors(Case):
    def test_status_codes(self):
        cases = [(lambda c: c.get_org_overview("example-org"), raw({"error": "x"}, 404),
                  "org_not_found"),
                 (lambda c: c.list_repos("model", "example-org"), raw({"error": "x"}, 404),
                  "not_found"),
                 (lambda c: c.list_repos("model", "example-org"), raw({}, 401), "unauthorised"),
                 (lambda c: c.list_repos("model", "example-org"), raw({}, 429), "rate_limited"),
                 (lambda c: c.list_repos("model", "example-org"), raw(b"<html>", 503),
                  "server_error"),
                 (lambda c: c.list_repos("model", "example-org"), raw({}, 400), "bad_query"),
                 (lambda c: c.list_repos("model", "example-org"), raw({}, 418),
                  "unexpected_status"),
                 (lambda c: c.list_repos("model", "example-org"), raw(b"not json"),
                  "malformed_json"),
                 (lambda c: c.list_repos("model", "example-org"), raw({"items": []}),
                  "unexpected_shape"),
                 (lambda c: c.list_repos("model", "example-org"),
                  raw(b"", 302, {"Location": "https://example.com/"}), "redirect_refused")]
        for call, response, code in cases:
            with self.subTest(code=code):
                c, _, _ = live(response)
                self.raises(code, call, c)

    def test_sticky_stop_after_401_and_429(self):
        for status, code in ((401, "unauthorised"), (429, "rate_limited")):
            with self.subTest(status=status):
                c, t, _ = live(raw({}, status), raw(OVERVIEW))
                self.raises(code, c.list_repos, "model", "example-org")
                self.raises(code, c.get_org_overview, "example-org")
                self.assertEqual(len(t.calls), 1)

    def test_429_carries_the_reset_seconds(self):
        c, _, _ = live(raw({}, 429, {"RateLimit": '"api";r=0;t=42'}))
        e = self.raises("rate_limited", c.list_repos, "model", "example-org")
        self.assertEqual(e.reset, 42)

    def test_remaining_zero_blocks_the_next_request(self):
        c, t, _ = live(raw(OVERVIEW, headers={"RateLimit": '"api";r=0;t=30'}), raw([]))
        self.assertEqual(c.get_org_overview("example-org").name, "example-org")
        self.assertEqual(c.rate_limit, hf_api.RateLimit(0, 30))
        e = self.raises("rate_limited", c.list_repos, "model", "example-org")
        self.assertEqual(e.reset, 30)
        self.assertEqual(len(t.calls), 1)

    def test_rate_limit_header_parsing(self):
        self.assertEqual(parse_rate_limit({"ratelimit": '"api";r=475;t=68'}),
                         hf_api.RateLimit(475, 68))
        self.assertEqual(parse_rate_limit({}), hf_api.RateLimit(None, None))
        self.assertEqual(parse_rate_limit({"ratelimit": "SENTINEL"}), hf_api.RateLimit(None, None))


class Pagination(Case):
    def test_cursor_is_parsed_and_sent_back_never_the_url(self):
        c, t, _ = live(raw([{"id": "example-org/a"}], headers={**RATE, "Link": link("Q2F0YQ==")}),
                       raw([{"id": "example-org/b"}]))
        first = c.list_repos("model", "example-org")
        self.assertEqual(first.next_cursor, "Q2F0YQ==")
        c.list_repos("model", "example-org", cursor=first.next_cursor)
        second = t.calls[1]
        self.assertTrue(second["url"].startswith(API_BASE + "/api/models?"))
        self.assertEqual(query(second)["cursor"], ["Q2F0YQ=="])
        self.assertEqual(query(second)["author"], ["example-org"])

    def test_malicious_link_headers_are_refused(self):
        bad = [link("abc", host="example.com"), link("abc", host="huggingface.co.example.com"),
               link("abc").replace("https://", "http://"), link("abc", path="/api/datasets"),
               link("abc", host="huggingface.co:8443"), link("abc", host="user:@huggingface.co"),
               link("a%20b"), link("a<b"), link("x" * 1025), link("abc", extra="&cursor=def"),
               link("abc", extra="#frag"), 'garbage; rel="next"', "<not a url>; rel=next",
               "x" * 5000]
        for value in bad:
            with self.subTest(value=value[:50]):
                self.raises("bad_link", next_cursor, {"link": value}, "/api/models")
        self.assertIsNone(next_cursor({}, "/api/models"))
        self.assertIsNone(next_cursor({"link": link("abc").replace("next", "prev")},
                                      "/api/models"))

    def test_list_all_stops_at_the_page_cap_and_reports_listing_capped(self):
        full = [{"id": f"example-org/m{i}"} for i in range(500)]
        c, t, _ = live(raw(full, headers={**RATE, "Link": link("p2")}),
                       raw(full, headers={**RATE, "Link": link("p3")}))
        listing = c.list_all("model", "example-org")
        self.assertEqual((len(listing.records), listing.pages, listing.code),
                         (1000, 2, LISTING_CAPPED))
        self.assertEqual(len(t.calls), 2)

    def test_list_all_ends_without_a_cursor(self):
        c, t, _ = live(raw([{"id": "example-org/a"}],
                           headers={**RATE, "Link": link("p2", path="/api/spaces")}),
                       raw([{"id": "example-org/b"}]))
        listing = c.list_all("space", "example-org", limit=1)
        self.assertEqual((listing.pages, listing.code), (2, None))
        c, t, _ = live(raw([{"id": "example-org/a"}]))
        self.assertEqual(c.list_all("space", "example-org").code, None)
        self.assertEqual(len(t.calls), 1)
        self.raises("bad_max_pages", c.list_all, "space", "example-org", max_pages=3)


class ExpandPerKind(Case):
    def test_each_kind_sends_its_own_expand_list(self):
        for kind, path, fields in (
                ("model", "/api/models",
                 ["author", "lastModified", "private", "gated", "disabled"]),
                ("dataset", "/api/datasets",
                 ["author", "lastModified", "private", "gated", "disabled"]),
                ("space", "/api/spaces", ["author", "lastModified", "private", "disabled"])):
            with self.subTest(kind=kind):
                c, t, _ = live(raw([]))
                c.list_all(kind, "example-org", None)
                self.assertTrue(t.calls[0]["url"].startswith(API_BASE + path + "?"))
                self.assertEqual(query(t.calls[0])["expand"], fields)

    def test_gated_expand_for_spaces_is_refused_before_sending(self):
        c, t, b = live(raw([]))
        for expand in (("gated",), ("author", "gated"), hf_api.EXPAND_BY_KIND["model"]):
            with self.subTest(expand=expand):
                self.raises("bad_expand", c.list_repos, "space", "example-org", expand)
                self.raises("bad_expand", c.list_all, "space", "example-org", expand)
        self.assertEqual(t.calls, [])
        self.assertEqual(b.used, 0)
        c.list_repos("model", "example-org", ("gated",))
        self.assertEqual(query(t.calls[0])["expand"], ["gated"])

    def test_expand_map_stays_inside_the_record_allowlist(self):
        from repo_signals.collector.hf_fields import RECORD_KEYS
        self.assertEqual(set(hf_api.EXPAND_BY_KIND), set(hf_api.LIST_PATHS))
        for kind, fields in hf_api.EXPAND_BY_KIND.items():
            with self.subTest(kind=kind):
                self.assertTrue(set(fields) <= set(RECORD_KEYS) - {"id"})
        self.assertNotIn("gated", hf_api.EXPAND_BY_KIND["space"])

    def test_capped_for_a_short_last_page_with_a_cursor_remaining(self):
        c, t, _ = live(raw([{"id": "example-org/a"}], headers={**RATE, "Link": link("p2")}),
                       raw([{"id": "example-org/b"}], headers={**RATE, "Link": link("p3")}))
        listing = c.list_all("model", "example-org")
        self.assertEqual((len(listing.records), listing.pages, listing.code),
                         (2, 2, LISTING_CAPPED))
        self.assertEqual(len(t.calls), 2)
        c, t, _ = live(raw([{"id": "example-org/a"}], headers={**RATE, "Link": link("p2")}))
        self.assertEqual(c.list_all("model", "example-org", max_pages=1).code, LISTING_CAPPED)
        self.assertEqual(len(t.calls), 1)

    def test_constants_block_records_the_2026_10_27_check(self):
        src = (ROOT / "src/repo_signals/collector/hf_api.py").read_text()
        block = " ".join(line.lstrip("# ") for line in src.split("# ---", 2)[1].splitlines())
        for phrase in ('Checked live on 2026-10-27 (anonymous, read-only, org "huggingface")',
                       "/api/spaces returns 400 when expand includes gated",
                       "return all five on every record",
                       "private and disabled were false on every record seen",
                       "whether a short page (fewer records than the limit) can carry a next "
                       "cursor"):
            self.assertIn(phrase, block)
        unchecked = block.split("Still unchecked:", 1)[1]
        self.assertIn("gated and private behaviour", unchecked)


class NotWired(unittest.TestCase):
    def test_cli_does_not_import_the_client(self):
        self.assertNotIn("hf_api", (ROOT / "src/repo_signals/cli.py").read_text())
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import repo_signals.cli; "
                "print('repo_signals.collector.hf_api' in sys.modules)")
        r = subprocess.run([sys.executable, "-B", "-c", code, str(_src.SRC)],
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)

    def test_constants_block_lists_checked_and_unchecked(self):
        src = (ROOT / "src/repo_signals/collector/hf_api.py").read_text()
        self.assertEqual(src.count("# ---"), 2)
        block = " ".join(line.lstrip("# ") for line in src.split("# ---", 2)[1].splitlines())
        self.assertIn("Checked live on 2026-10-26 (anonymous, read-only, org \"huggingface\")",
                      block)
        for phrase in ("no website, blog or domain field", "500 requests per 300-second fixed "
                       "window", "silently capped at 1,000", "the 429 body and Retry-After",
                       "gated and private behaviour", "the token quota and scope",
                       "redirects for renamed repositories", "user-owned repositories",
                       "the exact search tokenisation",
                       "Hugging Face's terms for automated access and for outreach use"):
            self.assertIn(phrase, block)
        for name in ("API_HOST = \"huggingface.co\"", "/api/organizations/{name}/overview",
                     '"/api/models"', '"/api/datasets"', '"/api/spaces"'):
            self.assertIn(name, src)


if __name__ == "__main__":
    unittest.main()
