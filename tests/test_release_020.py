"""Release 0.2.0: the version, the changelog entry above 0.1.1, and the User-Agent of both
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
NEW, OLD = "\n## 0.2.0 (2026-10-27)\n", "\n## 0.1.1 (2026-10-23)\n"
AGENT = f"repo-signals/{release.CURRENT} (+https://honeycombagents.ai)"


class Release020(unittest.TestCase):
    def test_pyproject_and_package_are_0_2_0(self):
        with open(ROOT / "pyproject.toml", "rb") as fh:
            version = tomllib.load(fh)["project"]["version"]
        self.assertEqual(version, release.CURRENT)
        self.assertEqual(__version__, release.CURRENT)

    def test_changelog_has_0_2_0_above_0_1_1(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertIn(NEW, text)
        self.assertLess(text.index(NEW), text.index(OLD))
        self.assertEqual(text.count(NEW), 1)
        entry = text.split(NEW, 1)[1].split("\n## ", 1)[0]
        checked = entry.split("\n### Checked live\n", 1)[1].split("\n### Not checked live\n", 1)
        self.assertEqual(len(checked), 2)
        flat_checked, flat_unchecked = (" ".join(part.split()) for part in checked)
        self.assertIn("On 2026-10-27, anonymously, against the organisation `huggingface`",
                      flat_checked)
        self.assertIn("Nothing else in this release was checked against a real service.",
                      flat_checked)
        for phrase in ("`run --live` for a Hugging Face file, end to end", "429 body",
                       "`Retry-After`", "tokens", "renamed repositories",
                       "user-owned repositories", "unusual characters", "several appends",
                       "Hugging Face's terms"):
            self.assertIn(phrase, flat_unchecked)
        flat = " ".join(entry.split())
        for phrase in ("`source`", "`kinds`", "`modified_within_days`", "`collect --live`",
                       "`hf-probe-org`", "`run --live`", "`REPO_SIGNALS_HF_MAX_CALLS`",
                       "an automated search of public repositories", "This changes GitHub "
                       "drafts too", "`REPO_SIGNALS_SIGNAL_FILE (CODE)`", "cold run"):
            self.assertIn(phrase, flat)
        self.assertNotIn("—", entry)

    def test_changelog_records_the_evidence_to_draft_check(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        entry = text.split(NEW, 1)[1].split("\n## ", 1)[0]
        checked, unchecked = (" ".join(part.split()) for part in entry.split(
            "\n### Checked live\n", 1)[1].split("\n### Not checked live\n", 1))
        for phrase in ("Also on 2026-10-27, anonymously, with no sheet and no people provider",
                       "returned 6 evidence items of kind `hf-model` (2 requests: 1 overview and "
                       "1 listing page)", "The guard accepted both",
                       "each draft contained its evidence summary and URL",
                       "11 columns and an unchanged `signal_evidence` cell",
                       "refused as `no_evidence`", "No Hunter, Apollo or Google calls were made."):
            self.assertIn(phrase, checked)
        self.assertTrue(checked.endswith("Nothing else in this release was checked against a "
                                         "real service."))
        self.assertIn("Collection, drafting and building the sheet row were each checked "
                      "separately (above), but the full run, with a real sheet and a people "
                      "provider, was not.", unchecked)
        self.assertIn("Whether Hunter or Apollo return people for a Hugging Face organisation's "
                      "domain was not checked either.", unchecked)

    def test_user_agent_carries_0_2_0(self):
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
