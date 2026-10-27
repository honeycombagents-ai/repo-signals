"""Build step 5 of the Hugging Face source: orchestration. A fake client and synthetic records only
(example-org names), with sockets patched to fail; no HTTP code is involved."""
import ast
import json
import os
import socket
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import _src  # noqa: F401
from repo_signals.budget import CallBudget
from repo_signals.collector.hf_api import (API_HOST, EXPAND_BY_KIND, HuggingFaceClient, Listing,
                                           Overview)
from repo_signals.collector.hf_orchestrate import collect_hf
from repo_signals.collector.hf_matching import MATCHED, NOT_ATTEMPTED, SUPPRESSED, UNRESOLVED
from repo_signals.collector.orchestrate import COMPLETE, PARTIAL, REFUSED, SkippedSearch
from repo_signals.collector.plan import build_plan
from repo_signals.collector.signal import HF_KINDS, Signal, Target, parse_signal
from repo_signals.drafting.suppression import parse_suppression
from repo_signals.http import HttpClient, RawResponse
from repo_signals.providers import ProviderError

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 26)
STOP_CODES = ("unauthorised", "rate_limited", "budget_exhausted", "server_error",
              "unexpected_shape")
PREFIX = {"model": "https://huggingface.co/", "dataset": "https://huggingface.co/datasets/",
          "space": "https://huggingface.co/spaces/"}


def rec(owner, name, modified="2026-10-20T08:30:00.000Z", **extra):
    raw = {"id": f"{owner}/{name}", "lastModified": modified, "private": False,
           "description": "SENTINELDESC", "cardData": {"SENTINEL": 1}}
    raw.update(extra)
    return raw


def overview(name, models=5, datasets=5, spaces=5, verified=True):
    return Overview(name, verified, models, datasets, spaces)


class FakeClient:
    """Listings are a list of pages (each a list of records), or an exception to raise."""

    def __init__(self, overviews=None, listings=None):
        self.overviews = dict(overviews or {})
        self.listings = dict(listings or {})
        self.calls = []          # one entry per request the real client would send
        self.list_args = []

    def get_org_overview(self, org):
        self.calls.append(("overview", org))
        value = self.overviews.get(org, ProviderError("org_not_found"))
        if isinstance(value, BaseException):
            raise value
        return value

    def list_all(self, kind, org, expand=(), limit=500, max_pages=2):
        self.list_args.append((kind, org, expand, max_pages))
        value = self.listings.get((kind, org), [[]])
        if isinstance(value, BaseException):
            self.calls.append(("page", kind, org, 1))
            raise value
        if not isinstance(value, list):
            self.calls.append(("page", kind, org, 1))
            return value
        taken = value[:max_pages]
        for n in range(1, len(taken) + 1):
            self.calls.append(("page", kind, org, n))
        return Listing(tuple(r for page in taken for r in page), len(taken),
                       "listing_capped" if len(value) > max_pages else None)


def signal(targets, kinds=HF_KINDS, terms=("examplelib",)):
    return Signal(name="synthetic-hf", terms=terms,
                  targets=tuple(Target(d, o, True) for d, o in targets), source="huggingface",
                  kinds=kinds, modified_within_days=30)


TWO = [("example.com", "example-org"), ("example.invalid", "example-labs")]


def happy_client():
    return FakeClient(
        {"example-org": overview("example-org"), "example-labs": overview("example-labs")},
        {("model", "example-org"): [[rec("example-org", "examplelib-7b"),
                                     rec("example-org", "other-model")],
                                    [rec("example-org", "examplelib-old",
                                         "2025-01-01T00:00:00.000Z")]],
         ("dataset", "example-org"): [[rec("example-org", "examplelib-data"),
                                       rec("example-org", "examplelib-private", private=True)]],
         ("space", "example-org"): [[rec("example-org", "examplelib-demo"),
                                     rec("someone-else", "examplelib-copy")]],
         ("model", "example-labs"): [[rec("example-labs", "ExampleLib-small")]],
         ("dataset", "example-labs"): [[]],
         ("space", "example-labs"): [[rec("example-labs", "examplelib-gated", gated="auto")]]})


def pages(client):
    return [c for c in client.calls if c[0] == "page"]


def run(sig, client, suppression=None, cap=500):
    return collect_hf(sig, client, suppression, TODAY, budget_cap=cap)


