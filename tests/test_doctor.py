import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

import _src  # noqa: F401
from repo_signals import config, doctor
from repo_signals.config import Settings
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider

GOOD_SHEET = "S" * 10 + "_-" + "9" * 20
GOOD_TOKEN = "t0k" + "x" * 30


class KeyedStub:
    name = "keyed"
    required_env = ("STUB_API_KEY", "STUB_ACCOUNT")

    def find_people(self, company_domain, titles, limit):
        return []


def registry(*providers):
    r = Registry()
    for p in providers or (FakeProvider(),):
        r.register(p)
    return r


def found(name):
    return "/usr/bin/" + name


def run(env, reg=None, **kw):
    out = io.StringIO()
    kw.setdefault("which", found)
    code = doctor.run(environ=env, registry=reg or registry(), out=out, **kw)
    return code, out.getvalue()


def lines(text, status):
    return [l for l in text.splitlines() if l.startswith(status + " ")]


FULL = {config.SHEET_ID: GOOD_SHEET, config.GITHUB_TOKEN: GOOD_TOKEN,
        config.SENDER_NAME: "Avery Alder", config.SENDER_ROLE: "Engineer",
        config.ORG_NAME: "Example Org", config.POSTAL_ADDRESS: "1 Example Street, Example City",
        config.OPTOUT_TEXT: "Reply stop to opt out."}
_TMP = None


def setUpModule():
    """A synthetic service-account file (no real key) so FULL passes every check."""
    global _TMP
    _TMP = tempfile.TemporaryDirectory()
    path = os.path.join(_TMP.name, "sa.json")
    with open(path, "w") as fh:
        json.dump({"type": "service_account", "client_email": "svc" + "@" + "example.invalid",
                   "private_key": "-----BEGIN " + "PRIVATE" + " KEY-----\nsynthetic\n"}, fh)
    os.chmod(path, 0o600)
    FULL[config.GOOGLE_CREDENTIALS] = path
    suppression = os.path.join(_TMP.name, "suppression.txt")
    with open(suppression, "w") as fh:
        fh.write("# synthetic\nexample.invalid\n")
    FULL[config.SUPPRESSION_FILE] = suppression
    signal = os.path.join(_TMP.name, "signal.toml")
    with open(signal, "w") as fh:
        fh.write('name = "synthetic"\nterms = ["examplelib"]\n\n[[targets]]\n'
                 'domain = "example.com"\norg = "example-org"\n')
    FULL[config.SIGNAL_FILE] = signal


def tearDownModule():
    _TMP.cleanup()


class PassPaths(unittest.TestCase):
    def test_all_pass(self):
        code, out = run(dict(FULL))
        self.assertEqual(code, 0, out)
        self.assertEqual(lines(out, "WARN") + lines(out, "FAIL"), [])
        for expected in ("PASS Python ", "PASS REPO_SIGNALS_PROVIDER not set: using the built-in",
                         "PASS the selected provider needs no environment variables",
                         "PASS REPO_SIGNALS_MAX_CALLS not set: using the default",
                         "PASS dry run is on", "PASS REPO_SIGNALS_SHEET_ID is set",
                         "PASS REPO_SIGNALS_GITHUB_TOKEN is set",
                         "PASS REPO_SIGNALS_GOOGLE_CREDENTIALS is set",
                         "PASS REPO_SIGNALS_GOOGLE_CREDENTIALS file parses as JSON",
                         "PASS openssl found on PATH",
                         "PASS REPO_SIGNALS_POSTAL_ADDRESS is set",
                         "PASS REPO_SIGNALS_SUPPRESSION_FILE file parses: 0 email(s), 1 domain(s)",
                         "PASS drafting templates present and valid",
                         "PASS REPO_SIGNALS_SIGNAL_FILE file parses: 1 term(s), 1 target(s)"):
            self.assertIn(expected, out)
        self.assertIn("PASS live collection is not armed", out)
        self.assertIn("PASS REPO_SIGNALS_PROVIDER_MAX_CALLS not set: using the default of 25", out)
        self.assertIn("PASS REPO_SIGNALS_SHEETS_MAX_CALLS not set: using the default of 10", out)
        self.assertIn("PASS run --live is not armed; failing gates: REPO_SIGNALS_DRY_RUN", out)
        self.assertIn("doctor: 24 pass, 0 warn, 0 fail", out)

    def test_explicit_provider_and_max_calls(self):
        code, out = run({**FULL, config.PROVIDER: "fake", config.MAX_CALLS: "500"})
        self.assertEqual(code, 0, out)
        self.assertIn("PASS REPO_SIGNALS_PROVIDER names a registered provider", out)
        self.assertIn("PASS REPO_SIGNALS_MAX_CALLS is within the hard bound of 500", out)

    def test_required_env_set(self):
        env = {**FULL, config.PROVIDER: "keyed", "STUB_API_KEY": "k" * 12, "STUB_ACCOUNT": "a1"}
        code, out = run(env, registry(FakeProvider(), KeyedStub()))
        self.assertEqual(code, 0, out)
        self.assertIn("PASS STUB_API_KEY is set", out)
        self.assertIn("PASS STUB_ACCOUNT is set", out)


