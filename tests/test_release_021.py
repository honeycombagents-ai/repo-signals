"""Release 0.2.1: the version, the changelog entry above 0.2.0, and the User-Agent of both
collectors (GitHub and Hugging Face). Offline: fake transports only."""
import tomllib
import unittest
from pathlib import Path

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_hf_live as hf_fx
import test_release as release
import test_run as run_fx
from repo_signals import __version__
from repo_signals.budget import CallBudget
from repo_signals.collector import hf_api
from repo_signals.collector.github_api import ALLOWED_HOSTS, GitHubClient
from repo_signals.collector.hf_api import HuggingFaceClient
from repo_signals.http import HttpClient

ROOT = Path(__file__).resolve().parent.parent
NEW, OLD = "\n## 0.2.1 (2026-10-27)\n", "\n## 0.2.0 (2026-10-27)\n"
AGENT = f"repo-signals/{release.CURRENT} (+https://honeycombagents.ai)"


class Release021(unittest.TestCase):
    def test_pyproject_and_package_are_0_2_1(self):
        with open(ROOT / "pyproject.toml", "rb") as fh:
            version = tomllib.load(fh)["project"]["version"]
        self.assertEqual(version, release.CURRENT)
        self.assertEqual(__version__, release.CURRENT)

    def test_changelog_has_0_2_1_above_0_2_0(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertEqual(text.count(NEW), 1)
        self.assertLess(text.index(NEW), text.index(OLD))
        self.assertTrue(text.startswith("# Changelog\n"))
        entry = " ".join(text.split(NEW, 1)[1].split("\n## ", 1)[0].split())
        for phrase in ("no longer warns that `REPO_SIGNALS_GITHUB_TOKEN` is missing",
                       "`source = \"huggingface\"`", "A malformed token still fails.",
                       "optional and for GitHub signal files only",
                       "Nothing in this release was checked against a real service.",
                       "The full `run --live` for a Hugging Face file against a real sheet",
                       "Hunter or Apollo returning people for a Hugging Face organisation's "
                       "domain, are still unchecked."):
            self.assertIn(phrase, entry)
        self.assertNotIn("—", entry)

    def test_user_agent_carries_0_2_1(self):
        fake = gh_fx.FakeGitHub(run_fx.ORGS, run_fx.REPOS)
        client = GitHubClient(HttpClient(ALLOWED_HOSTS, transport=fake), CallBudget(1),
                              dry_run=False)
        client.get_org("example-org")
        self.assertEqual(fake.calls[0]["headers"]["User-Agent"], AGENT)
        hf = hf_fx.FakeHF()
        client = HuggingFaceClient(HttpClient(hf_api.ALLOWED_HOSTS, transport=hf), CallBudget(1),
                                   dry_run=False)
        client.get_org_overview("example-org")
        self.assertEqual(hf.calls[0]["headers"]["User-Agent"], AGENT)


if __name__ == "__main__":
    unittest.main()
