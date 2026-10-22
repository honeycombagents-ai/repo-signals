"""Release 0.1.0: one version everywhere, the changelog, and the GitHub User-Agent."""
import contextlib
import io
import re
import tomllib
import unittest
from pathlib import Path

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_run as run_fx
from repo_signals import __version__
from repo_signals.budget import CallBudget
from repo_signals.cli import main
from repo_signals.collector.github_api import ALLOWED_HOSTS, GitHubClient
from repo_signals.http import HttpClient

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.1.0"


class Version(unittest.TestCase):
    def test_pyproject_matches_package(self):
        with open(ROOT / "pyproject.toml", "rb") as fh:
            project = tomllib.load(fh)["project"]
        self.assertEqual(project["version"], __version__)
        self.assertEqual(__version__, VERSION)

    def test_cli_reports_the_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"repo-signals {VERSION}")

    def test_github_user_agent_carries_the_version(self):
        fake = gh_fx.FakeGitHub(run_fx.ORGS, run_fx.REPOS)
        client = GitHubClient(HttpClient(ALLOWED_HOSTS, transport=fake), CallBudget(1),
                              dry_run=False)
        client.get_org("example-org")
        self.assertEqual(fake.calls[0]["headers"]["User-Agent"],
                         f"repo-signals/{VERSION} (+https://honeycombagents.ai)")


class Changelog(unittest.TestCase):
    def test_entry_and_headings(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertTrue(text.startswith("# Changelog\n"))
        self.assertIn(f"\n## {VERSION} (2026-10-22)\n", text)
        entry = text.split(f"\n## {VERSION} (2026-10-22)\n", 1)[1].split("\n## ", 1)[0]
        positions = [entry.index(f"\n### {h}\n") for h in ("Checked live", "Not checked live",
                                                            "Not built")]
        self.assertEqual(positions, sorted(positions))
        flat = " ".join(entry.split())
        self.assertIn("Nothing is ever sent by email", flat)
        self.assertIn("Dry run is the default", flat)
        self.assertNotIn("\u2014", text)

    def test_readme_links_the_changelog(self):
        readme = (ROOT / "README.md").read_text()
        status, rest = readme.split("\n## What it is\n", 1)
        self.assertIn("[CHANGELOG.md](CHANGELOG.md)", status)

    def test_forbidden_keyword_absent(self):
        word = re.compile("lam" + "bda", re.IGNORECASE)
        for name in ("CHANGELOG.md", "README.md", "pyproject.toml", "src/repo_signals/__init__.py",
                     "tests/test_release.py"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))


if __name__ == "__main__":
    unittest.main()