class WarnPaths(unittest.TestCase):
    def test_missing_sheet_and_token_warn_only(self):
        code, out = run({})
        self.assertEqual(code, 0, out)
        self.assertIn("WARN REPO_SIGNALS_SHEET_ID is missing", out)
        self.assertIn("WARN REPO_SIGNALS_GITHUB_TOKEN is missing", out)
        self.assertIn("WARN REPO_SIGNALS_GOOGLE_CREDENTIALS is missing", out)
        self.assertIn("WARN REPO_SIGNALS_OPTOUT_TEXT is missing", out)
        self.assertIn("WARN REPO_SIGNALS_SUPPRESSION_FILE is missing", out)
        self.assertIn("WARN REPO_SIGNALS_SIGNAL_FILE is missing", out)
        self.assertEqual(len(lines(out, "WARN")), 10)
        self.assertEqual(lines(out, "FAIL"), [])

    def test_whitespace_only_counts_as_missing(self):
        code, out = run({config.SHEET_ID: "", config.GITHUB_TOKEN: "",
                         config.GOOGLE_CREDENTIALS: "", config.SENDER_NAME: "   ",
                         config.SUPPRESSION_FILE: "", config.SIGNAL_FILE: ""})
        self.assertEqual(code, 0, out)
        self.assertEqual(len(lines(out, "WARN")), 10)


class FailPaths(unittest.TestCase):
    def assertFails(self, env, needle, reg=None, **kw):
        code, out = run(env, reg, **kw)
        self.assertEqual(code, 1, out)
        self.assertTrue(any(needle in l for l in lines(out, "FAIL")), out)
        return out

    def test_old_python(self):
        self.assertFails(dict(FULL), "3.12 or newer is required", version_info=(3, 11, 9))

    def test_malformed_provider(self):
        self.assertFails({**FULL, config.PROVIDER: "Not Valid"}, "REPO_SIGNALS_PROVIDER must be")

    def test_unregistered_provider(self):
        out = self.assertFails({**FULL, config.PROVIDER: "nosuch"},
                               "does not name a registered provider")
        self.assertNotIn("nosuch", out)

    def test_provider_missing_from_this_registry(self):
        self.assertFails({**FULL, config.PROVIDER: "apollo"}, "does not name a registered")

    def test_required_env_missing(self):
        env = {**FULL, config.PROVIDER: "keyed", "STUB_ACCOUNT": "a1"}
        out = self.assertFails(env, "STUB_API_KEY is missing",
                               registry(FakeProvider(), KeyedStub()))
        self.assertIn("PASS STUB_ACCOUNT is set", out)
        self.assertNotIn("PASS STUB_API_KEY", out)

    def test_required_env_blank_is_missing(self):
        env = {**FULL, config.PROVIDER: "keyed", "STUB_API_KEY": "  ", "STUB_ACCOUNT": "a1"}
        self.assertFails(env, "STUB_API_KEY is missing", registry(FakeProvider(), KeyedStub()))

    def test_bad_max_calls(self):
        for bad in ("0", "501", "ten", "5.0"):
            with self.subTest(bad=bad):
                self.assertFails({**FULL, config.MAX_CALLS: bad}, "REPO_SIGNALS_MAX_CALLS must be")

    def test_malformed_sheet_and_token(self):
        self.assertFails({**FULL, config.SHEET_ID: "short"}, "REPO_SIGNALS_SHEET_ID is not")
        self.assertFails({**FULL, config.GITHUB_TOKEN: "short"}, "REPO_SIGNALS_GITHUB_TOKEN is not")

    def test_dry_run_off_warns_and_arms(self):
        code, out = run(dict(FULL), settings=Settings(dry_run=False))
        self.assertEqual(code, 0, out)
        self.assertIn("WARN dry run is off: probe-org and collect --live can make GitHub "
                      "requests", out)
        self.assertIn("WARN live collection is ARMED (dry run is off and "
                      "REPO_SIGNALS_SIGNAL_FILE is set)", out)

    def test_dry_run_from_the_environment(self):
        code, out = run({**FULL, config.DRY_RUN: "false"})
        self.assertIn("WARN live collection is ARMED", out)
        code, out = run({config.DRY_RUN: "false"})
        self.assertIn("PASS live collection is not armed", out)
        self.assertIn("WARN dry run is off", out)
        code, out = run({**FULL, config.DRY_RUN: "SENTINELNO"})
        self.assertEqual(code, 1)
        self.assertIn('FAIL REPO_SIGNALS_DRY_RUN must be "true" or "false"', out)
        self.assertNotIn("SENTINEL", out)


