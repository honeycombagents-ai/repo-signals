"""Docs fixes from the audit: setup steps, the adapters guide, the probe-org claim, live-check dates."""
import re
import unittest
from pathlib import Path

import _src  # noqa: F401
from repo_signals.collector.github_fields import WEB_HOST

ROOT = Path(__file__).resolve().parent.parent
CLONE = f"git clone https://{WEB_HOST}/honeycombagents-ai/repo-signals.git"
STEPS = (CLONE, "cd repo-signals", "python3 -m venv .venv", "source .venv/bin/activate",
         "python3 -m pip install -e .")


def flat(name):
    return " ".join((ROOT / name).read_text().split())


class SetupSteps(unittest.TestCase):
    def test_clone_and_venv_come_before_pip_install(self):
        for name in ("README.md", "docs/setup-guide.md"):
            text = (ROOT / name).read_text()
            with self.subTest(file=name):
                for step in STEPS:
                    self.assertIn(step + "\n", text)
                positions = [text.index(step + "\n") for step in STEPS]
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(text.index("pip install"),
                                 text.index(STEPS[-1]) + len("python3 -m "))


class AdaptersGuide(unittest.TestCase):
    def test_library_use_and_fork_and_edit(self):
        text = flat("docs/adapters.md")
        self.assertIn("cannot load a provider registered in your own script", text)
        self.assertIn("fork and edit", text)
        self.assertIn("## Replacing the sheet", (ROOT / "docs/adapters.md").read_text())
        self.assertIn("SheetWriter._new_only", text)

    def test_no_provider_export_example(self):
        text = (ROOT / "docs/adapters.md").read_text()
        self.assertNotIn("export REPO_SIGNALS_PROVIDER", text)
        self.assertNotIn("export EXAMPLE_DIRECTORY_KEY", text)


class ProbeOrgClaim(unittest.TestCase):
    SENTENCE = ('`probe-org` makes one real GitHub request with only dry run turned off: it needs '
                'no terminal and no typed "yes".')

    def test_changelog_says_probe_org_needs_no_typed_yes(self):
        text = flat("CHANGELOG.md")
        self.assertIn(self.SENTENCE, text)
        self.assertNotIn("every live command needs", text)

    def test_readme_says_probe_org_needs_no_typed_yes(self):
        text = flat("README.md")
        self.assertIn("it needs no terminal and no typed \"yes\"", text)
        self.assertNotIn("every live command is gated", text)


class LiveCheckDates(unittest.TestCase):
    def test_github_date_agrees_everywhere(self):
        code = (ROOT / "src/repo_signals/collector/github_api.py").read_text()
        doc = flat("docs/github-client.md")
        changelog = flat("CHANGELOG.md")
        date = re.search(r"Checked live on (\d{4}-\d{2}-\d{2})", code).group(1)
        self.assertIn(f"Checked live on {date}", doc)
        self.assertIn(f"**GitHub collect ({date}):**", changelog)

    def test_no_unverified_label_on_checked_constants_blocks(self):
        for name in ("src/repo_signals/collector/github_api.py",
                     "src/repo_signals/sheets/google_api.py"):
            with self.subTest(file=name):
                self.assertNotIn("UNVERIFIED", (ROOT / name).read_text())
        self.assertIn("Checked live on 2026-10-22:", (ROOT / "docs/google-sheets.md").read_text())


if __name__ == "__main__":
    unittest.main()
