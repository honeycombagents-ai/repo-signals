"""Small fixes from the audit: the doctor dry-run line, the dead collect branch, the setup guide
plan wording, and the CONTRIBUTING guide."""
import inspect
import re
import unittest
from pathlib import Path

import _src  # noqa: F401
from repo_signals import doctor
from repo_signals.collector import dry_run
from repo_signals.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def flat(name):
    return " ".join((ROOT / name).read_text().split())


class DoctorLine(unittest.TestCase):
    def test_dry_run_off_line_names_every_live_command(self):
        [(status, message)] = doctor.check_dry_run(Settings(dry_run=False))
        self.assertEqual(status, "WARN")
        self.assertEqual(message, "dry run is off: probe-org, collect --live and run --live can "
                                  "make GitHub requests")


class DeadBranch(unittest.TestCase):
    def test_collect_dry_run_has_no_live_branch(self):
        self.assertFalse(hasattr(dry_run, "LIVE_NOT_IMPLEMENTED"))
        self.assertNotIn("dry_run", inspect.signature(dry_run.run).parameters)
        self.assertNotIn("not implemented yet", (ROOT / "src/repo_signals/collector/dry_run.py")
                         .read_text())


class SetupGuide(unittest.TestCase):
    def test_plan_wording_matches_the_plan_line(self):
        guide = flat("docs/setup-guide.md")
        self.assertNotIn("most calls each service could use", guide)
        self.assertIn("a minimum of 6 calls for the header check and one append, plus one more "
                      "per extra append, with `REPO_SIGNALS_SHEETS_MAX_CALLS` as the hard limit",
                      guide)


class Contributing(unittest.TestCase):
    def test_contributing_has_the_commands_and_the_rules(self):
        text = (ROOT / "CONTRIBUTING.md").read_text()
        for command in ("python3 -m unittest discover -s tests\n",
                        "python3 scripts/scan.py --history\n"):
            self.assertIn(command, text)
        words = " ".join(text.split())
        for phrase in ("private repository secret", "Pull requests from forks do not get that "
                       "secret", "small", "Nothing may send email",
                       "Nothing may delete, clear or overwrite sheet data",
                       "No live calls in tests", "fast-forward"):
            self.assertIn(phrase, words)
        self.assertIsNone(re.search(r"/Users/|/home/|\\\\Users\\\\", text))
        self.assertNotIn("—", text)

    def test_readme_links_contributing(self):
        self.assertIn("[CONTRIBUTING.md](CONTRIBUTING.md)", (ROOT / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
