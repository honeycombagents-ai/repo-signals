"""Build step 1 of the Hugging Face source: the signal file's source key, the Hugging Face target
rules, the request plan and REPO_SIGNALS_HF_MAX_CALLS. No client and no network code exist yet:
live commands refuse a Hugging Face file. Offline only, synthetic data."""
import io
import unittest
from unittest import mock

import _src  # noqa: F401
import test_run as run_fx
from repo_signals import config, doctor
from repo_signals import run as run_mod
from repo_signals.collector import dry_run, live
from repo_signals.collector.plan import (HF, HF_LISTING, HF_ORG_OVERVIEW, PlanError,
                                         build_plan, hf_requests_per_target)
from repo_signals.collector.signal import HF_KINDS, Signal, SignalError, Target, parse_signal
from repo_signals.drafting.suppression import parse_suppression
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider

GITHUB_TEXT = ('name = "synthetic"\nterms = ["examplelib"]\n\n'
               '[[targets]]\ndomain = "example.com"\norg = "example-org"\n')


def hf_text(extra="", target='org = "sentinel-org"\nconfirm_org = true\n', top=""):
    return (f'source = "huggingface"\nname = "synthetic-hf"\nterms = ["examplelib"]\n{top}{extra}\n'
            f'[[targets]]\ndomain = "example.com"\n{target}')


def code_of(text):
    with CapturedCode() as cm:
        parse_signal(text)
    return cm


class CapturedCode:
    """A tiny context manager that captures the SignalError code (no unittest instance needed)."""

    def __enter__(self):
        self.code = None
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            raise AssertionError("no SignalError raised")
        if not issubclass(kind, SignalError):
            return False
        self.code, self.text = value.code, str(value)
        return True


class GitHubUnchanged(unittest.TestCase):
    def test_a_file_without_source_parses_as_github_exactly_as_before(self):
        signal = parse_signal(GITHUB_TEXT)
        self.assertEqual(signal, Signal(name="synthetic", terms=("examplelib",),
                                        targets=(Target("example.com", "example-org"),)))
        self.assertEqual((signal.source, signal.kinds, signal.modified_within_days),
                         ("github", (), None))
        self.assertEqual(repr(signal), "Signal(1 term(s), 1 target(s))")
        self.assertEqual(parse_signal('source = "github"\n' + GITHUB_TEXT), signal)

    def test_github_plan_is_unchanged(self):
        plan = build_plan(parse_signal(GITHUB_TEXT), None, 25)
        self.assertEqual([r.kind for r in plan.requests], ["org_lookup", "repo_search"])
        self.assertEqual((plan.total, plan.core_count, plan.search_count, plan.hf_count),
                         (2, 1, 1, 0))

    def test_github_refuses_hugging_face_keys(self):
        for key, value in (("kinds", '["model"]'), ("modified_within_days", "30")):
            with self.subTest(key=key):
                cm = code_of(f"{key} = {value}\n" + GITHUB_TEXT)
                self.assertEqual(cm.code, f"signal_github_refuses_{key}")


class HuggingFaceFile(unittest.TestCase):
    def test_org_and_confirm_org_parse_with_defaults(self):
        signal = parse_signal(hf_text())
        self.assertEqual((signal.source, signal.kinds, signal.modified_within_days),
                         ("huggingface", HF_KINDS, 365))
        self.assertEqual(signal.targets, (Target("example.com", "sentinel-org", True),))
        self.assertEqual(repr(signal), "Signal(1 term(s), 1 target(s), huggingface)")
        chosen = parse_signal(hf_text('kinds = ["space", "model"]\nmodified_within_days = 30\n'))
        self.assertEqual((chosen.kinds, chosen.modified_within_days), (("space", "model"), 30))

    def test_each_refusal_has_its_code_and_no_value(self):
        cases = [
            ("missing org", hf_text(target="confirm_org = true\n"),
             "signal_target_1_confirm_org_without_org"),
            ("domain only", hf_text(target=""), "signal_target_1_hf_needs_org"),
            ("missing confirm_org", hf_text(target='org = "sentinel-org"\n'),
             "signal_target_1_hf_needs_confirm_org"),
            ("confirm_org false", hf_text(target='org = "sentinel-org"\nconfirm_org = false\n'),
             "signal_target_1_hf_needs_confirm_org"),
            ("pushed_within_days", hf_text("pushed_within_days = 30\n"),
             "signal_hf_refuses_pushed_within_days"),
            ("language", hf_text('language = "Python"\n'), "signal_hf_refuses_language"),
            ("bad kind", hf_text('kinds = ["sentinelkind"]\n'), "signal_bad_kinds"),
            ("empty kinds", hf_text("kinds = []\n"), "signal_bad_kinds"),
            ("kinds not a list", hf_text('kinds = "model"\n'), "signal_bad_kinds"),
            ("duplicate kind", hf_text('kinds = ["model", "model"]\n'), "signal_duplicate_kind"),
            ("window 0", hf_text("modified_within_days = 0\n"), "signal_bad_modified_within_days"),
            ("window 3651", hf_text("modified_within_days = 3651\n"),
             "signal_bad_modified_within_days"),
            ("window text", hf_text('modified_within_days = "30"\n'),
             "signal_bad_modified_within_days"),
            ("window bool", hf_text("modified_within_days = true\n"),
             "signal_bad_modified_within_days"),
            ("bad source", 'source = "sentinelsource"\n' + GITHUB_TEXT, "signal_bad_source"),
        ]
        for label, text, code in cases:
            with self.subTest(label=label):
                cm = code_of(text)
                self.assertEqual(cm.code, code)
                self.assertNotIn("sentinel", cm.text)


