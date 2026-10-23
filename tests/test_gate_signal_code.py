"""The run --live gate line carries the signal-file code when the file does not load.

Offline only: the run is refused before any client is built, and doctor reads files only.
"""
import io
import unittest
from unittest import mock

import _src  # noqa: F401
import test_run as run_fx
from repo_signals import config, doctor
from repo_signals import run as run_mod
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider
from repo_signals.run_gates import check_gates

VAR = config.SIGNAL_FILE
DOMAIN_AS_ORG = ('name = "synthetic"\nterms = ["examplelib"]\n\n[[targets]]\n'
                 'domain = "example.com"\norg = "sentinel-example.com"\n')


class SignalGate(run_fx.Case):
    def refused(self, env):
        with mock.patch.object(run_mod, "GitHubClient", side_effect=AssertionError("built")):
            code = self.live(env)
        self.assertEqual(code, 2, self.text)
        self.assertEqual(self.calls(), (0, 0))
        self.assertNotIn("Type", self.out.getvalue())
        return self.err.getvalue()

    def doctor(self, env):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        code = doctor.run(environ=env, registry=reg, out=out, which=str)
        return code, out.getvalue()

    def assert_no_path_or_content(self, text):
        self.assertNotIn(self.dir, text)
        self.assertNotIn("SENTINELPATH", text)
        self.assertNotIn("sentinel-example", text)
        self.assertNotIn("[[targets]]", text)
        self.assertNotIn("examplelib", text)

    def test_domain_as_org_shows_the_code_in_the_run_refusal_and_doctor(self):
        env = self.env(**{VAR: self.write("bad-org.toml", DOMAIN_AS_ORG)})
        gate = f"{VAR} (signal_target_1_bad_org)"
        err = self.refused(env)
        self.assertIn(f"failing gates: {gate}; nothing requested", err)
        self.assert_no_path_or_content(err)
        code, out = self.doctor(env)
        self.assertIn(f"PASS run --live is not armed; failing gates: {gate}\n", out)
        self.assert_no_path_or_content(out)
        # doctor's exit code still comes from the signal file's own FAIL line, as before.
        self.assertIn(f"FAIL {VAR} file is not usable (signal_target_1_bad_org)", out)
        self.assertEqual(code, 1)

    def test_an_unparsable_file_shows_its_code(self):
        env = self.env(**{VAR: self.write("broken.toml", "name = [[[ sentinel-example\n")})
        err = self.refused(env)
        self.assertIn(f"failing gates: {VAR} (signal_not_toml); nothing requested", err)
        self.assert_no_path_or_content(err)

    def test_a_missing_variable_shows_no_code(self):
        failures = check_gates(self.env(**{VAR: None}), signer=object()).failures
        self.assertIn(VAR, failures)
        self.assertFalse(any(f.startswith(f"{VAR} (") for f in failures), failures)
        err = self.refused(self.env(**{VAR: None}))
        self.assertIn(f"failing gates: {VAR}; nothing requested", err)

    def test_an_unreadable_path_shows_its_code_not_the_path(self):
        env = self.env(**{VAR: self.dir + "/missing-sentinel.toml"})
        err = self.refused(env)
        self.assertIn(f"failing gates: {VAR} (signal_unreadable); nothing requested", err)
        self.assert_no_path_or_content(err)
        self.assertNotIn("missing-sentinel", err)


if __name__ == "__main__":
    unittest.main()
