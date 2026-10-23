"""Collector PR 1 tests: the signal file, the request plan, collect --dry-run and doctor.
Offline and synthetic only. There is no network code to call."""
import ast
import io
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

import _src  # noqa: F401
from repo_signals import config, doctor
from repo_signals.cli import main
from repo_signals.collector import dry_run
from repo_signals.collector.plan import (CANDIDATE_SEARCH, MAX_CANDIDATES_CHECKED, ORG_LOOKUP,
                                         REPO_SEARCH, SEARCH_RATE_CEILING_PER_MINUTE, PlanError,
                                         build_plan)
from repo_signals.collector.signal import (DEFAULT_PUSHED_WITHIN_DAYS, MAX_SIGNAL_BYTES,
                                           MAX_TARGETS, MAX_TERMS, Signal, SignalError, Target,
                                           load_signal, parse_signal, signal_from_env)
from repo_signals.drafting.suppression import parse_suppression
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider

ROOT = Path(__file__).resolve().parent.parent


def toml_list(values):
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def signal_text(name="examplelib-adopters", terms=("examplelib",), targets=None, extra="",
                target_extra=""):
    targets = targets if targets is not None else [("example.com", "example-org")]
    lines = [f'name = "{name}"', f"terms = {toml_list(terms)}", extra, ""]
    for domain, org in targets:
        lines.append("[[targets]]")
        lines.append(f'domain = "{domain}"')
        if org:
            lines.append(f'org = "{org}"')
        if target_extra:
            lines.append(target_extra)
        lines.append("")
    return "\n".join(lines)


def everything(e):
    return "".join(traceback.format_exception(e)) + repr(e) + str(e.args)


class Raises:
    def raises(self, code, text):
        with self.assertRaises(SignalError) as cm:
            parse_signal(text)
        self.assertEqual(cm.exception.code, code)
        self.assertNotIn("SENTINEL", everything(cm.exception).upper())
        return cm.exception


# ---- the signal file ----

class SignalValid(Raises, unittest.TestCase):
    def test_minimal_and_defaults(self):
        s = parse_signal(signal_text())
        self.assertEqual(s, Signal("examplelib-adopters", ("examplelib",),
                                   (Target("example.com", "example-org", False),), None, 365))
        self.assertEqual(DEFAULT_PUSHED_WITHIN_DAYS, 365)

    def test_all_fields(self):
        text = signal_text(extra='schema = 1\nlanguage = "C++"\npushed_within_days = 30',
                           targets=[("Example.COM", "Example-Org"), ("example.invalid", None)])
        s = parse_signal(text)
        self.assertEqual((s.language, s.pushed_within_days), ("C++", 30))
        self.assertEqual(s.targets, (Target("example.com", "Example-Org", False),
                                     Target("example.invalid", None, False)))

    def test_confirm_org(self):
        s = parse_signal(signal_text(target_extra="confirm_org = true"))
        self.assertTrue(s.targets[0].confirm_org)
        s = parse_signal(signal_text(target_extra="confirm_org = false"))
        self.assertFalse(s.targets[0].confirm_org)
        self.raises("signal_target_1_confirm_org_without_org",
                    signal_text(targets=[("example.com", None)], target_extra="confirm_org = true"))
        self.raises("signal_target_1_bad_confirm_org", signal_text(target_extra='confirm_org = "yes"'))
        self.raises("signal_target_1_bad_confirm_org", signal_text(target_extra="confirm_org = 1"))

    def test_repr_holds_no_content(self):
        s = parse_signal(signal_text(name="sentinel-name", terms=("SENTINELTERM",)))
        self.assertNotIn("SENTINEL", repr(s).upper())


