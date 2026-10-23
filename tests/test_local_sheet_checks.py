"""Local sheet checks before the typed yes: the key file, openssl and the sheets budget.

doctor and the run --live gates share the run's own key-file check. Offline only: temporary key
files hold synthetic key-shaped text, openssl is never run, and nothing touches a network.
"""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_run as run_fx
import test_sheets as sheets_fx
from repo_signals import config, doctor
from repo_signals import run as run_mod
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider
from repo_signals.run_gates import (MIN_SHEETS_CALLS, OPENSSL_GATE, SHEETS_BUDGET_GATE,
                                    check_gates)

VAR = config.GOOGLE_CREDENTIALS
KEY = ("-----BEGIN " + "PRIVATE" + " KEY-----\nSENTINELKEYMATERIAL\n"
       "-----END " + "PRIVATE" + " KEY-----\n")
EMAIL = "svc-SENTINELSA" + "@" + "example.invalid"
GOOD = {"type": "service_account", "client_email": EMAIL, "private_key": KEY}
BAD = {
    "missing file": (None, "credentials_unreadable", "does not point to a readable file"),
    "not JSON": (b"{SENTINELKEYMATERIAL", "credentials_malformed", "does not parse as a JSON"),
    "missing field": ({"type": "service_account", "client_email": EMAIL},
                      "credentials_malformed", "lacks the expected keys: private_key"),
    "private key not shaped like a key": ({**GOOD, "private_key": "SENTINELKEYMATERIAL"},
                                          "credentials_malformed",
                                          "malformed field: private_key"),
    "client email with two @": ({**GOOD, "client_email": "svc" + "@@" + "example.invalid"},
                                "credentials_malformed", "malformed field: client_email"),
    "client email with a space": ({**GOOD, "client_email": "svc SENTINELSA" + "@" +
                                   "example.invalid"},
                                  "credentials_malformed", "malformed field: client_email"),
}


def found(name):
    return "/opt/synthetic/bin/" + name


def missing(name):
    return None


class Checks(run_fx.Case):
    """``self.env()`` is a full live environment that passes every gate (see test_run)."""

    def key_file(self, content):
        tmp = tempfile.TemporaryDirectory(prefix="SENTINELPATH-")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "sa.json")
        if content is not None:
            with open(path, "wb") as fh:
                fh.write(content if isinstance(content, bytes) else json.dumps(content).encode())
            os.chmod(path, 0o600)
        return path

    def doctor(self, env, which=found):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        code = doctor.run(environ=env, registry=reg, out=out, which=which)
        text = out.getvalue()
        self.assert_no_key_content(text)
        return code, text

    def gates(self, env, **kw):
        failures = list(check_gates(env, **kw).failures)
        self.assert_no_key_content(" ".join(failures))
        return failures

    def assert_no_key_content(self, text):
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn("BEGIN", text)
        self.assertNotIn("example.invalid", text)

    def test_every_bad_key_file_fails_doctor_and_the_gate(self):
        for label, (content, code, needle) in BAD.items():
            with self.subTest(label=label):
                env = self.env(**{VAR: self.key_file(content)})
                exit_code, out = self.doctor(env)
                self.assertEqual(exit_code, 1, out)
                fail = [l for l in out.splitlines() if l.startswith(f"FAIL {VAR}")]
                self.assertEqual(len(fail), 1, out)
                self.assertIn(needle, fail[0])
                self.assertTrue(fail[0].endswith(f"({code})"), fail[0])
                self.assertEqual(self.gates(env, which=found), [f"{VAR} file ({code})"])
                self.assertIn(f"failing gates: {VAR} file ({code})", out)

    def test_a_good_key_file_passes_doctor_and_the_gate(self):
        env = self.env(**{VAR: self.key_file(GOOD)})
        code, out = self.doctor(env)
        self.assertEqual(code, 0, out)
        self.assertEqual([l for l in out.splitlines() if l.startswith("FAIL")], [])
        self.assertIn("WARN run --live is ARMED", out)
        self.assertEqual(self.gates(env, which=found), [])

    def test_missing_openssl_blocks_only_the_built_in_signer(self):
        env = self.env(**{VAR: self.key_file(GOOD)})
        self.assertEqual(self.gates(env, which=missing), [OPENSSL_GATE])
        self.assertEqual(self.gates(env, which=missing, signer=sheets_fx.FakeSigner()), [])
        self.assertEqual(self.gates(env, which=found), [])

    def test_doctor_lists_openssl_as_a_gate_but_keeps_its_exit_code(self):
        env = self.env(**{VAR: self.key_file(GOOD)})
        code_found, _ = self.doctor(env, which=found)
        code_missing, out = self.doctor(env, which=missing)
        self.assertEqual((code_found, code_missing), (0, 0))
        self.assertIn("WARN openssl not found on PATH", out)
        self.assertIn(f"PASS run --live is not armed; failing gates: {OPENSSL_GATE}", out)

    def test_sheets_budget_of_5_blocks_and_6_passes(self):
        key = self.key_file(GOOD)
        self.assertEqual(self.gates(self.env(**{VAR: key, config.SHEETS_MAX_CALLS: "5"}),
                                    which=found), [SHEETS_BUDGET_GATE])
        self.assertEqual(self.gates(self.env(**{VAR: key, config.SHEETS_MAX_CALLS: "6"}),
                                    which=found), [])
        self.assertEqual(SHEETS_BUDGET_GATE, "REPO_SIGNALS_SHEETS_MAX_CALLS of at least 6")

    def test_the_minimum_matches_the_plan_constant(self):
        self.assertEqual(MIN_SHEETS_CALLS, run_mod.SHEETS_CALLS_PER_WRITE)
        self.assertEqual(MIN_SHEETS_CALLS, 6)


class RunGate(run_fx.Case):
    def run_live(self, env, signer, which):
        out, err = gh_fx.TTYOut(), io.StringIO()
        with mock.patch("repo_signals.run_gates.shutil.which", side_effect=which):
            code = run_mod.run(live=True, environ=env, stdin=gh_fx.TTYIn("no\n"), out=out,
                               err=err, signer=signer, run_id="gate")
        return code, out.getvalue() + err.getvalue()

    def test_bad_key_file_refuses_before_the_prompt(self):
        tmp = tempfile.TemporaryDirectory(prefix="SENTINELPATH-")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "sa.json")
        with open(path, "w") as fh:
            json.dump({**GOOD, "client_email": "svc" + "@@" + "example.invalid"}, fh)
        code, text = self.run_live(self.env(**{VAR: path}), sheets_fx.FakeSigner(), found)
        self.assertEqual(code, 2, text)
        self.assertIn(f"failing gates: {VAR} file (credentials_malformed)", text)
        self.assertNotIn("Type", text)
        self.assertNotIn("SENTINEL", text)

    def test_missing_openssl_refuses_only_without_a_custom_signer(self):
        code, text = self.run_live(self.env(), None, missing)
        self.assertEqual(code, 2, text)
        self.assertIn(f"failing gates: {OPENSSL_GATE}", text)
        self.assertNotIn("Type", text)
        code, text = self.run_live(self.env(), sheets_fx.FakeSigner(), missing)
        self.assertEqual(code, 1, text)  # reached the prompt, then declined
        self.assertIn('Type "yes" to run', text)


if __name__ == "__main__":
    unittest.main()
