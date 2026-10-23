"""The sheet preflight: after the typed yes and before any GitHub or provider call, one header
read through the writer's own client. Offline only: a fake GitHub and a fake Google."""
import unittest

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_run as run_fx
import test_sheets as sheets_fx
from repo_signals import run as run_mod
from repo_signals.http import RawResponse
from repo_signals.providers.fake import FakeProvider
from repo_signals.run_gates import MIN_SHEETS_CALLS
from repo_signals.sheets.layout import COLUMNS


class WatchedGitHub(gh_fx.FakeGitHub):
    """Records the Google calls already made when the first GitHub request arrives."""

    def __init__(self, google):
        super().__init__(run_fx.ORGS, run_fx.REPOS)
        self.google = google
        self.google_before_first = None

    def __call__(self, *args, **kwargs):
        if self.google_before_first is None:
            self.google_before_first = self.google.kinds()
        return super().__call__(*args, **kwargs)


class Preflight(run_fx.Case):
    def run_with(self, google):
        self.providers = 0

        def factory(name, budget, registry):
            self.providers += 1
            return FakeProvider(), False

        github = WatchedGitHub(google)
        code = self.live(self.env(), github=github, google=google, provider_factory=factory)
        return code, github

    def assert_ended_before_collect(self, code, github, kinds, sheets_used, reason):
        self.assertEqual(code, 4, self.text)
        self.assertEqual(github.calls, [])
        self.assertEqual(self.providers, 0)
        self.assertEqual(self.google.kinds(), kinds)
        out = self.out.getvalue()
        self.assertIn(f"sheet preflight: refused ({reason}); nothing collected, no people "
                      "looked up", out)
        self.assertIn(f"requests made: github 0 of 10, provider 0 of 5, sheets {sheets_used} "
                      "of 10", out)
        self.assertIn(f"run: refused (sheet preflight: {reason})", out)
        self.assertNotIn("collect:", out)

    def test_a_good_sheet_passes_and_the_run_proceeds(self):
        code, github = self.run_with(sheets_fx.FakeGoogle(header=COLUMNS))
        self.assertEqual(code, 0, self.text)
        self.assertIn("sheet preflight: ok (the tool's header)", self.out.getvalue())
        self.assertEqual(github.google_before_first, ["token", "header"])
        self.assertEqual(self.google.kinds(), ["token", "header", "header", "rows", "append"])
        self.assertEqual(len(self.google.rows), 6)

    def test_an_empty_sheet_passes(self):
        code, github = self.run_with(sheets_fx.FakeGoogle())
        self.assertEqual(code, 0, self.text)
        self.assertIn("sheet preflight: ok (empty first row)", self.out.getvalue())
        self.assertEqual(github.google_before_first, ["token", "header"])
        self.assertEqual(self.google.kinds(),
                         ["token", "header", "header", "rows", "put", "append"])

    def test_another_header_ends_the_run_before_collect(self):
        google = sheets_fx.FakeGoogle(header=["their", "columns"], rows=[["their", "data"]])
        code, github = self.run_with(google)
        self.assert_ended_before_collect(code, github, ["token", "header"], 2, "header_mismatch")
        self.assertEqual((google.header, google.rows), (["their", "columns"], [["their", "data"]]))

    def test_an_unshared_sheet_ends_the_run_before_collect(self):
        google = sheets_fx.FakeGoogle(fail={"header": RawResponse(404, {}, b"{}")})
        code, github = self.run_with(google)
        self.assert_ended_before_collect(code, github, ["token", "header"], 2, "sheet_not_found")

    def test_a_refused_token_ends_the_run_before_collect(self):
        google = sheets_fx.FakeGoogle(fail={"token": RawResponse(400, {}, b"{}")})
        code, github = self.run_with(google)
        self.assert_ended_before_collect(code, github, ["token"], 1, "token_rejected")

    def test_exactly_one_extra_sheets_call_when_the_token_is_reused(self):
        code, _ = self.run_with(sheets_fx.FakeGoogle(header=COLUMNS))
        self.assertEqual(code, 0, self.text)
        kinds = self.google.kinds()
        self.assertEqual(kinds.count("token"), 1)
        self.assertEqual(kinds.count("header"), 2)
        # The writer alone makes token, header, rows, append (4); the preflight adds one.
        self.assertIn("sheets 5 of 10", self.out.getvalue())

    def test_the_writer_still_rereads_the_header(self):
        google = sheets_fx.FakeGoogle(header=COLUMNS)
        seen = []

        def change_after_preflight(kind):
            seen.append(kind)
            # The hook runs before the fake answers: this edit lands on the writer's own header
            # read, as if someone changed the sheet after the preflight.
            if seen == ["token", "header", "header"]:
                google.header = ["their", "columns"]

        google.on_call = change_after_preflight
        code, _ = self.run_with(google)
        self.assertEqual(code, 4, self.text)
        self.assertIn("sheet preflight: ok (the tool's header)", self.out.getvalue())
        self.assertIn("run: refused (sheet: header_mismatch)", self.out.getvalue())
        self.assertEqual(google.rows, [])

    def test_the_minimum_and_the_plan_constant_are_6(self):
        self.assertEqual(MIN_SHEETS_CALLS, run_mod.SHEETS_CALLS_PER_WRITE)
        self.assertEqual(MIN_SHEETS_CALLS, 6)


if __name__ == "__main__":
    unittest.main()
