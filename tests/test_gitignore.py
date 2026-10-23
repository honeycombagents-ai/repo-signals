""".gitignore: the key, build and operating-system patterns, and no tracked file they match."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOOGLE_KEY = "*-" + "[0-9a-f]" * 12 + ".json"
KEY_PATTERNS = ("service-account.json", "*-service-account*.json", GOOGLE_KEY)
OTHER_PATTERNS = ("dist/", "build/", "*.egg-info/", ".DS_Store")


def tracked_matches(pattern):
    """Tracked files that ``pattern`` would ignore, as git itself matches it."""
    r = subprocess.run(["git", "ls-files", "-z", "--cached", "--ignored", f"--exclude={pattern}"],
                       cwd=ROOT, capture_output=True, check=True)
    return [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]


class Patterns(unittest.TestCase):
    def test_each_pattern_is_a_line(self):
        lines = (ROOT / ".gitignore").read_text().splitlines()
        for pattern in KEY_PATTERNS + OTHER_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, lines)

    def test_no_tracked_file_matches_a_key_pattern(self):
        # The check itself must work: a pattern for tracked files finds them.
        self.assertIn("scripts/scan.py", tracked_matches("*.py"))
        for pattern in KEY_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertEqual(tracked_matches(pattern), [])

    def test_example_key_names_are_ignored(self):
        names = ("service-account.json", "keys/prod-service-account-key.json",
                 "example-project-123456-0123456789ab.json")
        r = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], cwd=ROOT,
                           input="\n".join(names) + "\n", capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), list(names))
        r = subprocess.run(["git", "check-ignore", "--no-index", "-q", "package.json"], cwd=ROOT)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