class SignalCaps(Raises, unittest.TestCase):
    def test_terms_count(self):
        five = [f"term{i}" for i in range(MAX_TERMS)]
        self.assertEqual(len(parse_signal(signal_text(terms=five)).terms), 5)
        self.raises("signal_too_many_terms", signal_text(terms=five + ["term9"]))
        self.raises("signal_no_terms", signal_text(terms=()))
        self.assertEqual(MAX_TERMS, 5)

    def test_term_length_and_characters(self):
        for term in ("ab", "a" * 60, "a.b_c+d-e", "Example2"):
            with self.subTest(term=term):
                self.assertEqual(parse_signal(signal_text(terms=(term,))).terms, (term,))
        for term in ("a", "a" * 61, "SENTINEL term", "SENTINEL:org", "-SENTINEL", "SENTINEL/x",
                     "SENTINEL'x", "é" * 3, ".SENTINEL"):
            with self.subTest(term=term):
                self.raises("signal_bad_term_1", signal_text(terms=(term,)))
        self.raises("signal_bad_term_2", signal_text(terms=("ok-term", "bad term")))

    def test_targets_count(self):
        fifty = [(f"t{i}.example.com", None) for i in range(MAX_TARGETS)]
        self.assertEqual(len(parse_signal(signal_text(targets=fifty)).targets), 50)
        self.raises("signal_too_many_targets",
                    signal_text(targets=fifty + [("extra.example.com", None)]))
        self.raises("signal_no_targets", 'name = "x"\nterms = ["examplelib"]\ntargets = []\n')
        self.assertEqual(MAX_TARGETS, 50)

    def test_pushed_within_days_bounds(self):
        for days in (1, 3650):
            with self.subTest(days=days):
                text = signal_text(extra=f"pushed_within_days = {days}")
                self.assertEqual(parse_signal(text).pushed_within_days, days)
        for bad in ("0", "3651", "-5", "true", '"365"', "36.5"):
            with self.subTest(bad=bad):
                self.raises("signal_bad_pushed_within_days",
                            signal_text(extra=f"pushed_within_days = {bad}"))

    def test_size_cap(self):
        base = signal_text()
        pad = MAX_SIGNAL_BYTES - len(base.encode()) - 2
        exact = base + "#" + "x" * pad + "\n"
        self.assertEqual(len(exact.encode()), MAX_SIGNAL_BYTES)
        parse_signal(exact)
        self.raises("signal_too_large", exact + "#")
        self.assertEqual(MAX_SIGNAL_BYTES, 64 * 1024)


