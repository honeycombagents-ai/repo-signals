"""Build step 4 of the Hugging Face source: matching targets to organisations. A fake client and
synthetic data only (example-org names); no HTTP code and no socket."""
import ast
import socket
import unittest
from pathlib import Path
from unittest import mock

import _src  # noqa: F401
from repo_signals.collector.hf_api import Overview
from repo_signals.collector.hf_matching import (MATCHED, NOT_ATTEMPTED, SUPPRESSED, UNCONFIRMED,
                                                UNRESOLVED, HFMatchResult, match_hf_targets,
                                                report_line)
from repo_signals.collector.signal import HF_KINDS, Signal, Target, parse_signal
from repo_signals.drafting.suppression import parse_suppression
from repo_signals.providers import ProviderError

ROOT = Path(__file__).resolve().parent.parent
STOP_CODES = ("unauthorised", "rate_limited", "budget_exhausted", "server_error",
              "unexpected_shape")


def overview(name, verified=True, models=3, datasets=1, spaces=0):
    return Overview(name, verified, models, datasets, spaces)


class BioOverview:
    """An overview-like object that fails the test if anything beyond the allowlist is read."""

    def __init__(self, test, name):
        self._test, self.name, self.is_verified = test, name, True
        self.models, self.datasets, self.spaces = 1, 0, 0

    @property
    def bio(self):
        self._test.fail("the bio was read")


class FakeClient:
    def __init__(self, overviews=None):
        self.overviews = dict(overviews or {})
        self.calls = []

    def get_org_overview(self, org):
        self.calls.append(org)
        value = self.overviews.get(org, ProviderError("org_not_found"))
        if isinstance(value, BaseException):
            raise value
        return value


def signal(targets):
    return Signal(name="synthetic-hf", terms=("examplelib",),
                  targets=tuple(Target(*t) for t in targets), source="huggingface",
                  kinds=HF_KINDS, modified_within_days=30)


def confirmed(*pairs):
    return signal([(domain, org, True) for domain, org in pairs])


class NoSocket(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("socket used"))
        patcher.start()
        self.addCleanup(patcher.stop)


class Outcomes(NoSocket):
    def test_confirmed_verified_and_unverified_lines(self):
        client = FakeClient({"example-org": overview("example-org", True),
                             "example-labs": overview("example-labs", False)})
        report = match_hf_targets(confirmed(("example.com", "example-org"),
                                            ("a.example.com", "example-labs")), client)
        self.assertEqual(report.lines, (
            "example.com -> example-org (huggingface, confirmed, verified)",
            "a.example.com -> example-labs (huggingface, confirmed, unverified)"))
        self.assertEqual(report.results[0], HFMatchResult("example.com", "example-org", MATCHED,
                                                          True, 3, 1, 0))
        self.assertEqual(report.counts, {MATCHED: 2})
        self.assertIsNone(report.stop)
        self.assertEqual(client.calls, ["example-org", "example-labs"])

    def test_is_verified_false_still_matches(self):
        report = match_hf_targets(confirmed(("example.com", "example-org")),
                                  FakeClient({"example-org": overview("example-org", False)}))
        self.assertEqual(report.results[0].status, MATCHED)
        self.assertIs(report.results[0].is_verified, False)
        self.assertEqual([r.org for r in report.usable], ["example-org"])

    def test_not_found_is_unresolved_and_the_loop_continues(self):
        client = FakeClient({"example-org": overview("example-org")})
        report = match_hf_targets(confirmed(("a.example.com", "example-gone"),
                                            ("example.com", "example-org")), client)
        self.assertEqual([(r.status, r.code) for r in report.results],
                         [(UNRESOLVED, "org_not_found"), (MATCHED, None)])
        self.assertEqual(report.lines[0],
                         "a.example.com: org example-gone not found on Hugging Face (unresolved)")
        self.assertEqual(client.calls, ["example-gone", "example-org"])
        self.assertIsNone(report.stop)
        self.assertEqual([r.org for r in report.usable], ["example-org"])

    def test_suppressed_target_makes_no_request(self):
        client = FakeClient({"example-org": overview("example-org"),
                             "example-labs": overview("example-labs")})
        suppression = parse_suppression("example.com\n")
        report = match_hf_targets(confirmed(("shop.example.com", "example-org"),
                                            ("example.org", "example-labs")), client, suppression)
        self.assertEqual([r.status for r in report.results], [SUPPRESSED, MATCHED])
        self.assertEqual(client.calls, ["example-labs"])
        self.assertEqual(report.lines[0], "shop.example.com: suppressed (nothing requested)")

    def test_unconfirmed_is_never_requested(self):
        client = FakeClient({"example-org": overview("example-org")})
        report = match_hf_targets(signal([("example.com", "example-org", False),
                                          ("a.example.com", None, True),
                                          ("b.example.com", "example-org", "true")]), client)
        self.assertEqual([r.status for r in report.results], [UNCONFIRMED] * 3)
        self.assertEqual(client.calls, [])
        self.assertEqual(report.counts, {UNCONFIRMED: 3})
        self.assertEqual(report.lines[0], "example.com: not confirmed (set org and "
                                          "confirm_org = true; nothing requested)")
        self.assertEqual(report.usable, ())

    def test_zero_count_org_is_matched_and_flagged(self):
        client = FakeClient({"example-org": overview("example-org", True, 0, 0, 0),
                             "example-labs": overview("example-labs", False, 0, 0, 1)})
        report = match_hf_targets(confirmed(("example.com", "example-org"),
                                            ("a.example.com", "example-labs")), client)
        self.assertEqual([r.status for r in report.results], [MATCHED, MATCHED])
        self.assertEqual([r.no_repos for r in report.results], [True, False])
        self.assertEqual(report.lines[0], "example.com -> example-org (huggingface, confirmed, "
                                          "verified, no models, datasets or Spaces)")
        self.assertEqual(report.lines[1],
                         "a.example.com -> example-labs (huggingface, confirmed, unverified)")

    def test_a_parsed_signal_file_matches(self):
        text = ('source = "huggingface"\nname = "synthetic-hf"\nterms = ["examplelib"]\n\n'
                '[[targets]]\ndomain = "example.com"\norg = "example-org"\nconfirm_org = true\n')
        report = match_hf_targets(parse_signal(text),
                                  FakeClient({"example-org": overview("example-org")}))
        self.assertEqual(report.counts, {MATCHED: 1})


