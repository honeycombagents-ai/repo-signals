"""Release 0.2.2: the version, the changelog entry above 0.2.1 with both live notes, and the
User-Agent of both collectors (GitHub and Hugging Face). Offline: fake transports only."""
import tomllib
import unittest
from pathlib import Path

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_hf_live as hf_fx
import test_run as run_fx
from repo_signals import __version__
from repo_signals.budget import CallBudget
from repo_signals.collector import hf_api
from repo_signals.collector.github_api import ALLOWED_HOSTS, GitHubClient
from repo_signals.collector.hf_api import HuggingFaceClient
from repo_signals.http import HttpClient

ROOT = Path(__file__).resolve().parent.parent
NEW, OLD = "\n## 0.2.2 (2026-10-27)\n", "\n## 0.2.1 (2026-10-27)\n"
AGENT = "repo-signals/0.2.2 (+https://honeycombagents.ai)"


class Release022(unittest.TestCase):
    def test_pyproject_and_package_are_0_2_2(self):
        with open(ROOT / "pyproject.toml", "rb") as fh:
            version = tomllib.load(fh)["project"]["version"]
        self.assertEqual(version, "0.2.2")
        self.assertEqual(__version__, "0.2.2")

    def test_changelog_has_0_2_2_above_0_2_1_with_both_live_notes(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertEqual(text.count(NEW), 1)
        self.assertLess(text.index(NEW), text.index(OLD))
        entry = text.split(NEW, 1)[1].split("\n## ", 1)[0]
        self.assertIn('"Hugging Face use" note in the README', " ".join(entry.split()))
        parts = entry.split("\n### Checked live\n", 1)[1].split("\n### Not checked live\n", 1)
        self.assertEqual(len(parts), 2)
        checked, unchecked = (" ".join(p.split()) for p in parts)
        for phrase in ("On 2026-10-27, one full `run --live` for a Hugging Face file",
                       "against the organisation `huggingface`, models only, one listing page",
                       "(2 requests: 1 overview and 1 listing page)",
                       "Hunter as the people provider (3 people found for the `huggingface.co` "
                       "domain)", "the sheet preflight passed on an empty sheet",
                       "3 rows were appended",
                       "huggingface 2 of 5, provider 1 of 1, sheets 6 of 6",
                       "The run ended complete, with nothing sent."):
            self.assertIn(phrase, checked)
        for phrase in ("duplicate skip on a real sheet", "the run was not repeated",
                       "Apollo as the people provider", "Datasets and Spaces through "
                       "`run --live`", "`collect --live` only", "429 body", "`Retry-After`",
                       "tokens", "renamed repositories", "user-owned repositories",
                       "unusual characters", "several appends against a real sheet"):
            self.assertIn(phrase, unchecked)
        self.assertNotIn("—", entry)

    def test_user_agent_carries_0_2_2(self):
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