class SignalRules(Raises, unittest.TestCase):
    def test_unknown_keys(self):
        self.raises("signal_unknown_key", signal_text(extra="SENTINELKEY = 1"))
        self.raises("signal_unknown_key", signal_text(extra='filenames = ["SENTINEL.yaml"]'))
        self.raises("signal_unknown_key", signal_text(extra='keywords = ["SENTINEL"]'))
        self.raises("signal_target_1_unknown_key", signal_text(target_extra='SENTINEL = "x"'))
        self.raises("signal_target_1_unknown_key", signal_text(target_extra="min_age = 3"))

    def test_wrong_types(self):
        cases = [('name = "x"\nterms = "SENTINEL"\n[[targets]]\ndomain = "example.com"\n',
                  "signal_bad_terms"),
                 ('name = "x"\nterms = [1, 2]\n[[targets]]\ndomain = "example.com"\n',
                  "signal_bad_term_1"),
                 ('name = "x"\nterms = ["examplelib"]\ntargets = "SENTINEL"\n', "signal_bad_targets"),
                 ('name = "x"\nterms = ["examplelib"]\ntargets = ["SENTINEL"]\n',
                  "signal_target_1_not_a_table"),
                 ('name = 5\nterms = ["examplelib"]\n[[targets]]\ndomain = "example.com"\n',
                  "signal_bad_name"),
                 ('terms = ["examplelib"]\n[[targets]]\ndomain = "example.com"\n',
                  "signal_missing_name"),
                 ('name = "x"\n[[targets]]\ndomain = "example.com"\n', "signal_missing_terms"),
                 ('name = "x"\nterms = ["examplelib"]\n', "signal_missing_targets"),
                 (signal_text(extra="language = 3"), "signal_bad_language"),
                 (signal_text(extra='language = "SENTINEL lang"'), "signal_bad_language"),
                 (signal_text(extra='language = "' + "x" * 33 + '"'), "signal_bad_language"),
                 (signal_text(extra="schema = 2"), "signal_bad_schema"),
                 (signal_text(extra='schema = "1"'), "signal_bad_schema")]
        for text, code in cases:
            with self.subTest(code=code):
                self.raises(code, text)

    def test_names(self):
        self.assertEqual(parse_signal(signal_text(name="a" * 48)).name, "a" * 48)
        for bad in ("a" * 49, "Upper", "1abc", "has space", "SENTINEL"):
            with self.subTest(bad=bad):
                self.raises("signal_bad_name", signal_text(name=bad))

    def test_duplicates(self):
        self.raises("signal_duplicate_domain",
                    signal_text(targets=[("example.com", None), ("EXAMPLE.com", None)]))
        self.raises("signal_duplicate_org",
                    signal_text(targets=[("example.com", "example-org"),
                                         ("example.invalid", "EXAMPLE-ORG")]))
        self.raises("signal_duplicate_term", signal_text(terms=("examplelib", "ExampleLib")))

    def test_bad_hostnames(self):
        for domain in ("localhost", "SENTINEL", "-x.example.com", "x-.example.com",
                       "x_y.example.com", "example.com.", "a" * 64 + ".example.com",
                       "192.0.2.1", "https://example.com", "example.com/path",
                       "example .com", "", "SENTINEL" + "@example.com", "example.c"):
            with self.subTest(domain=domain):
                self.raises("signal_target_1_bad_domain", signal_text(targets=[(domain, None)]))
        self.raises("signal_target_1_bad_domain",
                    'name = "x"\nterms = ["examplelib"]\n[[targets]]\norg = "example-org"\n')
        self.raises("signal_target_2_bad_domain",
                    signal_text(targets=[("example.com", None), ("bad", None)]))

    def test_org_shapes(self):
        for org in ("a", "example-org", "A1-b2-C3", "x" * 39):
            with self.subTest(org=org):
                self.assertEqual(parse_signal(signal_text(targets=[("example.com", org)]))
                                 .targets[0].org, org)
        for org in ("x" * 40, "-SENTINEL", "SENTINEL-", "SENTINEL--x", "SENTINEL_x",
                    "SENTINEL.x", "SENTINEL x", "SENTINEL/x"):
            with self.subTest(org=org):
                self.raises("signal_target_1_bad_org", signal_text(targets=[("example.com", org)]))
        self.raises("signal_target_1_bad_org",
                    'name = "x"\nterms = ["examplelib"]\n[[targets]]\ndomain = "example.com"\n'
                    "org = 7\n")

    def test_not_toml_or_not_utf8_never_echoed(self):
        self.raises("signal_not_toml", 'name = "SENTINEL\n[[[ SENTINEL')
        self.raises("signal_not_toml", 'SENTINEL = = "x"')
        self.raises("signal_not_toml", "[" * 5000)
        self.raises("signal_not_utf8", b"name = \"\xff\xfeSENTINEL\"")
        self.raises("signal_not_toml", None)

    def test_files_and_environment(self):
        with tempfile.TemporaryDirectory(prefix="SENTINELPATH-") as tmp:
            path = os.path.join(tmp, "signal.toml")
            with self.assertRaises(SignalError) as cm:
                load_signal(path)
            self.assertEqual(cm.exception.code, "signal_unreadable")
            self.assertNotIn("SENTINELPATH", everything(cm.exception))
            with open(path, "w") as fh:
                fh.write(signal_text())
            self.assertEqual(signal_from_env({config.SIGNAL_FILE: path}).name,
                             "examplelib-adopters")
            with self.assertRaises(SignalError) as cm:
                load_signal(tmp)
            self.assertEqual(cm.exception.code, "signal_unreadable")
        self.assertIsNone(signal_from_env({}))
        self.assertIsNone(signal_from_env({config.SIGNAL_FILE: ""}))
        with self.assertRaises(SignalError) as cm:
            signal_from_env({config.SIGNAL_FILE: " SENTINELPATH"})
        self.assertEqual(cm.exception.code, "signal_path_malformed")

    def test_config_setting(self):
        self.assertEqual(config.parse_signal_file({config.SIGNAL_FILE: "/tmp/s.toml"}),
                         "/tmp/s.toml")
        for bad in ('{"name": "SENTINEL"}', "/tmp/a\nb", " /tmp/x", "x" * 4097):
            with self.subTest(n=len(bad)), self.assertRaises(config.ConfigError) as cm:
                config.parse_signal_file({config.SIGNAL_FILE: bad})
            self.assertNotIn("SENTINEL", str(cm.exception))
            self.assertNotIn("/tmp", str(cm.exception))