class NoSocket(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def within_plan(self, sig, client, result, suppression=None):
        planned = build_plan(sig, suppression).total
        self.assertEqual(result.run_report.planned_requests, planned)
        self.assertLessEqual(len(client.calls), planned)
        self.assertLessEqual(result.run_report.requests_made, planned)
        for _, _, _, max_pages in client.list_args:
            self.assertEqual(max_pages, 2)


class HappyPath(NoSocket):
    def test_three_kinds_two_targets(self):
        client = happy_client()
        sig = signal(TWO)
        r = run(sig, client)
        self.assertEqual(client.calls, [
            ("overview", "example-org"), ("overview", "example-labs"),
            ("page", "model", "example-org", 1), ("page", "model", "example-org", 2),
            ("page", "dataset", "example-org", 1), ("page", "space", "example-org", 1),
            ("page", "model", "example-labs", 1), ("page", "dataset", "example-labs", 1),
            ("page", "space", "example-labs", 1)])
        self.assertEqual({a[2] for a in client.list_args}, {None})  # the client's per-kind list
        urls = {d: [e.url for e in items] for d, items in r.evidence_by_domain.items()}
        self.assertEqual(urls, {
            "example.com": [PREFIX["model"] + "example-org/examplelib-7b",
                            PREFIX["dataset"] + "example-org/examplelib-data",
                            PREFIX["space"] + "example-org/examplelib-demo"],
            "example.invalid": [PREFIX["model"] + "example-labs/ExampleLib-small"]})
        self.assertEqual([e.kind for e in r.evidence_by_domain["example.com"]],
                         ["hf-model", "hf-dataset", "hf-space"])
        run_report = r.run_report
        self.assertEqual((run_report.status, run_report.code), (COMPLETE, None))
        self.assertEqual((run_report.matching_requests, run_report.searches_made), (2, 7))
        self.assertEqual(run_report.planned_requests, 14)
        self.assertEqual(run_report.skipped_searches, ())
        self.assertEqual(r.match_report.counts, {MATCHED: 2})
        self.assertEqual(dict(r.filter_report.dropped), {
            "term_not_in_name": 1, "outside_window": 1, "private": 1, "owner_not_target": 1,
            "gated": 1})
        self.assertEqual(r.filter_report.kept, 4)
        self.within_plan(sig, client, r)
        for items in r.evidence_by_domain.values():
            for e in items:
                self.assertNotIn("SENTINEL", e.summary.upper())

    def test_drop_counts_add_up(self):
        client = happy_client()
        r = run(signal(TWO), client)
        returned = sum(len(p) for v in client.listings.values() for p in v)
        self.assertEqual(r.filter_report.kept + sum(r.filter_report.dropped.values()), returned)
        self.assertEqual(r.filter_report.kept,
                         sum(len(v) for v in r.evidence_by_domain.values()))

    def test_evidence_is_capped_at_ten_per_target(self):
        many = [rec("example-org", f"examplelib-{n}") for n in range(15)]
        client = FakeClient({"example-org": overview("example-org")},
                            {("model", "example-org"): [many[:8], many[8:]]})
        r = run(signal(TWO[:1], kinds=("model",)), client)
        self.assertEqual(len(r.evidence_by_domain["example.com"]), 10)
        self.assertEqual(dict(r.filter_report.dropped), {"over_target_cap": 5})
        self.assertEqual(r.run_report.status, COMPLETE)


class Targets(NoSocket):
    def test_zero_count_kinds_are_skipped_without_a_request(self):
        client = FakeClient({"example-org": overview("example-org", 2, 0, 1),
                             "example-labs": overview("example-labs", 0, 0, 0)},
                            {("model", "example-org"): [[rec("example-org", "examplelib-7b")]]})
        sig = signal(TWO)
        r = run(sig, client)
        self.assertEqual(pages(client), [("page", "model", "example-org", 1),
                                         ("page", "space", "example-org", 1)])
        self.assertEqual(r.run_report.status, COMPLETE)
        self.assertEqual(r.evidence_by_domain["example.invalid"], ())
        self.assertTrue(r.match_report.results[1].no_repos)
        self.within_plan(sig, client, r)

    def test_unresolved_target_lists_nothing(self):
        client = FakeClient({"example-labs": overview("example-labs")})
        r = run(signal(TWO), client)
        self.assertEqual([r.status for r in r.match_report.results], [UNRESOLVED, MATCHED])
        self.assertFalse([c for c in pages(client) if c[2] == "example-org"])
        self.assertEqual(len(pages(client)), 3)
        self.assertNotIn("example.com", r.evidence_by_domain)
        self.assertEqual(r.run_report.status, COMPLETE)

    def test_suppressed_target_is_not_requested(self):
        client = happy_client()
        suppression = parse_suppression("example.com\n")
        sig = signal(TWO)
        r = run(sig, client, suppression)
        self.assertFalse([c for c in client.calls if "example-org" in c])
        self.assertEqual(r.match_report.results[0].status, SUPPRESSED)
        self.assertEqual(r.run_report.suppressed, ("example.com",))
        self.assertEqual(r.run_report.planned_requests, 7)
        self.assertEqual(list(r.evidence_by_domain), ["example.invalid"])
        self.within_plan(sig, client, r, suppression)

    def test_a_parsed_signal_file_runs(self):
        text = ('source = "huggingface"\nname = "synthetic-hf"\nterms = ["examplelib"]\n'
                'kinds = ["model"]\n\n[[targets]]\ndomain = "example.com"\n'
                'org = "example-org"\nconfirm_org = true\n')
        client = FakeClient({"example-org": overview("example-org")},
                            {("model", "example-org"): [[rec("example-org", "examplelib-7b")]]})
        r = run(parse_signal(text), client)
        self.assertEqual(len(r.evidence_by_domain["example.com"]), 1)


class Stops(NoSocket):
    def test_each_stop_code_in_a_listing_halts_and_keeps_earlier_evidence(self):
        for code in STOP_CODES:
            with self.subTest(code=code):
                client = happy_client()
                client.listings[("dataset", "example-org")] = ProviderError(code)
                sig = signal(TWO)
                r = run(sig, client)
                self.assertEqual(pages(client)[-1], ("page", "dataset", "example-org", 1))
                self.assertEqual(len(pages(client)), 3)
                self.assertEqual((r.run_report.status, r.run_report.code), (PARTIAL, code))
                self.assertEqual(r.run_report.skipped_searches, (
                    SkippedSearch("example.com", 2, code),
                    SkippedSearch("example.com", 3, NOT_ATTEMPTED),
                    SkippedSearch("example.invalid", 1, NOT_ATTEMPTED),
                    SkippedSearch("example.invalid", 2, NOT_ATTEMPTED),
                    SkippedSearch("example.invalid", 3, NOT_ATTEMPTED)))
                self.assertEqual([e.url for e in r.evidence_by_domain["example.com"]],
                                 [PREFIX["model"] + "example-org/examplelib-7b"])
                self.assertEqual(r.evidence_by_domain["example.invalid"], ())
                self.within_plan(sig, client, r)

    def test_each_stop_code_in_matching_means_no_listings(self):
        for code in STOP_CODES:
            with self.subTest(code=code):
                client = happy_client()
                client.overviews["example-labs"] = ProviderError(code)
                r = run(signal(TWO), client)
                self.assertEqual(pages(client), [])
                self.assertEqual((r.run_report.status, r.run_report.code), (PARTIAL, code))
                self.assertEqual(r.run_report.skipped_searches, tuple(
                    SkippedSearch("example.com", n, NOT_ATTEMPTED) for n in (1, 2, 3)))
                self.assertEqual(r.evidence_by_domain, {"example.com": ()})

    def test_other_listing_errors_continue(self):
        client = happy_client()
        client.listings[("model", "example-org")] = ProviderError("bad_link")
        client.listings[("space", "example-org")] = RuntimeError("SENTINEL https://example.com/x")
        r = run(signal(TWO), client)
        self.assertEqual(len(pages(client)), 6)
        self.assertEqual((r.run_report.status, r.run_report.code), (PARTIAL, None))
        self.assertEqual(r.run_report.skipped_searches, (
            SkippedSearch("example.com", 1, "bad_link"),
            SkippedSearch("example.com", 3, "client_error")))
        self.assertNotIn("SENTINEL", repr(r.run_report).upper())
        self.assertEqual(len(r.evidence_by_domain["example.invalid"]), 1)

    def test_listing_capped_marks_partial_without_stopping(self):
        client = happy_client()
        client.listings[("model", "example-org")].append([rec("example-org", "examplelib-3")])
        sig = signal(TWO)
        r = run(sig, client)
        self.assertEqual(len(pages(client)), 7)
        self.assertEqual((r.run_report.status, r.run_report.code), (PARTIAL, None))
        self.assertEqual(r.run_report.skipped_searches,
                         (SkippedSearch("example.com", 1, "listing_capped"),))
        self.assertEqual(len(r.evidence_by_domain["example.com"]), 3)
        self.assertEqual(len(r.evidence_by_domain["example.invalid"]), 1)
        self.within_plan(sig, client, r)

    def test_a_malformed_listing_stops_as_unexpected_shape(self):
        for bad in ("SENTINEL", Listing((), 3, None), Listing((), 0, None),
                    Listing("x", 1, None), Listing((), 1, "other")):
            with self.subTest(bad=bad):
                client = happy_client()
                client.listings[("model", "example-org")] = bad
                r = run(signal(TWO), client)
                self.assertEqual((r.run_report.status, r.run_report.code),
                                 (PARTIAL, "unexpected_shape"))
                self.assertEqual(len(pages(client)), 1)

    def test_request_counts_never_exceed_the_plan(self):
        for cap_pages in (1, 2, 5):
            with self.subTest(cap_pages=cap_pages):
                listing = [[rec("example-org", f"examplelib-{n}")] for n in range(cap_pages)]
                client = FakeClient(
                    {"example-org": overview("example-org"),
                     "example-labs": overview("example-labs")},
                    {(k, o): listing for k in HF_KINDS for o in ("example-org", "example-labs")})
                sig = signal(TWO)
                r = run(sig, client)
                self.within_plan(sig, client, r)
                self.assertEqual(r.run_report.requests_made, len(client.calls))

    def test_refused_plan_makes_no_call(self):
        client = happy_client()
        r = run(signal(TWO), client, cap=13)
        self.assertEqual((r.run_report.status, r.run_report.code), (REFUSED, "plan_over_budget"))
        self.assertEqual(client.calls, [])
        self.assertEqual(r.evidence_by_domain, {})


class ThroughTheClient(NoSocket):
    """The real client behind a fake transport: what each listing request carries."""

    def transport(self, spaces_status=200):
        calls = []

        def send(url, headers, timeout, max_bytes, method="GET", body=None):
            calls.append(url)
            path, query = urlsplit(url).path, parse_qs(urlsplit(url).query)
            if path.endswith("/overview"):
                data = {"name": "example-org", "isVerified": True, "numModels": 2,
                        "numDatasets": 2, "numSpaces": 2, "details": "SENTINELBIO"}
            elif path == "/api/spaces" and "gated" in query.get("expand", []):
                return RawResponse(400, {}, b'{"error": "SENTINEL"}')
            else:
                kind = {"/api/models": "model", "/api/datasets": "dataset",
                        "/api/spaces": "space"}[path]
                data = [rec("example-org", f"examplelib-{kind}")]
                if kind == "space":
                    del data[0]["private"]
                    data[0].update(private=False, disabled=False)   # no gated key, as observed
                else:
                    data[0].update(author="example-org", gated=False, disabled=False)
            return RawResponse(200, {}, json.dumps(data).encode())

        return send, calls

    def test_spaces_are_requested_without_gated_and_yield_evidence(self):
        send, calls = self.transport()
        client = HuggingFaceClient(HttpClient((API_HOST,), transport=send), CallBudget(25),
                                   dry_run=False)
        r = collect_hf(signal(TWO[:1]), client, None, TODAY, budget_cap=25)
        expands = {urlsplit(u).path: parse_qs(urlsplit(u).query).get("expand") for u in calls}
        self.assertEqual(expands["/api/models"], list(EXPAND_BY_KIND["model"]))
        self.assertEqual(expands["/api/datasets"], list(EXPAND_BY_KIND["dataset"]))
        self.assertEqual(expands["/api/spaces"], ["author", "lastModified", "private", "disabled"])
        self.assertEqual((r.run_report.status, r.run_report.skipped_searches), (COMPLETE, ()))
        self.assertEqual([e.kind for e in r.evidence_by_domain["example.com"]],
                         ["hf-model", "hf-dataset", "hf-space"])
        self.assertEqual(r.run_report.requests_made, len(calls))


class NotWired(unittest.TestCase):
    def test_needs_a_hugging_face_signal(self):
        github = parse_signal('name = "synthetic"\nterms = ["examplelib"]\n\n'
                              '[[targets]]\ndomain = "example.com"\n')
        for bad in (github, "signal", None):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                collect_hf(bad, FakeClient(), None, TODAY)

    def test_only_the_gated_live_path_reaches_it(self):
        # live.py is collect --live and run.py is run --live: each reached only after its gates
        # and a typed "yes".
        for path in sorted((ROOT / "src/repo_signals").rglob("*.py")):
            if path.name not in ("hf_orchestrate.py", "live.py", "run.py"):
                with self.subTest(path=path.name):
                    self.assertNotIn("hf_orchestrate", path.read_text())
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import repo_signals.cli;"
                "print('repo_signals.collector.hf_orchestrate' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code, str(_src.SRC)],
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)

    def test_no_http_machinery(self):
        text = (ROOT / "src/repo_signals/collector/hf_orchestrate.py").read_text()
        self.assertNotIn("hf_api", text)
        self.assertNotIn("HuggingFaceClient", text)
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                for n in names:
                    self.assertFalse(n.split(".")[0] in ("http", "socket", "ssl", "urllib"), n)


if __name__ == "__main__":
    unittest.main()
