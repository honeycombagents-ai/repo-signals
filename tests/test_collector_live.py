"""Collector PR 6 tests: probe-org and collect --live. Offline: a FAKE transport behind
http.HttpClient stands in for GitHub. Nothing here makes a real request."""
import builtins
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import _src  # noqa: F401
from repo_signals import config, doctor
from repo_signals.cli import main
from repo_signals.collector import dry_run, live
from repo_signals.collector.github_api import ALLOWED_HOSTS, GitHubClient
from repo_signals.collector.github_fields import WEB_HOST
from repo_signals.collector.orchestrate import CollectResult, RunReport
from repo_signals.collector.filters import FilterReport
from repo_signals.http import HttpClient, RawResponse
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "SENTINELTOKEN" + "z" * 24
TODAY = date(2026, 10, 21)
RATE = {"X-RateLimit-Remaining": "57", "X-RateLimit-Reset": "1767225600"}


def ok(data, status=200, headers=None):
    return RawResponse(status, dict(RATE if headers is None else headers),
                       json.dumps(data).encode())


def repo(login, name, **over):
    record = {"name": name, "full_name": f"{login}/{name}",
              "html_url": f"https://{WEB_HOST}/{login}/{name}", "language": "Python",
              "pushed_at": "2026-09-30T12:00:00Z", "fork": False, "archived": False,
              "private": False, "description": "SENTINELDESC",
              "owner": {"login": login, "type": "Organization", "email": "SENTINEL"}}
    record.update(over)
    return record


class FakeGitHub:
    """Routes by path and query; records every request."""

    def __init__(self, orgs=None, repos=None, fail=None):
        self.orgs = dict(orgs or {})
        self.repos = dict(repos or {})
        self.fail = dict(fail or {})
        self.calls = []

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        parts = urlsplit(url)
        self.calls.append({"url": url, "headers": dict(headers), "method": method})
        path = unquote(parts.path)
        if path.startswith("/orgs/"):
            key = ("org", path[len("/orgs/"):])
            if key in self.fail:
                return self.fail[key]
            found = self.orgs.get(key[1])
            return ok(found) if found else ok({"message": "SENTINEL"}, status=404)
        if path == "/search/repositories":
            tokens = parse_qs(parts.query)["q"][0].split(" ")
            org = next(t.split(":", 1)[1] for t in tokens if t.startswith("org:"))
            key = ("repos", tokens[0], org)
            if key in self.fail:
                return self.fail[key]
            items = self.repos.get((tokens[0], org), [])
            return ok({"total_count": len(items), "incomplete_results": False, "items": items})
        return ok({"items": []})


ORG = {"login": "example-org", "type": "Organization",
       "blog": "https://example.com/SENTINELBLOG", "is_verified": True}