# ---- the plan ----

def sig(targets, terms=("examplelib",)):
    return parse_signal(signal_text(terms=terms, targets=targets))


class PlanCounts(unittest.TestCase):
    def test_target_with_org(self):
        plan = build_plan(sig([("example.com", "example-org")], ("a1", "b2")))
        self.assertEqual([r.kind for r in plan.requests], [ORG_LOOKUP, REPO_SEARCH, REPO_SEARCH])
        self.assertEqual((plan.total, plan.core_count, plan.search_count), (3, 1, 2))

    def test_domain_only_target(self):
        plan = build_plan(sig([("example.com", None)]))
        self.assertEqual([r.kind for r in plan.requests],
                         [CANDIDATE_SEARCH] + [ORG_LOOKUP] * MAX_CANDIDATES_CHECKED + [REPO_SEARCH])
        self.assertEqual((plan.total, plan.core_count, plan.search_count), (5, 3, 2))

    def test_mixed_shapes(self):
        cases = [([("example.com", "example-org")], 1, (2, 1, 1)),
                 ([("example.com", "example-org"), ("example.invalid", None)], 3, (11, 4, 7)),
                 ([(f"t{i}.example.com", f"org{i}") for i in range(4)], 5, (24, 4, 20)),
                 ([(f"t{i}.example.com", None) for i in range(2)], 2, (12, 6, 6))]
        for targets, n_terms, want in cases:
            with self.subTest(targets=len(targets), terms=n_terms):
                plan = build_plan(sig(targets, tuple(f"term{i}" for i in range(n_terms))))
                self.assertEqual((plan.total, plan.core_count, plan.search_count), want)
                self.assertEqual(plan.targets_planned, len(targets))
                self.assertEqual(plan.skipped, ())

    def test_descriptions_have_no_urls(self):
        plan = build_plan(sig([("example.com", "example-org"), ("example.invalid", None)],
                              ("examplelib",)))
        descriptions = [r.describe() for r in plan.requests]
        self.assertEqual(descriptions[:2], ["org lookup: example-org (for example.com)",
                                            "repository search: term examplelib in example-org"])
        self.assertIn("candidate search: organisations for example.invalid", descriptions)
        self.assertIn("repository search: term examplelib in the org matched for example.invalid",
                      descriptions)
        for d in descriptions:
            self.assertNotIn("://", d)
            self.assertNotIn("http", d)
            self.assertNotIn("?", d)


