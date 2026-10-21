"""doctor and config checks for the drafting settings. Offline and synthetic only."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _src  # noqa: F401
from repo_signals import config, doctor
from repo_signals.config import ConfigError, parse_sender_var
from repo_signals.drafting import template as template_mod
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider

SENDER_ENV = {config.SENDER_NAME: "SENTINELNAME A", config.SENDER_ROLE: "SENTINELROLE",
              config.ORG_NAME: "SENTINELORG",
              config.POSTAL_ADDRESS: "SENTINELPOSTAL 1 Street\nExample City",
              config.OPTOUT_TEXT: "SENTINELOPTOUT reply stop"}


def found(name):
    return "/opt/synthetic/bin/" + name


class DoctorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="SENTINELPATH-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def suppression(self, text):
        path = os.path.join(self.dir, "suppression.txt")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def doctor(self, env):
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        code = doctor.run(environ=env, registry=reg, out=out, which=found)
        text = out.getvalue()
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn(self.dir, text)
        self.assertNotIn("Example City", text)
        self.assertNotIn("blake", text)
        return code, text


class Sender(DoctorCase):
    def test_all_set(self):
        code, out = self.doctor(dict(SENDER_ENV))
        self.assertEqual(code, 0, out)
        for var in config.SENDER_VARS:
            self.assertIn(f"PASS {var} is set", out)

    def test_each_missing_warns_by_name(self):
        for var in config.SENDER_VARS:
            with self.subTest(var=var):
                env = {k: v for k, v in SENDER_ENV.items() if k != var}
                code, out = self.doctor(env)
                self.assertEqual(code, 0, out)
                self.assertIn(f"WARN {var} is missing (drafting refuses without it)", out)

    def test_malformed_fails_without_value(self):
        for var, bad in ((config.SENDER_NAME, "SENTINELNAME\nX"),
                         (config.SENDER_ROLE, "SENTINELROLE "),
                         (config.POSTAL_ADDRESS, "\n".join(["SENTINELPOSTAL"] * 7)),
                         (config.OPTOUT_TEXT, "SENTINELOPTOUT\x07")):
            with self.subTest(var=var):
                code, out = self.doctor({**SENDER_ENV, var: bad})
                self.assertEqual(code, 1, out)
                self.assertIn(f"FAIL {var} must be", out)


class SuppressionFile(DoctorCase):
    def test_missing_warns(self):
        _, out = self.doctor({})
        self.assertIn(f"WARN {config.SUPPRESSION_FILE} is missing", out)

    def test_counts_only(self):
        path = self.suppression("# synthetic\n" + "blake" + "@example.invalid\n"
                                "casey" + "@example.invalid\nblocked.example.com\n")
        code, out = self.doctor({config.SUPPRESSION_FILE: path})
        self.assertEqual(code, 0, out)
        self.assertIn(f"PASS {config.SUPPRESSION_FILE} file parses: 2 email(s), 1 domain(s)", out)
        self.assertNotIn("blocked", out)

    def test_problems_fail(self):
        cases = [("SENTINEL not valid\n", "suppression_malformed_line_1"),
                 (None, "suppression_unreadable")]
        for text, code_name in cases:
            with self.subTest(code=code_name):
                path = os.path.join(self.dir, "nope.txt") if text is None \
                    else self.suppression(text)
                code, out = self.doctor({config.SUPPRESSION_FILE: path})
                self.assertEqual(code, 1, out)
                self.assertIn(f"FAIL {config.SUPPRESSION_FILE} file is not usable ({code_name})",
                              out)
        code, out = self.doctor({config.SUPPRESSION_FILE: "SENTINELPATH\n"})
        self.assertEqual(code, 1)


class Templates(DoctorCase):
    def test_present(self):
        _, out = self.doctor({})
        self.assertIn("PASS drafting templates present and valid (intro, follow_up)", out)

    def test_missing_or_invalid_fails(self):
        with open(os.path.join(self.dir, "intro.txt"), "w") as fh:
            fh.write("Subject: {first_name}\n\nno placeholders\n")
        with mock.patch.object(template_mod, "TEMPLATE_DIR", __import__("pathlib").Path(self.dir)):
            code, out = self.doctor({})
        self.assertEqual(code, 1)
        self.assertIn("FAIL drafting templates missing or invalid: intro, follow_up", out)


class Offline(DoctorCase):
    def test_no_network_and_cli_prints_no_value(self):
        path = self.suppression("blocked.example.com\n")
        env = {**SENDER_ENV, config.SUPPRESSION_FILE: path}
        with mock.patch("socket.socket", side_effect=AssertionError("socket")), \
                mock.patch("urllib.request.OpenerDirector.open",
                           side_effect=AssertionError("network")):
            code, _ = self.doctor(env)
        self.assertEqual(code, 0)
        r = subprocess.run([sys.executable, "-m", "repo_signals", "doctor"],
                           env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_src.SRC),
                                "PYTHONDONTWRITEBYTECODE": "1", **env},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"PASS {config.OPTOUT_TEXT} is set", r.stdout)
        self.assertNotIn("SENTINEL", r.stdout + r.stderr)
        self.assertNotIn("Example City", r.stdout + r.stderr)


class ConfigSender(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_sender_var(SENDER_ENV, config.POSTAL_ADDRESS),
                         "SENTINELPOSTAL 1 Street\nExample City")
        self.assertIsNone(parse_sender_var({}, config.SENDER_NAME))
        self.assertIsNone(parse_sender_var({config.SENDER_NAME: "  "}, config.SENDER_NAME))

    def test_invalid_errors_name_the_variable_only(self):
        cases = [(config.SENDER_NAME, "SENTINEL\nX"), (config.SENDER_NAME, "S" * 201),
                 (config.ORG_NAME, " SENTINEL"), (config.POSTAL_ADDRESS, "SENTINEL\n\nX"),
                 (config.POSTAL_ADDRESS, "S" * 501), (config.OPTOUT_TEXT, "SENTINEL\r\nX"),
                 (config.SENDER_ROLE, "SENTINEL\x00")]
        for var, bad in cases:
            with self.subTest(var=var, n=len(bad)), self.assertRaises(ConfigError) as cm:
                parse_sender_var({var: bad}, var)
            self.assertEqual(cm.exception.var, var)
            self.assertNotIn("SENTINEL", str(cm.exception))
        with self.assertRaises(ValueError):
            parse_sender_var({}, "HOME")

    def test_suppression_path(self):
        self.assertEqual(config.parse_suppression_file({config.SUPPRESSION_FILE: "/tmp/s.txt"}),
                         "/tmp/s.txt")
        for bad in ("example.com\nother.example.com", " /tmp/s", '["x"]'):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as cm:
                config.parse_suppression_file({config.SUPPRESSION_FILE: bad})
            self.assertNotIn("example.com", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