class TTYIn:
    def __init__(self, answer):
        self.answer = answer
        self.events = None

    def isatty(self):
        return True

    def readline(self):
        if self.events is not None:
            self.events.append("prompt answered")
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class TTYOut(io.StringIO):
    def isatty(self):
        return True


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patcher = mock.patch("time.sleep", side_effect=AssertionError("slept"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def signal_file(self, targets=(("example.com", "example-org"),), terms=("examplelib",)):
        quoted = ", ".join(f'"{t}"' for t in terms)
        text = f'name = "synthetic"\nterms = [{quoted}]\n'
        for domain, org_login in targets:
            text += f'\n[[targets]]\ndomain = "{domain}"\norg = "{org_login}"\n'
        self.files = getattr(self, "files", 0) + 1
        return self.write(f"signal{self.files}.toml", text)

    def env(self, **over):
        env = {config.DRY_RUN: "false", config.SIGNAL_FILE: self.signal_file(),
               config.GITHUB_TOKEN: TOKEN, config.MAX_CALLS: "10"}
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def live(self, env, answer="yes\n", transport=None, stdin=None, out=None):
        self.transport = transport if transport is not None else FakeGitHub(
            {"example-org": ORG}, {("examplelib", "example-org"): [
                repo("example-org", "examplelib-core"), repo("example-org", "widget")]})
        self.out = out if out is not None else TTYOut()
        self.err = io.StringIO()
        stdin = stdin if stdin is not None else TTYIn(answer)
        code = live.run_live(env, stdin=stdin, out=self.out, err=self.err,
                             http=HttpClient(ALLOWED_HOSTS, transport=self.transport), today=TODAY)
        self.text = self.out.getvalue() + self.err.getvalue()
        self.assertNotIn(TOKEN, self.text)
        self.assertNotIn("SENTINEL", self.text.upper())
        return code


# ---- probe-org ----

class Probe(Case):
    def probe(self, login, env, transport=None):
        self.transport = transport if transport is not None else FakeGitHub({"example-org": ORG})
        out, err = io.StringIO(), io.StringIO()
        code = live.run_probe(login, env, out=out, err=err,
                              http=HttpClient(ALLOWED_HOSTS, transport=self.transport))
        self.out, self.err = out.getvalue(), err.getvalue()
        for text in (self.out, self.err):
            self.assertNotIn("SENTINEL", text.upper())
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("://", text)
        return code

    def test_exactly_one_request_and_code_only_output(self):
        with mock.patch.object(live, "GitHubClient", wraps=GitHubClient) as built:
            code = self.probe("example-org", self.env())
        self.assertEqual(code, 0, self.err)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(built.call_args.args[1].max_calls, 1)
        self.assertEqual(self.out.splitlines(), [
            "probe-org: login shape ok", "type: Organization", "website listed: yes",
            "is_verified: yes", "rate limit remaining: 57", "rate limit reset: 1767225600",
            "outcome: ok"])
        call = self.transport.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, call["url"])

    def test_other_shapes(self):
        t = FakeGitHub({"example-org": {"login": "example-org", "type": "User", "blog": "",
                                        "is_verified": None}})
        self.probe("example-org", self.env(), t)
        self.assertIn("type: User", self.out)
        self.assertIn("website listed: no", self.out)
        self.assertIn("is_verified: unknown", self.out)
        t = FakeGitHub({"example-org": {**ORG, "type": "SENTINEL <b>", "is_verified": False}})
        self.probe("example-org", self.env(), t)
        self.assertIn("type: unrecognised", self.out)
        self.assertIn("is_verified: no", self.out)

    def test_request_errors(self):
        self.assertEqual(self.probe("example-gone", self.env()), 1)
        self.assertIn("outcome: not_found", self.out)
        t = FakeGitHub(fail={("org", "example-org"): ok({}, status=403)})
        self.assertEqual(self.probe("example-org", self.env(), t), 1)
        self.assertIn("outcome: rate_limited", self.out)
        self.assertIn("rate limit reset: 1767225600", self.out)
        t = FakeGitHub(fail={("org", "example-org"): ok({}, status=401, headers={})})
        self.assertEqual(self.probe("example-org", self.env(), t), 1)
        self.assertIn("outcome: token_rejected", self.out)
        self.assertIn("rate limit remaining: unknown", self.out)
        self.assertEqual(len(t.calls), 1)

    def test_refusals_make_no_request(self):
        cases = [("example-org", self.env(**{config.DRY_RUN: None})),
                 ("example-org", self.env(**{config.DRY_RUN: "true"})),
                 ("example-org", self.env(**{config.DRY_RUN: "SENTINEL"})),
                 ("-bad", self.env()), ("SENTINEL org", self.env()), ("x" * 40, self.env()),
                 ("example-org", self.env(**{config.GITHUB_TOKEN: "SENTINEL short"}))]
        for login, env in cases:
            with self.subTest(login=login[:12], env=env.get(config.DRY_RUN)):
                with mock.patch.object(live, "GitHubClient",
                                       side_effect=AssertionError("built")):
                    self.assertEqual(self.probe(login, env), 2)
                self.assertEqual(self.transport.calls, [])
                self.assertIn("probe-org: refused:", self.err)
                self.assertEqual(self.out, "")


# ---- collect --live: refusals ----

class Refusals(Case):
    def assertRefused(self, env, needle, **kw):
        with mock.patch.object(live, "GitHubClient", side_effect=AssertionError("built")):
            code = self.live(env, **kw)
        self.assertEqual(code, 2, self.text)
        self.assertEqual(self.transport.calls, [])
        self.assertIn(needle, self.err.getvalue())
        self.assertIn("collect: refused:", self.err.getvalue())
        self.assertNotIn("Type", self.out.getvalue())

    def test_each_missing_condition(self):
        self.assertRefused(self.env(**{config.DRY_RUN: None}), "dry run is on (set "
                           "REPO_SIGNALS_DRY_RUN=false")
        self.assertRefused(self.env(**{config.DRY_RUN: "true"}), "dry run is on")
        self.assertRefused(self.env(**{config.SIGNAL_FILE: None}),
                           "REPO_SIGNALS_SIGNAL_FILE is not set")
        bad = self.write("bad.toml", 'name = "SENTINEL"\n')
        self.assertRefused(self.env(**{config.SIGNAL_FILE: bad}),
                           "the signal file is not usable (signal_bad_name)")
        self.assertRefused(self.env(**{config.MAX_CALLS: "1"}),
                           "plan_over_budget: 2 planned, 1 allowed")
        self.assertRefused(self.env(), "stdin and stdout must be a terminal",
                           stdin=io.StringIO("yes\n"))
        self.assertRefused(self.env(), "stdin and stdout must be a terminal",
                           out=io.StringIO())
        self.assertRefused(self.env(**{config.GITHUB_TOKEN: "SENTINEL bad"}),
                           "REPO_SIGNALS_GITHUB_TOKEN is not a well-formed token")
        supp = self.write("s.txt", "SENTINEL bad line\n")
        self.assertRefused(self.env(**{config.SUPPRESSION_FILE: supp}),
                           "the suppression list is not usable")

    def test_over_the_search_ceiling(self):
        targets = [(f"t{i}.example.com", f"org{i}") for i in range(7)]
        path = self.signal_file(targets, tuple(f"term{i}" for i in range(5)))
        self.assertRefused(self.env(**{config.SIGNAL_FILE: path, config.MAX_CALLS: "500"}),
                           "plan_over_search_rate: 35 planned, 30 allowed")


# ---- the confirmation prompt ----

class Prompt(Case):
    def test_only_exact_yes(self):
        for answer in ("y\n", "YES\n", "yes \n", " yes\n", "\n", "", "yes\r\n", "no\n",
                       "yesyes\n", KeyboardInterrupt(), OSError("SENTINEL")):
            with self.subTest(answer=repr(answer)):
                with mock.patch.object(live, "GitHubClient",
                                       side_effect=AssertionError("built")):
                    code = self.live(self.env(), answer=answer)
                self.assertEqual(code, 1)
                self.assertEqual(self.transport.calls, [])
                self.assertIn("collect: not confirmed; nothing requested", self.err.getvalue())
                self.assertIn('Type "yes" to make these GitHub requests: ', self.out.getvalue())

    def test_yes_with_or_without_newline(self):
        for answer in ("yes\n", "yes"):
            with self.subTest(answer=answer):
                self.assertEqual(self.live(self.env(), answer=answer), 0, self.text)
                self.assertEqual(len(self.transport.calls), 2)

    def test_plan_shown_before_the_prompt(self):
        self.live(self.env(), answer="no\n")
        lines = self.out.getvalue().splitlines()
        self.assertEqual(lines[:4], ["signal: synthetic",
                                     "targets: 1 (1 planned, 0 skipped as suppressed)",
                                     "terms: 1",
                                     "planned requests: at most 2 (core 1, search 1); budget 10; "
                                     "search ceiling 30 per minute (unverified)"])

    def test_client_built_only_after_confirmation(self):
        events = []
        stdin = TTYIn("yes\n")
        stdin.events = events

        def build(*args, **kwargs):
            events.append("client built")
            return GitHubClient(*args, **kwargs)

        with mock.patch.object(live, "GitHubClient", side_effect=build):
            self.assertEqual(self.live(self.env(), stdin=stdin), 0, self.text)
        self.assertEqual(events, ["prompt answered", "client built"])

    def test_no_yes_flag(self):
        for argv in (["collect", "--live", "--yes"], ["collect", "--yes"], ["collect", "-y"],
                     ["collect", "--live", "--no-confirm"], ["collect", "--live", "--force"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    main(argv)
                self.assertEqual(cm.exception.code, 2)
        for name in ("src/repo_signals/cli.py", "src/repo_signals/collector/live.py"):
            text = (ROOT / name).read_text()
            self.assertNotIn("--yes", text)
            self.assertNotIn("--force", text)
            self.assertNotIn("assume", text.lower())


# ---- the live run ----

class LiveRun(Case):
    def test_complete_run_prints_summary_and_evidence(self):
        code = self.live(self.env())
        self.assertEqual(code, 0, self.text)
        lines = self.out.getvalue().split('these GitHub requests: ', 1)[1].splitlines()
        self.assertEqual(lines, [
            "collect: complete", "planned requests: 2; searches made: 1", "matching: matched 1",
            "example.com -> example-org (user)", "evidence: example.com: 1", "kept: 1",
            "dropped 1: term_not_in_name",
            "evidence for example.com:",
            "  summary: The public repository example-org/examplelib-core matches the term "
            "examplelib and its primary language on GitHub is Python, last pushed on 2026-09-30.",
            f"  url: https://{WEB_HOST}/example-org/examplelib-core"])
        for call in self.transport.calls:
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertNotIn(TOKEN, call["url"])
            self.assertEqual(urlsplit(call["url"]).netloc, next(iter(ALLOWED_HOSTS)))

    def test_partial_exits_3_and_stops_on_rate_limit(self):
        targets = (("example.com", "example-org"), ("example.invalid", "example-labs"))
        orgs = {"example-org": ORG, "example-labs": {**ORG, "login": "example-labs",
                                                     "blog": "https://example.invalid"}}
        t = FakeGitHub(orgs, fail={("repos", "examplelib", "example-org"): ok({}, status=403)})
        env = self.env(**{config.SIGNAL_FILE: self.signal_file(targets)})
        self.assertEqual(self.live(env, transport=t), 3, self.text)
        self.assertEqual(len(t.calls), 3)  # two org lookups, one search, then stop
        self.assertIn("collect: partial (rate_limited)", self.out.getvalue())
        self.assertIn("search skipped: example.invalid term 1: not_attempted", self.out.getvalue())

    def test_partial_on_other_errors(self):
        t = FakeGitHub({"example-org": ORG},
                       fail={("repos", "examplelib", "example-org"): ok({}, status=502)})
        self.assertEqual(self.live(self.env(), transport=t), 3)
        self.assertIn("collect: partial", self.out.getvalue())

    def test_refused_result_exits_4(self):
        refused = CollectResult({}, None, FilterReport(),
                                RunReport("refused", "plan_over_budget",
                                          "plan_over_budget: 9 planned, 8 allowed", 0, (), 0, ()))
        with mock.patch.object(live, "collect", return_value=refused):
            self.assertEqual(self.live(self.env()), 4)
        self.assertIn("collect: refused: plan_over_budget", self.out.getvalue())

    def test_nothing_written_to_disk(self):
        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError("write attempted")
            return real_open(file, mode, *args, **kwargs)

        env = self.env()
        before = sorted(os.listdir(self.dir))
        with mock.patch("builtins.open", guarded_open), \
                mock.patch("os.open", side_effect=AssertionError("os.open")):
            self.assertEqual(self.live(env), 0, self.text)
        self.assertEqual(sorted(os.listdir(self.dir)), before)

    def test_token_never_printed_on_errors(self):
        t = FakeGitHub(fail={("org", "example-org"): ok({"message": TOKEN}, status=401)})
        code = self.live(self.env(), transport=t)
        self.assertEqual(code, 3)
        self.assertIn("example.com: not matched (token_rejected)", self.out.getvalue())

    def test_token_optional(self):
        env = self.env(**{config.GITHUB_TOKEN: None})
        self.assertEqual(self.live(env), 0)
        for call in self.transport.calls:
            self.assertNotIn("Authorization", call["headers"])


# ---- unchanged dry run, CLI dispatch, doctor ----

class Unchanged(Case):
    def test_dry_run_still_requests_nothing(self):
        for dry in ("true", "false"):
            with self.subTest(dry=dry):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch("socket.socket", side_effect=AssertionError("socket")), \
                        mock.patch.object(live, "GitHubClient", side_effect=AssertionError):
                    code = dry_run.run(dry_run=True, environ=self.env(**{config.DRY_RUN: dry}),
                                       out=out, err=err)
                self.assertEqual(code, 0, err.getvalue())
                self.assertEqual(out.getvalue().splitlines()[0],
                                 "collect: dry run, nothing requested")

    def test_cli_dispatch(self):
        with mock.patch.object(live, "run_live", return_value=0) as run_live, \
                mock.patch.object(live, "run_probe", return_value=0) as run_probe, \
                mock.patch.object(dry_run, "run", return_value=0) as run_dry:
            self.assertEqual(main(["collect", "--live"]), 0)
            self.assertEqual(main(["probe-org", "example-org"]), 0)
            self.assertEqual(main(["collect", "--dry-run"]), 0)
        run_live.assert_called_once()
        run_probe.assert_called_once()
        self.assertEqual(run_probe.call_args.args[0], "example-org")
        run_dry.assert_called_once_with(dry_run=True)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(main(["collect"]), 2)
        self.assertEqual(err.getvalue(), "usage: repo-signals collect --dry-run | --live\n")
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            main(["collect", "--dry-run", "--live"])

    def test_importing_the_cli_loads_no_live_code(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import repo_signals.cli;"
                "print([m for m in ('repo_signals.collector.live', "
                "'repo_signals.collector.github_api', 'repo_signals.collector.orchestrate') "
                "if m in sys.modules])")
        r = subprocess.run([sys.executable, "-c", code, str(_src.SRC)],
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "[]", r.stderr)

    def test_doctor_armed_warning(self):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        doctor.run(environ=self.env(), registry=reg, out=out, which=str)
        self.assertIn("WARN live collection is ARMED (dry run is off and "
                      "REPO_SIGNALS_SIGNAL_FILE is set)", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(self.dir, out.getvalue())
        out = io.StringIO()
        doctor.run(environ=self.env(**{config.DRY_RUN: None}), registry=reg, out=out, which=str)
        self.assertIn("PASS live collection is not armed", out.getvalue())

    def test_config_dry_run_parsing(self):
        self.assertTrue(config.load_settings({}).dry_run)
        self.assertTrue(config.load_settings({config.DRY_RUN: "true"}).dry_run)
        self.assertFalse(config.load_settings({config.DRY_RUN: "false"}).dry_run)
        for bad in ("False", "0", "no", "off", " false", "false ", "SENTINEL"):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError) as cm:
                config.load_settings({config.DRY_RUN: bad})
            self.assertNotIn("SENTINEL", str(cm.exception))


class Source(unittest.TestCase):
    def test_forbidden_keyword_absent(self):
        word = re.compile("lam" + "bda", re.IGNORECASE)
        for name in ("src/repo_signals/collector/live.py", "src/repo_signals/cli.py",
                     "src/repo_signals/config.py", "src/repo_signals/doctor.py",
                     "tests/test_collector_live.py", "docs/live-collection.md",
                     "docs/collector-design.md"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))


if __name__ == "__main__":
    unittest.main()