class PlanRefusal(unittest.TestCase):
    def test_over_the_budget(self):
        s = sig([("example.com", "example-org"), ("example.invalid", None)], ("a1", "b2"))
        self.assertEqual(build_plan(s, budget_cap=9).total, 9)
        with self.assertRaises(PlanError) as cm:
            build_plan(s, budget_cap=8)
        e = cm.exception
        self.assertEqual((e.code, e.planned, e.allowed), ("plan_over_budget", 9, 8))
        self.assertEqual(str(e), "plan_over_budget: 9 planned, 8 allowed")

    def test_over_the_search_rate_ceiling(self):
        self.assertEqual(SEARCH_RATE_CEILING_PER_MINUTE, 30)
        terms = tuple(f"term{i}" for i in range(5))
        ok = sig([(f"t{i}.example.com", f"org{i}") for i in range(6)], terms)
        self.assertEqual(build_plan(ok, budget_cap=500).search_count, 30)
        over = sig([(f"t{i}.example.com", f"org{i}") for i in range(7)], terms)
        with self.assertRaises(PlanError) as cm:
            build_plan(over, budget_cap=500)
        self.assertEqual((cm.exception.code, cm.exception.planned, cm.exception.allowed),
                         ("plan_over_search_rate", 35, 30))
        self.assertEqual(str(cm.exception), "plan_over_search_rate: 35 planned, 30 allowed")

    def test_budget_cap_validated(self):
        s = sig([("example.com", "example-org")])
        for bad in (0, 501, True, "25", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_plan(s, budget_cap=bad)
        with self.assertRaises(TypeError):
            build_plan("signal")


class PlanSuppression(unittest.TestCase):
    def test_suppressed_targets_skipped_and_reported(self):
        s = sig([("example.com", "example-org"), ("shop.example.invalid", None),
                 ("example.invalid", "example-labs")], ("a1",))
        plan = build_plan(s, parse_suppression("example.invalid\n"))
        self.assertEqual(plan.skipped, ("shop.example.invalid", "example.invalid"))
        self.assertEqual(plan.targets_planned, 1)
        self.assertEqual({r.domain for r in plan.requests}, {"example.com"})
        self.assertEqual(plan.total, 2)

    def test_suppressed_email_domain_skips_the_target(self):
        s = sig([("example.com", "example-org"), ("example.invalid", None)])
        plan = build_plan(s, parse_suppression("someone" + "@example.com\n"))
        self.assertEqual(plan.skipped, ("example.com",))
        self.assertNotIn("example.com", {r.domain for r in plan.requests})

    def test_unrelated_suppression_skips_nothing(self):
        s = sig([("example.com", "example-org")])
        plan = build_plan(s, parse_suppression("other.example.invalid\n"))
        self.assertEqual((plan.skipped, plan.total), ((), 2))


# ---- collect --dry-run ----

class TmpDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="SENTINELPATH-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path


class Collect(TmpDir):
    def collect(self, env, dry=True):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("socket.socket", side_effect=AssertionError("socket")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("socket")):
            code = dry_run.run(dry_run=dry, environ=env, out=out, err=err)
        self.out, self.err = out.getvalue(), err.getvalue()
        for text in (self.out, self.err):
            self.assertNotIn("://", text)
            self.assertNotIn(self.dir, text)
        return code

    def test_dry_run_prints_the_plan(self):
        targets = [("example.com", "example-org"), ("shop.example.com", None),
                   ("example.invalid", None)]
        path = self.write("s.toml", signal_text(terms=("examplelib", "example-sdk"),
                                                targets=targets))
        supp = self.write("supp.txt", "example.invalid\n")
        code = self.collect({config.SIGNAL_FILE: path, config.SUPPRESSION_FILE: supp})
        self.assertEqual(code, 0, self.err)
        lines = self.out.splitlines()
        self.assertEqual(lines[:6], [
            "collect: dry run, nothing requested",
            "signal: examplelib-adopters",
            "targets: 3 (2 planned, 1 skipped as suppressed)",
            "terms: 2",
            "planned requests: 9 (core 4, search 5); budget 25; search ceiling 30 per minute "
            "(unverified)",
            "skipped (suppressed): example.invalid"])
        self.assertEqual(lines[6], "first 5 planned request(s):")
        self.assertEqual(len(lines), 12)
        self.assertEqual(lines[7], "  1. org lookup: example-org (for example.com)")

    def test_short_plan_shows_all(self):
        path = self.write("s.toml", signal_text())
        self.assertEqual(self.collect({config.SIGNAL_FILE: path}), 0)
        self.assertIn("first 2 planned request(s):", self.out)

    def test_refused_plan_runs_nothing(self):
        path = self.write("s.toml", signal_text(
            terms=tuple(f"term{i}" for i in range(5)),
            targets=[(f"t{i}.example.com", None) for i in range(3)]))
        code = self.collect({config.SIGNAL_FILE: path})
        self.assertEqual(code, 1)
        self.assertEqual(self.out, "")
        self.assertEqual(self.err, "collect: refused: plan_over_budget: 27 planned, 25 allowed; "
                                   "nothing requested\n")
        code = self.collect({config.SIGNAL_FILE: path, config.MAX_CALLS: "500"})
        self.assertEqual(code, 0, self.err)

    def test_problems_are_code_only(self):
        bad = self.write("bad.toml", 'name = "x"\nterms = ["SENTINEL value"]\n')
        cases = [({}, "collect: REPO_SIGNALS_SIGNAL_FILE is not set\n"),
                 ({config.SIGNAL_FILE: bad}, "collect: the signal file is not usable "
                                             "(signal_bad_term_1)\n"),
                 ({config.SIGNAL_FILE: os.path.join(self.dir, "nope.toml")},
                  "collect: the signal file is not usable (signal_unreadable)\n"),
                 ({config.SIGNAL_FILE: self.write("ok.toml", signal_text()),
                   config.SUPPRESSION_FILE: self.write("s.txt", "SENTINEL bad\n")},
                  "collect: the suppression list is not usable (suppression_malformed_line_1)\n"),
                 ({config.MAX_CALLS: "SENTINEL"},
                  "collect: REPO_SIGNALS_MAX_CALLS must be a whole number from 1 to 500\n")]
        for env, message in cases:
            with self.subTest(message=message):
                self.assertEqual(self.collect(env), 1)
                self.assertEqual(self.err, message)
                self.assertNotIn("SENTINEL", self.out + self.err)

    def test_live_mode_refused(self):
        path = self.write("s.toml", signal_text())
        self.assertEqual(self.collect({config.SIGNAL_FILE: path}, dry=False), 2)
        self.assertEqual((self.out, self.err), ("", "live collection is not implemented yet\n"))

    def test_cli(self):
        path = self.write("s.toml", signal_text())
        base = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_src.SRC),
                "PYTHONDONTWRITEBYTECODE": "1", config.SIGNAL_FILE: path}
        r = subprocess.run([sys.executable, "-m", "repo_signals", "collect"], env=base,
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout, r.stderr),
                         (2, "", "usage: repo-signals collect --dry-run | --live\n"))
        r = subprocess.run([sys.executable, "-m", "repo_signals", "collect", "--dry-run"],
                           env=base, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("collect: dry run, nothing requested", r.stdout)
        self.assertNotIn("://", r.stdout + r.stderr)

    def test_main_dispatch(self):
        with mock.patch.object(dry_run, "run", return_value=0) as run:
            self.assertEqual(main(["collect", "--dry-run"]), 0)
            run.assert_called_once_with(dry_run=True)
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(["collect"]), 2)
            run.assert_called_once_with(dry_run=True)  # bare "collect" never reaches it