class Online(unittest.TestCase):
    def test_online_flag_changes_nothing(self):
        for env in (dict(FULL), {config.MAX_CALLS: "0"}):
            with self.subTest(env=sorted(env)):
                c1, o1 = run(env)
                c2, o2 = run(env, online=True)
                self.assertEqual(c1, c2)
                self.assertIn("--online: not implemented yet", o2)
                self.assertEqual(o2.replace("--online: not implemented yet\n", ""), o1)


SENTINELS = {
    config.SHEET_ID: "SENTINELSHEETID" + "z" * 10,
    config.GITHUB_TOKEN: "SENTINELTOKENVALUE" + "q" * 10,
    "STUB_API_KEY": "SENTINELAPIKEY123",
    "STUB_ACCOUNT": "SENTINELACCOUNT9",
}
BAD_SENTINELS = {
    config.PROVIDER: "SENTINELPROV",
    config.MAX_CALLS: "SENTINELMAX",
    config.SHEET_ID: "SENTINELBADSHEET",
    config.GITHUB_TOKEN: "SENTINEL BAD TOKEN",
}


def assert_no_sentinel(test, text):
    test.assertNotIn("SENTINEL", text.upper())
    test.assertNotIn("zzsentinel", text)


class NeverPrintsValues(unittest.TestCase):
    def test_valid_values_not_printed(self):
        reg = registry(FakeProvider(), KeyedStub())
        code, out = run({**SENTINELS, config.PROVIDER: "keyed", config.MAX_CALLS: "437"}, reg)
        self.assertEqual(code, 0, out)
        assert_no_sentinel(self, out)
        self.assertNotIn("437", out)

    def test_registered_provider_name_not_printed(self):
        class Named(KeyedStub):
            name = "zzsentinelprov"
        code, out = run({**SENTINELS, config.PROVIDER: "zzsentinelprov"}, registry(Named()))
        self.assertEqual(code, 0, out)
        assert_no_sentinel(self, out)

    def test_invalid_values_not_printed(self):
        code, out = run(dict(BAD_SENTINELS))
        self.assertEqual(code, 1)
        self.assertEqual(len(lines(out, "FAIL")), 4, out)
        assert_no_sentinel(self, out)

    def test_cli_stdout_and_stderr(self):
        for env in ({**SENTINELS, config.MAX_CALLS: "437"}, dict(BAD_SENTINELS)):
            with self.subTest(valid=env is not BAD_SENTINELS):
                r = subprocess.run(
                    [sys.executable, "-m", "repo_signals", "doctor", "--online"],
                    env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_src.SRC),
                         "PYTHONDONTWRITEBYTECODE": "1", **env},
                    capture_output=True, text=True)
                assert_no_sentinel(self, r.stdout + r.stderr)
                self.assertNotIn("437", r.stdout + r.stderr)
                self.assertEqual(r.stderr, "")


if __name__ == "__main__":
    unittest.main()
