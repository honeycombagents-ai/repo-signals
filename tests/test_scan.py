"""Tests for scripts/scan.py.

Every secret-shaped value is assembled at test time from fragments, so no
real or realistic value is ever committed to the tree.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCAN = Path(__file__).resolve().parent.parent / "scripts" / "scan.py"
IDENTITY = ["-c", "user.name=honeycombagents.ai", "-c", "user.email=noreply@honeycombagents.ai",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


def j(*parts):
    return "".join(parts)


# One synthetic fixture per generic check: (kind printed by the scanner, line).
ALNUM36 = "Q7" * 18
FIXTURES = [
    ("private key block", j("-----BEGIN ", "RSA ", "PRIVATE", " KEY-----")),
    ("GitHub token (ghp_)", j("token = ", "gh", "p_", ALNUM36)),
    ("GitHub token (github_pat_)", j("token = ", "github", "_pat_", "11AB", "c" * 30)),
    ("GitHub token (gho_)", j("token = ", "gh", "o_", ALNUM36)),
    ("Slack token (xox*)", j("hook = ", "xo", "xb-", "1234567890-abcdefghij")),
    ("AWS access key id", j("key = ", "AK", "IA", "Z" * 8, "7" * 8)),
    ("sk- style API key", j("key = ", "s", "k-", "ant-", "x" * 30)),
    ("AWS ARN", j("role = ", "ar", "n:aws", ":iam::role/fixture")),
    ("AWS account id", j("account: ", "1" * 6, "9" * 6)),
    ("Salesforce id", j("record ", "00", "1", "5g00000", "ABCdeAAG")),
    ("email address", j("contact ", "someone", "@", "example", ".com")),
]


class Repo:
    """A throwaway git repository with optional commits."""

    def __init__(self, tmp):
        self.path = Path(tmp)
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        subprocess.run(["git", *IDENTITY, *args], cwd=self.path, check=True,
                       capture_output=True)

    def write(self, name, text):
        p = self.path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.git("add", name)

    def commit(self, msg):
        self.git("commit", "-q", "-m", msg)

    def remove(self, name):
        self.git("rm", "-q", name)

    def scan(self, *args, deny=None):
        env = {k: v for k, v in os.environ.items() if k != "DENYLIST_PATTERNS"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if deny is not None:
            env["DENYLIST_PATTERNS"] = deny
        r = subprocess.run([sys.executable, str(SCAN), *args], cwd=self.path, env=env,
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class GenericChecks(ScanTestCase):
    def test_each_generic_check_catches_its_kind(self):
        for kind, line in FIXTURES:
            with self.subTest(kind=kind):
                self.repo.write("f.txt", f"clean line\n{line}\n")
                code, out = self.repo.scan()
                self.assertEqual(code, 1, out)
                self.assertIn(f"f.txt:2: {kind}", out)

    def test_each_fixture_triggers_only_its_own_kind(self):
        # Guards against one check masking the removal of another.
        for kind, line in FIXTURES:
            with self.subTest(kind=kind):
                self.repo.write("f.txt", line + "\n")
                _, out = self.repo.scan()
                hits = [l for l in out.splitlines() if l.startswith("f.txt:")]
                self.assertEqual(hits, [f"f.txt:1: {kind}"])

    def test_account_id_needs_context(self):
        self.repo.write("f.txt", j("order number ", "1" * 6, "9" * 6, "\n"))
        code, out = self.repo.scan()
        self.assertEqual(code, 0, out)

    def test_arn_context_flags_account_id(self):
        self.repo.write("f.txt", j("ar", "n prefix ", "4" * 12, "\n"))
        _, out = self.repo.scan()
        self.assertIn("f.txt:1: AWS account id", out)


class EmailRule(ScanTestCase):
    def test_allowed_noreply_passes(self):
        self.repo.write("f.txt", "git identity noreply@honeycombagents.ai\n")
        code, out = self.repo.scan()
        self.assertEqual(code, 0, out)

    def test_other_address_on_same_domain_fails(self):
        self.repo.write("f.txt", j("hello", "@", "honeycombagents", ".ai\n"))
        code, out = self.repo.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("f.txt:1: email address", out)

    def test_lookalike_addresses_fail(self):
        for addr in (j("x", "noreply", "@", "honeycombagents.ai"),
                     j("noreply", "@", "honeycombagents.ai", ".example.com"),
                     j("noreply", "@", "sub.", "honeycombagents.ai")):
            with self.subTest(addr=addr):
                self.repo.write("f.txt", f"id {addr}\n")
                code, out = self.repo.scan()
                self.assertEqual(code, 1, out)
                self.assertIn("email address", out)

    def test_allowed_next_to_disallowed_still_fails(self):
        self.repo.write("f.txt", j("noreply@honeycombagents.ai and ", "a", "@", "example.org\n"))
        code, out = self.repo.scan()
        self.assertEqual(code, 1, out)


class DenyList(ScanTestCase):
    def test_denylist_from_env_triggers(self):
        self.repo.write("f.txt", "this mentions Forbidden-Word here\n")
        code, out = self.repo.scan(deny="unrelated\nforbidden-word\n")
        self.assertEqual(code, 1, out)
        self.assertIn("f.txt:1: deny-list pattern #2", out)

    def test_denylist_is_case_insensitive_regex(self):
        self.repo.write("f.txt", "Alpha-42\n")
        code, out = self.repo.scan(deny=r"alpha-\d+")
        self.assertEqual(code, 1, out)

    def test_unset_denylist_prints_one_notice(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit("one")
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("DENYLIST_PATTERNS is not set"), 1, out)

    def test_set_denylist_prints_no_notice(self):
        self.repo.write("a.txt", "clean\n")
        code, out = self.repo.scan(deny="nomatch")
        self.assertEqual(code, 0, out)
        self.assertNotIn("is not set", out)

    def test_invalid_pattern_fails_without_echoing_it(self):
        self.repo.write("a.txt", "clean\n")
        code, out = self.repo.scan(deny="SENTINELPAT(")
        self.assertEqual(code, 1, out)
        self.assertNotIn("SENTINELPAT", out)


class NeverPrintsMatchedText(ScanTestCase):
    def test_output_never_contains_matched_text(self):
        sentinel_lines = [line for _, line in FIXTURES]
        self.repo.write("f.txt", "\n".join(sentinel_lines) + "\n")
        self.repo.commit("fixtures")
        code, out = self.repo.scan("--history", deny="zzsentinelword")
        self.assertEqual(code, 1)
        for _, line in FIXTURES:
            token = line.split(" ", 1)[-1] if " " in line else line
            with self.subTest(token=token[:12]):
                self.assertNotIn(token, out)

    def test_denylist_match_not_printed(self):
        self.repo.write("f.txt", "prefix ZZSENTINELWORD suffix\n")
        _, out = self.repo.scan(deny="zzsentinelword")
        self.assertNotIn("ZZSENTINELWORD", out)
        self.assertNotIn("zzsentinelword", out.lower().replace("deny-list pattern", ""))


class History(ScanTestCase):
    def test_clean_tree_passes(self):
        self.repo.write("README.md", "# fine\n\nnothing to see\n")
        self.repo.commit("init")
        for args in ((), ("--history",)):
            with self.subTest(args=args):
                code, out = self.repo.scan(*args)
                self.assertEqual(code, 0, out)
                self.assertIn("scan: 0 hit(s)", out)

    def test_history_catches_a_removed_secret(self):
        self.repo.write("keep.txt", "fine\n")
        self.repo.write("leak.txt", "a\n" + FIXTURES[5][1] + "\n")
        self.repo.commit("add")
        self.repo.remove("leak.txt")
        self.repo.commit("remove")
        code, out = self.repo.scan()
        self.assertEqual(code, 0, out)  # the tree is clean now
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"commit [0-9a-f]{12} leak\.txt:2: AWS access key id")

    def test_history_reports_line_numbers_of_later_hunks(self):
        self.repo.write("f.txt", "one\ntwo\nthree\n")
        self.repo.commit("base")
        self.repo.write("f.txt", "one\ntwo\nthree\n" + FIXTURES[10][1] + "\n")
        self.repo.commit("append")
        _, out = self.repo.scan("--history")
        self.assertIn("f.txt:4: email address", out)

    def test_history_without_commits_is_clean(self):
        self.repo.write("a.txt", "clean\n")
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