# ---- doctor ----

class Doctor(TmpDir):
    def doctor(self, env):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        with mock.patch("socket.socket", side_effect=AssertionError("socket")):
            code = doctor.run(environ=env, registry=reg, out=out, which=str)
        text = out.getvalue()
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn(self.dir, text)
        return code, text

    def test_missing_warns(self):
        _, out = self.doctor({})
        self.assertIn(f"WARN {config.SIGNAL_FILE} is missing (needed before signals can be "
                      "collected)", out)

    def test_parses_counts_only(self):
        path = self.write("s.toml", signal_text(name="sentinel-name",
                                                terms=("SENTINELTERM", "examplelib"),
                                                targets=[("sentinel.example.com", "SENTINEL-org"),
                                                         ("example.invalid", None)]))
        code, out = self.doctor({config.SIGNAL_FILE: path})
        self.assertEqual(code, 0, out)
        self.assertIn(f"PASS {config.SIGNAL_FILE} is set", out)
        self.assertIn(f"PASS {config.SIGNAL_FILE} file parses: 2 term(s), 2 target(s)", out)
        self.assertNotIn("example.invalid", out)

    def test_problems_fail_with_codes(self):
        cases = [(self.write("bad.toml", "SENTINEL = = 1"), "signal_not_toml"),
                 (os.path.join(self.dir, "nope.toml"), "signal_unreadable"),
                 (self.write("big.toml", "#" + "x" * MAX_SIGNAL_BYTES), "signal_too_large")]
        for path, code_name in cases:
            with self.subTest(code=code_name):
                code, out = self.doctor({config.SIGNAL_FILE: path})
                self.assertEqual(code, 1, out)
                self.assertIn(f"FAIL {config.SIGNAL_FILE} file is not usable ({code_name})", out)
        code, out = self.doctor({config.SIGNAL_FILE: "SENTINEL\nPATH"})
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL {config.SIGNAL_FILE} is not a well-formed file path", out)


