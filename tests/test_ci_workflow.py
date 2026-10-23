"""The CI workflow: the checkout scans the pull request head, and every action is pinned."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/scan.yml"
REF = "ref: ${{ github.event.pull_request.head.sha || github.sha }}"


def checkout_step(text):
    """The checkout step's lines, from its ``uses:`` line to the next step."""
    start = text.index("- uses: actions/checkout@")
    rest = text[start:].splitlines()
    step = [rest[0]]
    for line in rest[1:]:
        if line.lstrip().startswith("- "):
            break
        step.append(line)
    return [line.strip() for line in step]


class Checkout(unittest.TestCase):
    def test_checkout_uses_the_pr_head_with_full_history_and_no_credentials(self):
        step = checkout_step(WORKFLOW.read_text())
        self.assertIn(REF, step)
        self.assertIn("fetch-depth: 0", step)
        self.assertIn("persist-credentials: false", step)
        comment = step.index(REF) - 1
        self.assertTrue(step[comment].startswith("#"), step)

    def test_every_action_is_pinned_to_a_full_sha(self):
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", WORKFLOW.read_text(), re.M)
        self.assertGreaterEqual(len(uses), 2)
        for ref in uses:
            with self.subTest(uses=ref):
                self.assertRegex(ref, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")

    def test_permissions_stay_read_only(self):
        self.assertIn("\npermissions:\n  contents: read\n", WORKFLOW.read_text())


if __name__ == "__main__":
    unittest.main()
