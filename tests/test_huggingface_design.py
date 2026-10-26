"""The Hugging Face source design doc: present, structured, and honest about what is checked."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs/huggingface-design.md"
HEADINGS = ("# Design: a Hugging Face source for the collector", "## 1. Goal and non-goals",
            "## 2. What does not change", "## 3. The signal file change",
            "## 4. Matching a company to an organisation", "## 5. The signals",
            "## 6. Evidence output", "## 7. Request plan and budget",
            "## 8. Errors and sticky stops", "## 9. Privacy",
            "## 10. Checked and unchecked API details", "## 11. Open questions",
            "## 12. Build plan (small reviewed PRs, dry run first)")
UNCHECKED = ("the 429 body and `Retry-After`", "gated and private behaviour",
             "the token quota and scope", "redirects for renamed repositories",
             "user-owned repositories", "the exact search tokenisation",
             "Hugging Face's terms for automated access and for outreach use")


def flat(text):
    return " ".join(text.split())


class HuggingFaceDesign(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text()
        self.flat = flat(self.text)

    def test_exists_with_its_headings_in_order(self):
        lines = self.text.splitlines()
        positions = [lines.index(h) for h in HEADINGS]
        self.assertEqual(positions, sorted(positions))

    def test_checked_list(self):
        checked = self.flat.split("Checked live on 2026-10-26 (anonymous, read-only, org "
                                  "`huggingface`):", 1)[1].split("Still unchecked", 1)[0]
        self.assertEqual(checked.count("- [x]"), 8)
        for phrase in ("no website, blog or domain field", "anonymous reads work",
                       "Search ignores case but is not a substring match",
                       "silently capped at 1,000", "an unknown or private repository returns 401",
                       "500 requests per 300-second fixed window",
                       "`/datasets/ORG/NAME`, `/spaces/ORG/NAME`"):
            self.assertIn(phrase, checked)

    def test_unchecked_list(self):
        unchecked = self.text.split("Still unchecked, so check each of these before relying on it:",
                                    1)[1].split("\n## ", 1)[0]
        items = [line[6:] for line in unchecked.splitlines() if line.startswith("- [ ] ")]
        self.assertEqual(items, list(UNCHECKED))

    def test_source_defaults_to_github_and_no_website(self):
        self.assertIn("`source` defaults to `github`", self.flat)
        self.assertIn("A file without `source` is a GitHub file, so existing files are unchanged.",
                      self.flat)
        self.assertIn("It has **no website, blog or domain field**", self.flat)
        self.assertIn("every target needs both an `org` and `confirm_org = true`", self.flat)

    def test_plain_prose_and_no_private_detail(self):
        self.assertNotIn("—", self.text)
        self.assertIsNone(re.search(r"/Users/|/home/|~/", self.text))
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", self.text))

    def test_linked_from_the_collector_design(self):
        self.assertIn("[huggingface-design.md](huggingface-design.md)",
                      (ROOT / "docs/collector-design.md").read_text())


if __name__ == "__main__":
    unittest.main()
