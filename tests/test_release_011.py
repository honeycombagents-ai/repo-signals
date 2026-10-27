"""Release 0.1.1: the version, the changelog entry above 0.1.0, and the GitHub User-Agent."""
import tomllib
import unittest
from pathlib import Path

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_release as release
import test_run as run_fx
from repo_signals import __version__
from repo_signals.budget import CallBudget
from repo_signals.collector.github_api import ALLOWED_HOSTS, GitHubClient
from repo_signals.http import HttpClient

ROOT = Path(__file__).resolve().parent.parent


class Release011(unittest.TestCase):
    def test_pyproject_and_package_agree_on_0_1_1(self):
        with open(ROOT / "pyproject.toml", "rb") as fh:
            version = tomllib.load(fh)["project"]["version"]
        self.assertEqual(version, __version__)
        self.assertEqual(__version__, release.CURRENT)

    def test_changelog_has_0_1_1_above_0_1_0(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        new, old = "\n## 0.1.1 (2026-10-23)\n", "\n## 0.1.0 (2026-10-22)\n"
        self.assertIn(new, text)
        self.assertLess(text.index(new), text.index(old))
        self.assertNotIn("## Unreleased", text)
        entry = text.split(new, 1)[1].split("\n## ", 1)[0]
        self.assertIn("\n### Not checked live\n", entry)
        flat = " ".join(entry.split())
        self.assertIn("Only the Hunter limit check above was made against a real service.", flat)
        self.assertIn("not against a real sheet", flat)

    def test_github_user_agent_carries_0_1_1(self):
        fake = gh_fx.FakeGitHub(run_fx.ORGS, run_fx.REPOS)
        client = GitHubClient(HttpClient(ALLOWED_HOSTS, transport=fake), CallBudget(1),
                              dry_run=False)
        client.get_org("example-org")
        self.assertEqual(fake.calls[0]["headers"]["User-Agent"],
                         f"repo-signals/{release.CURRENT} (+https://honeycombagents.ai)")


if __name__ == "__main__":
    unittest.main()
