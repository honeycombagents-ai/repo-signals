"""doctor and config checks for the Google Sheets settings. Offline and synthetic only."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _src  # noqa: F401
from repo_signals import config, doctor
from repo_signals.config import ConfigError, Settings, load_settings
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider

VAR = config.GOOGLE_CREDENTIALS
KEY = ("-----BEGIN " + "PRIVATE" + " KEY-----\nSENTINELKEYMATERIAL\n"
       "-----END " + "PRIVATE" + " KEY-----\n")
GOOD = {"type": "service_account", "client_email": "svc-SENTINELSA" + "@" + "example.invalid",
        "private_key": KEY, "private_key_id": "SENTINELKEYID"}


def found(name):
    return "/opt/synthetic/bin/" + name


def missing(name):
    return None


class CredsDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="SENTINELPATH-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, content, mode=0o600, name="sa.json"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(content if isinstance(content, bytes) else json.dumps(content).encode())
        os.chmod(path, mode)
        return path

    def doctor(self, env, which=found):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        code = doctor.run(environ=env, registry=reg, out=out, which=which)
        text = out.getvalue()
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn(self.dir, text)
        return code, text


class DoctorCredentials(CredsDir):
    def test_missing_warns(self):
        code, out = self.doctor({})
        self.assertEqual(code, 0)
        self.assertIn(f"WARN {VAR} is missing", out)

    def test_good_file(self):
        code, out = self.doctor({VAR: self.write(GOOD)})
        self.assertEqual(code, 0, out)
        self.assertIn(f"PASS {VAR} is set", out)
        self.assertIn(f"PASS {VAR} file parses as JSON with the expected keys "
                      "(type, client_email, private_key)", out)
        self.assertNotIn("readable by other users", out)

    def test_problems_fail_with_names_only(self):
        cases = [(None, "does not point to a readable file"),
                 (b"{SENTINELKEYMATERIAL", "does not parse as a JSON object"),
                 (b"[]", "does not parse as a JSON object"),
                 ({"type": "service_account", "private_key_id": "SENTINELKEYID"},
                  "lacks the expected keys: client_email, private_key"),
                 ({**GOOD, "type": "SENTINELTYPE"}, "is not a service-account key"),
                 (b" " * (64 * 1024 + 1), "too large")]
        for content, needle in cases:
            with self.subTest(needle=needle):
                path = os.path.join(self.dir, "nope.json") if content is None \
                    else self.write(content)
                code, out = self.doctor({VAR: path})
                self.assertEqual(code, 1, out)
                self.assertIn(f"PASS {VAR} is set", out)
                self.assertTrue(any(l.startswith(f"FAIL {VAR}") and needle in l
                                    for l in out.splitlines()), out)

    def test_directory_is_not_a_file(self):
        code, out = self.doctor({VAR: self.dir})
        self.assertEqual(code, 1)
        self.assertIn("does not point to a readable file", out)

    def test_loose_permissions_warn(self):
        code, out = self.doctor({VAR: self.write(GOOD, mode=0o644)})
        self.assertEqual(code, 0, out)
        self.assertIn(f"WARN {VAR} file is readable by other users", out)

    def test_json_in_the_variable_is_refused_unprinted(self):
        code, out = self.doctor({VAR: json.dumps(GOOD)})
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL {VAR} must be a path to the service-account JSON file", out)

    def test_openssl(self):
        _, out = self.doctor({}, which=found)
        self.assertIn("PASS openssl found on PATH", out)
        self.assertNotIn("/opt/synthetic", out)
        code, out = self.doctor({}, which=missing)
        self.assertEqual(code, 0)
        self.assertIn("WARN openssl not found on PATH", out)

    def test_sheet_id_set_or_missing(self):
        _, out = self.doctor({})
        self.assertIn("WARN REPO_SIGNALS_SHEET_ID is missing", out)
        _, out = self.doctor({config.SHEET_ID: "SENTINELSHEETID_abcdefghij"})
        self.assertIn("PASS REPO_SIGNALS_SHEET_ID is set", out)

    def test_offline(self):
        path = self.write(GOOD)
        with mock.patch("socket.socket", side_effect=AssertionError("socket")), \
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess")), \
                mock.patch("urllib.request.OpenerDirector.open",
                           side_effect=AssertionError("network")):
            code, _ = self.doctor({VAR: path})
        self.assertEqual(code, 0)

    def test_cli_prints_no_value(self):
        path = self.write(GOOD)
        r = subprocess.run(
            [sys.executable, "-m", "repo_signals", "doctor"],
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_src.SRC),
                 "PYTHONDONTWRITEBYTECODE": "1", VAR: path,
                 config.SHEET_ID: "SENTINELSHEETID_abcdefghij"},
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"PASS {VAR} file parses as JSON", r.stdout)
        self.assertNotIn("SENTINEL", r.stdout + r.stderr)
        self.assertNotIn(self.dir, r.stdout + r.stderr)


class ConfigCredentials(unittest.TestCase):
    def test_valid_path(self):
        for path in ("/tmp/sa.json", "relative/sa.json", "C:\\keys\\sa.json", "~/sa.json"):
            with self.subTest(path=path):
                self.assertEqual(load_settings({VAR: path}).google_credentials, path)
        self.assertIsNone(load_settings({}).google_credentials)
        self.assertIsNone(load_settings({VAR: ""}).google_credentials)

    def test_invalid(self):
        for bad in (json.dumps(GOOD), ' {"type": 1}', "[1]", "/tmp/sa.json ", " /tmp/x",
                    "/tmp/a\nb", "/tmp/\x00x", "x" * 4097):
            with self.subTest(n=len(bad)), self.assertRaises(ConfigError) as cm:
                load_settings({VAR: bad})
            self.assertEqual(cm.exception.var, VAR)
            self.assertNotIn("SENTINEL", str(cm.exception))
            self.assertNotIn("/tmp", str(cm.exception))

    def test_repr_hides_the_path(self):
        s = Settings(google_credentials="/SENTINELPATH/sa.json")
        self.assertNotIn("SENTINELPATH", repr(s) + str(s))
        self.assertIn("google_credentials=<set>", repr(s))

    def test_dry_run_still_the_default(self):
        self.assertIs(load_settings({VAR: "/tmp/sa.json"}).dry_run, True)


if __name__ == "__main__":
    unittest.main()