class Stops(NoSocket):
    def three(self):
        return confirmed(("example.com", "example-org"), ("a.example.com", "example-labs"),
                         ("b.example.com", "example-hq"))

    def test_each_stop_code_halts_the_loop_and_keeps_earlier_results(self):
        for code in STOP_CODES:
            with self.subTest(code=code):
                client = FakeClient({"example-org": overview("example-org"),
                                     "example-labs": ProviderError(code),
                                     "example-hq": overview("example-hq")})
                report = match_hf_targets(self.three(), client)
                self.assertEqual(report.stop, code)
                self.assertEqual(client.calls, ["example-org", "example-labs"])
                self.assertEqual([r.org for r in report.results], ["example-org"])
                self.assertEqual(report.counts, {MATCHED: 1, NOT_ATTEMPTED: 2})
                self.assertEqual(report.lines[-1],
                                 f"matching stopped ({code}): 2 target(s) not attempted")

    def test_any_other_exception_stops_with_a_code_only(self):
        client = FakeClient({"example-org": RuntimeError("SENTINEL https://example.com/x")})
        report = match_hf_targets(self.three(), client)
        self.assertEqual(report.stop, "client_error")
        self.assertEqual(report.results, ())
        self.assertEqual(report.counts, {NOT_ATTEMPTED: 3})
        self.assertNotIn("SENTINEL", " ".join(report.lines))

    def test_a_malformed_overview_stops_as_unexpected_shape(self):
        for bad in (None, {"name": "example-labs"}, overview("example-other"),
                    overview("example-labs", "yes"), overview("example-labs", True, -1),
                    overview("example-labs", True, 1, True), overview("bad name")):
            with self.subTest(bad=bad):
                client = FakeClient({"example-org": overview("example-org"), "example-labs": bad})
                report = match_hf_targets(self.three(), client)
                self.assertEqual(report.stop, "unexpected_shape")
                self.assertEqual(report.counts, {MATCHED: 1, NOT_ATTEMPTED: 2})
                self.assertEqual(client.calls, ["example-org", "example-labs"])

    def test_report_counts_add_up(self):
        suppression = parse_suppression("suppressed.example\n")
        s = signal([("example.com", "example-org", True), ("a.example.com", "example-gone", True),
                    ("suppressed.example", "example-hq", True),
                    ("b.example.com", "example-labs", False),
                    ("c.example.com", "example-busy", True), ("d.example.com", "example-x", True)])
        client = FakeClient({"example-org": overview("example-org", False),
                             "example-busy": ProviderError("rate_limited"),
                             "example-x": overview("example-x")})
        report = match_hf_targets(s, client, suppression)
        self.assertEqual(report.counts, {MATCHED: 1, UNRESOLVED: 1, SUPPRESSED: 1, UNCONFIRMED: 1,
                                         NOT_ATTEMPTED: 2})
        self.assertEqual(sum(report.counts.values()), len(s.targets))
        self.assertEqual(len(report.lines), len(report.results) + 1)
        self.assertEqual(client.calls, ["example-org", "example-gone", "example-busy"])


class Privacy(NoSocket):
    def test_the_bio_is_never_read(self):
        report = match_hf_targets(confirmed(("example.com", "example-org")),
                                  FakeClient({"example-org": BioOverview(self, "example-org")}))
        self.assertEqual(report.counts, {MATCHED: 1})
        self.assertEqual(report.results[0], HFMatchResult("example.com", "example-org", MATCHED,
                                                          True, 1, 0, 0))

    def test_report_line_for_an_unknown_status_is_code_only(self):
        self.assertEqual(report_line(HFMatchResult("example.com", "example-org", "other",
                                                   code="server_error")),
                         "example.com: not matched (server_error)")

    def test_needs_a_hugging_face_signal(self):
        github = parse_signal('name = "synthetic"\nterms = ["examplelib"]\n\n'
                              '[[targets]]\ndomain = "example.com"\n')
        for bad in (github, "signal", None):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                match_hf_targets(bad, FakeClient())


class NotWired(unittest.TestCase):
    def test_no_http_machinery_imported(self):
        tree = ast.parse((ROOT / "src/repo_signals/collector/hf_matching.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module or "", ("hf_api", "github_api", "http"))
                self.assertFalse((node.module or "").startswith(("http", "urllib")))
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotIn(a.name.split(".")[0], ("http", "socket", "ssl", "urllib"))

    def test_no_module_imports_it(self):
        for path in sorted((ROOT / "src/repo_signals").rglob("*.py")):
            if path.name != "hf_matching.py":
                with self.subTest(path=path.name):
                    self.assertNotIn("hf_matching", path.read_text())


if __name__ == "__main__":
    unittest.main()