class HuggingFacePlan(unittest.TestCase):
    def plan_for(self, kinds, targets=1, cap=25, suppression=None):
        signal = Signal(name="synthetic-hf", terms=("examplelib",),
                        targets=tuple(Target(f"t{i}.example.com", f"org-{i}", True)
                                      for i in range(targets)),
                        source="huggingface", kinds=kinds, modified_within_days=365)
        return build_plan(signal, suppression, cap)

    def test_bound_is_one_overview_plus_two_pages_per_kind(self):
        for kinds, per_target in ((("model",), 3), (("model", "dataset"), 5), (HF_KINDS, 7)):
            with self.subTest(kinds=kinds):
                self.assertEqual(hf_requests_per_target(kinds), per_target)
                plan = self.plan_for(kinds)
                self.assertEqual((plan.total, plan.hf_count, plan.search_count, plan.core_count),
                                 (per_target, per_target, 0, 0))
                self.assertEqual(self.plan_for(kinds, targets=2).total, 2 * per_target)
        plan = self.plan_for(("model", "space"))
        self.assertEqual([(r.kind, r.listing, r.page) for r in plan.requests],
                         [(HF_ORG_OVERVIEW, None, None), (HF_LISTING, "model", 1),
                          (HF_LISTING, "model", 2), (HF_LISTING, "space", 1),
                          (HF_LISTING, "space", 2)])
        self.assertTrue(all(r.resource == HF and "://" not in r.describe() for r in plan.requests))

    def test_budget_rate_ceiling_and_suppression(self):
        with self.assertRaises(PlanError) as cm:
            self.plan_for(HF_KINDS, targets=4, cap=27)
        self.assertEqual((cm.exception.code, cm.exception.planned, cm.exception.allowed),
                         ("plan_over_budget", 28, 27))
        with self.assertRaises(PlanError) as cm:
            self.plan_for(HF_KINDS, targets=60, cap=500)
        self.assertEqual(cm.exception.code, "plan_over_hf_rate")
        plan = self.plan_for(HF_KINDS, targets=2, suppression=parse_suppression("t0.example.com\n"))
        self.assertEqual((plan.skipped, plan.targets_planned, plan.total),
                         (("t0.example.com",), 1, 7))


class HfMaxCalls(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(config.parse_hf_max_calls({}), 25)
        self.assertEqual(config.parse_hf_max_calls({config.HF_MAX_CALLS: "7"}), 7)
        self.assertEqual(config.load_settings({config.HF_MAX_CALLS: "500"}).hf_max_calls, 500)
        for bad in ("0", "501", "SENTINEL", " 7"):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError) as cm:
                config.parse_hf_max_calls({config.HF_MAX_CALLS: bad})
            self.assertEqual(cm.exception.var, config.HF_MAX_CALLS)
            self.assertNotIn("SENTINEL", str(cm.exception))


class Commands(run_fx.Case):
    def hf_env(self, **over):
        path = self.write("hf.toml", hf_text())
        return self.env(**{config.SIGNAL_FILE: path, **over})

    def test_collect_dry_run_prints_the_hugging_face_plan(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("socket.socket", side_effect=AssertionError("socket")):
            code = dry_run.run(environ=self.hf_env(), out=out, err=err)
        self.assertEqual(code, 0, err.getvalue())
        text = out.getvalue()
        self.assertIn("source: huggingface; kinds: model, dataset, space", text)
        self.assertIn("planned requests: at most 7 (huggingface 7: 1 overview per target, at "
                      "most 2 pages per kind); budget 25 (REPO_SIGNALS_HF_MAX_CALLS)", text)
        self.assertIn("organisation overview: sentinel-org (for example.com)", text)
        self.assertNotIn("://", text)

    def test_collect_live_without_a_terminal_refuses_before_any_request(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(live, "GitHubClient", side_effect=AssertionError("built")), \
                mock.patch.object(live, "HuggingFaceClient", side_effect=AssertionError("built")):
            code = live.run_live(self.hf_env(), stdin=io.StringIO("yes\n"), out=out, err=err)
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(),
                         "collect: refused: stdin and stdout must be a terminal (live collection "
                         "needs a typed confirmation); nothing requested\n")
        self.assertEqual(out.getvalue(), "")

    def test_run_live_refuses_before_any_request(self):
        with mock.patch.object(run_mod, "GitHubClient", side_effect=AssertionError("built")):
            code = self.live(self.hf_env())
        self.assertEqual(code, 2, self.text)
        self.assertIn("failing gates: huggingface_not_implemented; nothing requested", self.text)
        self.assertEqual(self.calls(), (0, 0))
        self.assertNotIn("sentinel-org", self.text)

    def test_doctor_shows_the_source_and_the_gate(self):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        doctor.run(environ=self.hf_env(), registry=reg, out=out, which=str)
        text = out.getvalue()
        self.assertIn("PASS REPO_SIGNALS_SIGNAL_FILE file parses: 1 term(s), 1 target(s); "
                      "source huggingface, 3 kind(s)", text)
        self.assertIn("PASS REPO_SIGNALS_HF_MAX_CALLS not set: using the default of 25", text)
        self.assertIn("failing gates: huggingface_not_implemented", text)
        self.assertNotIn("sentinel-org", text)

    def test_doctor_github_signal_line_is_unchanged(self):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        doctor.run(environ=self.env(), registry=reg, out=out, which=str)
        self.assertIn("PASS REPO_SIGNALS_SIGNAL_FILE file parses: 1 term(s), 2 target(s)\n",
                      out.getvalue())


if __name__ == "__main__":
    unittest.main()
