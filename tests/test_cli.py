import os
import subprocess
import sys
import unittest

import _src  # noqa: F401
from repo_signals.cli import main

GOOD_SHEET = "S" * 10 + "_-" + "9" * 20
GOOD_TOKEN = "t0k" + "x" * 30


def cli(*args, **env):
    r = subprocess.run([sys.executable, "-m", "repo_signals", *args],
                       env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_src.SRC),
                            "PYTHONDONTWRITEBYTECODE": "1", **env},
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


class ExitCodes(unittest.TestCase):
    def test_doctor_clean_exits_0(self):
        code, out, err = cli("doctor", REPO_SIGNALS_SHEET_ID=GOOD_SHEET,
                             REPO_SIGNALS_GITHUB_TOKEN=GOOD_TOKEN)
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 fail", out)

    def test_doctor_warnings_only_exits_0(self):
        code, out, _ = cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn(" warn, 0 fail", out)  # the warn count depends on openssl being on PATH

    def test_doctor_failure_exits_1(self):
        for env in ({"REPO_SIGNALS_MAX_CALLS": "0"}, {"REPO_SIGNALS_PROVIDER": "nosuch"}):
            with self.subTest(env=env):
                code, out, _ = cli("doctor", **env)
                self.assertEqual(code, 1, out)

    def test_online_keeps_exit_code(self):
        self.assertEqual(cli("doctor", "--online")[0], 0)
        self.assertEqual(cli("doctor", "--online", REPO_SIGNALS_MAX_CALLS="0")[0], 1)

    def test_no_command_is_usage_error(self):
        code, _, err = cli()
        self.assertEqual(code, 2)
        self.assertIn("usage", err)

    def test_main_returns_int(self):
        env_backup = dict(os.environ)
        try:
            for k in list(os.environ):
                if k.startswith("REPO_SIGNALS_"):
                    del os.environ[k]
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["doctor"]), 0)
        finally:
            os.environ.clear()
            os.environ.update(env_backup)


if __name__ == "__main__":
    unittest.main()
