"""The Hunter limit check of 2026-10-23 is recorded, and MAX_LIMIT itself is unchanged."""
import unittest
from pathlib import Path

import _src  # noqa: F401
from repo_signals.providers import hunter
from repo_signals.run import PEOPLE_LIMIT

ROOT = Path(__file__).resolve().parent.parent


def flat(name):
    return " ".join((ROOT / name).read_text().split())


class LimitCheck(unittest.TestCase):
    def test_max_limit_and_the_run_limit_are_unchanged(self):
        self.assertEqual(hunter.MAX_LIMIT, 100)
        self.assertEqual(PEOPLE_LIMIT, 3)

    def test_hunter_doc_records_the_check_and_the_refusal(self):
        doc = flat("docs/hunter.md")
        checked = doc.split("Checked live on 2026-10-23 (one account, domain search):", 1)[1]
        checked = checked.split("Still unchecked", 1)[0]
        self.assertIn("a limit of 3 returned 3 people and a limit of 10 returned 10", checked)
        self.assertIn("a limit of 100 was refused with HTTP 400", checked)
        unchecked = doc.split("Still unchecked", 1)[1]
        self.assertIn("between 10 and 99", unchecked)
        self.assertIn("whether a larger limit costs more", unchecked)
        self.assertNotIn("The request asks for at most 100", doc)
        self.assertIn("one account saw a limit of 100 refused with HTTP 400 on 2026-10-23", doc)

    def test_constants_comment_and_changelog_record_the_refusal(self):
        code = flat("src/repo_signals/providers/hunter.py")
        self.assertIn("limit=100 was refused with HTTP 400", code)
        self.assertIn("an unchecked upper bound, not a working value", code)
        changelog = flat("CHANGELOG.md").split("## 0.1.0", 1)[0]
        self.assertIn("a limit of 100 was refused with HTTP 400", changelog)


if __name__ == "__main__":
    unittest.main()