# ---- no network code; style ----

FORBIDDEN_MODULES = {"smtplib", "socket", "ssl", "urllib", "http", "subprocess", "asyncio",
                     "ftplib", "requests", "email"}
FORBIDDEN_RELATIVE = ("http", "providers", "sheets")
ALLOWED_SUBMODULES = {"urllib.parse"}  # pure string parsing; no network


class NoNetwork(unittest.TestCase):
    def test_collector_imports_nothing_that_talks_to_a_network(self):
        files = sorted((ROOT / "src/repo_signals/collector").glob("*.py"))
        self.assertEqual([f.name for f in files],
                         ["__init__.py", "dry_run.py", "evidence.py", "filters.py",
                          "github_api.py", "github_fields.py", "live.py", "matching.py",
                          "orchestrate.py", "plan.py", "signal.py"])
        # github_api.py is the one collector module allowed to use http.HttpClient; it is
        # tested on its own (test_collector_client.py) and is unreachable from the CLI.
        # live.py is the CLI's live path: it builds the client after a typed confirmation and is
        # tested on its own (test_collector_live.py).
        files = [f for f in files if f.name not in ("github_api.py", "live.py")]
        for f in files:
            for node in ast.walk(ast.parse(f.read_text())):
                with self.subTest(file=f.name, line=getattr(node, "lineno", 0)):
                    if isinstance(node, ast.Import):
                        for a in node.names:
                            if a.name not in ALLOWED_SUBMODULES:
                                self.assertNotIn(a.name.split(".")[0], FORBIDDEN_MODULES)
                    elif isinstance(node, ast.ImportFrom):
                        mod = node.module or ""
                        if node.level == 0:
                            if mod not in ALLOWED_SUBMODULES:
                                self.assertNotIn(mod.split(".")[0], FORBIDDEN_MODULES)
                        else:
                            self.assertFalse(mod.startswith(FORBIDDEN_RELATIVE), mod)

    def test_importing_the_collector_loads_no_network_module(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "import repo_signals.collector.signal, repo_signals.collector.plan,"
                " repo_signals.collector.dry_run, repo_signals.collector.github_fields,"
                " repo_signals.collector.filters, repo_signals.collector.evidence;"
                "bad = [m for m in ('repo_signals.http', 'socket', 'ssl', 'http.client',"
                " 'urllib.request', 'subprocess', 'smtplib') if m in sys.modules];"
                "print(bad)")
        r = subprocess.run([sys.executable, "-c", code, str(_src.SRC)],
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "[]", r.stderr)


class Source(unittest.TestCase):
    def test_design_doc_records_the_decisions(self):
        text = (ROOT / "docs/collector-design.md").read_text()
        self.assertIn("### Decided (maintainer)", text)
        self.assertNotIn("Decisions needed", text)
        self.assertNotIn("/search/code", text)
        self.assertNotIn("| `file` |", text)
        self.assertNotIn("cache purge`,** plus", text)
        self.assertIn("confirm_org = true", text)
        self.assertIn("defaults to 365", text)
        self.assertIn("6. **Live switch.**", text)


if __name__ == "__main__":
    unittest.main()
