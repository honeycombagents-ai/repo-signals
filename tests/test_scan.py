"""Tests for scripts/scan.py.

Every secret-shaped value is assembled at test time from fragments, so no
real or realistic value is ever committed to the tree.
"""
import os
import re
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

    def write_bytes(self, name, data):
        (self.path / name).write_bytes(data)
        self.git("add", name)

    def commit(self, msg, *extra):
        self.git("commit", "-q", "-m", msg, *extra)

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


TOKEN = FIXTURES[1][1].split(" = ", 1)[1]   # a synthetic token-shaped string
TOKEN_KIND = FIXTURES[1][0]


class HistoryByStructure(ScanTestCase):
    """History is read by structure: no content line, file name or ref can hide a token."""

    def leak_then_remove(self, name, data):
        if isinstance(data, bytes):
            self.repo.write_bytes(name, data)
        else:
            self.repo.write(name, data)
        self.repo.commit("add")
        self.repo.remove(name)
        self.repo.commit("remove")

    def history(self):
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 1, out)
        self.assertNotIn(TOKEN, out)
        return out

    def test_added_line_starting_with_plus_plus_does_not_hide_a_later_token(self):
        self.repo.write("keep.txt", "fine\n")
        self.leak_then_remove("f.txt", f"++ looks like a file header\nclean\n{TOKEN}\n")
        self.assertEqual(self.repo.scan()[0], 0)
        self.assertRegex(self.history(),
                         rf"commit [0-9a-f]{{12}} f\.txt:3: {re.escape(TOKEN_KIND)}")

    def test_file_names_git_would_quote_are_scanned(self):
        self.repo.write("keep.txt", "fine\n")
        names = ('dq"uote.txt', "ta\tb.txt", "back\\slash.txt")
        for name in names:
            self.repo.write(name, f"clean\n{TOKEN}\n")
        self.repo.commit("odd names")
        for name in names:
            self.repo.remove(name)
        self.repo.commit("remove")
        out = self.history()
        for name in names:
            with self.subTest(name=name):
                self.assertIn(f"{name}:2: {TOKEN_KIND}", out)

    def test_token_only_on_a_branch_other_than_head(self):
        self.repo.write("keep.txt", "fine\n")
        self.repo.commit("base")
        self.repo.git("switch", "-q", "-c", "side")
        self.repo.write("side.txt", f"{TOKEN}\n")
        self.repo.commit("side work")
        self.repo.git("switch", "-q", "main")
        self.assertEqual(self.repo.scan()[0], 0)
        self.assertIn(f"side.txt:1: {TOKEN_KIND}", self.history())

    def test_token_only_on_a_tag(self):
        self.repo.write("keep.txt", "fine\n")
        self.repo.commit("base")
        self.repo.git("switch", "-q", "-c", "gone")
        self.repo.write("tagged.txt", f"{TOKEN}\n")
        self.repo.commit("tagged work")
        self.repo.git("tag", "-a", "v0", "-m", "release")
        self.repo.git("switch", "-q", "main")
        self.repo.git("branch", "-q", "-D", "gone")
        self.assertIn(f"tagged.txt:1: {TOKEN_KIND}", self.history())

    def test_token_only_in_a_commit_message(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit(f"subject line\n\nbody with {TOKEN}")
        self.assertRegex(self.history(),
                         rf"commit [0-9a-f]{{12}} message:3: {re.escape(TOKEN_KIND)}")

    def test_token_only_in_an_author_name(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit("fine", f"--author={TOKEN} <noreply@honeycombagents.ai>")
        self.assertRegex(self.history(), rf"commit [0-9a-f]{{12}} author: {re.escape(TOKEN_KIND)}")

    def test_token_only_in_a_tag_message_or_tagger(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit("fine")
        self.repo.git("-c", f"user.name={TOKEN}", "tag", "-a", "v1", "-m", f"notes {TOKEN}")
        out = self.history()
        self.assertRegex(out, rf"tag [0-9a-f]{{12}} message:1: {re.escape(TOKEN_KIND)}")
        self.assertRegex(out, rf"tag [0-9a-f]{{12}} tagger: {re.escape(TOKEN_KIND)}")

    def test_a_foreign_committer_email_passes(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.git("-c", "user.email=" + j("someone", "@", "example", ".com"), "commit", "-q",
                      "-m", "fine")
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 0, out)
        self.assertNotIn("committer:", out)

    def test_utf16_file_with_a_token_is_caught(self):
        data = f"clean\n{TOKEN}\n".encode("utf-16")
        self.assertIn(b"\0", data)
        self.repo.write_bytes("wide.txt", data)
        code, out = self.repo.scan()
        self.assertEqual(code, 1, out)
        self.assertIn(f"wide.txt:2: {TOKEN_KIND} (UTF-16)", out)
        self.assertNotIn(TOKEN, out)
        self.repo.commit("wide")
        self.repo.remove("wide.txt")
        self.repo.commit("remove")
        self.assertIn(f"wide.txt:2: {TOKEN_KIND} (UTF-16)", self.history())

    def test_nul_bytes_do_not_hide_a_utf8_token(self):
        self.repo.write_bytes("bin.dat", b"\0\0\0header\n" + TOKEN.encode() + b"\n")
        code, out = self.repo.scan()
        self.assertEqual(code, 1, out)
        self.assertIn(f"bin.dat:2: {TOKEN_KIND}", out)

    def test_undecodable_utf16_is_reported_not_skipped(self):
        self.repo.write_bytes("bad16.txt", b"\xff\xfe" + b"\x00\xd8")  # a lone surrogate
        code, out = self.repo.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("bad16.txt:0: undecodable UTF-16 text", out)

    def test_a_file_name_holding_a_token_is_never_printed(self):
        name = f"f-{TOKEN}.txt"
        self.repo.write(name, f"{TOKEN}\n")
        code, out = self.repo.scan()
        self.assertEqual(code, 1, out)
        self.assertNotIn(TOKEN, out)
        self.assertRegex(out, rf"\(path [0-9a-f]{{12}}\): file name: {re.escape(TOKEN_KIND)}")
        self.repo.commit("named")
        self.history()

    def test_everything_at_once_never_prints_the_token(self):
        self.repo.write("a.txt", f"++ x\n{TOKEN}\n")
        self.repo.write('q"uote.txt', f"{TOKEN}\n")
        self.repo.write_bytes("w.txt", TOKEN.encode("utf-16"))
        self.repo.commit(f"message {TOKEN}", f"--author={TOKEN} <noreply@honeycombagents.ai>")
        self.repo.git("tag", "-a", "t1", "-m", TOKEN)
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 1, out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN[4:20], out)

    def test_merge_reports_only_lines_new_to_every_parent(self):
        self.repo.write("base.txt", "base\n")
        self.repo.commit("base")
        self.repo.git("switch", "-q", "-c", "topic")
        self.repo.write("topic.txt", f"{TOKEN}\n")
        self.repo.commit("topic")
        self.repo.git("switch", "-q", "main")
        self.repo.write("main.txt", "main\n")
        self.repo.commit("main")
        self.repo.git("merge", "-q", "--no-ff", "-m", "merge", "topic")
        out = self.history()
        commits = [l for l in out.splitlines() if l.startswith("commit ")]
        self.assertEqual(len(commits), 1, out)
        self.assertIn(f"topic.txt:1: {TOKEN_KIND}", commits[0])


FOREIGN_EMAIL = j("outside.contributor", "@", "example", ".org")
NOREPLY_EMAIL = j("12345678+octo-user", "@", "users.noreply.", "git", "hub", ".com")


class Identities(ScanTestCase):
    """Identities get the token checks and the deny-list, but not the email rule."""

    def test_a_foreign_author_email_passes(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit("fine", f"--author=Outside Contributor <{FOREIGN_EMAIL}>")
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 0, out)
        self.assertNotIn("author:", out)

    def test_a_committer_with_a_github_style_noreply_address_passes(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.git("-c", "user.name=GitHub", "-c", f"user.email={NOREPLY_EMAIL}", "commit",
                      "-q", "-m", "Merge pull request #1",
                      "--author=honeycombagents.ai <noreply@honeycombagents.ai>")
        self.repo.git("-c", "user.name=GitHub", "-c", f"user.email={NOREPLY_EMAIL}", "tag", "-a",
                      "v2", "-m", "release")
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 0, out)
        self.assertNotIn("committer:", out)
        self.assertNotIn("tagger:", out)

    def test_a_token_in_an_author_name_still_fails_with_a_foreign_email(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit("fine", f"--author={TOKEN} <{FOREIGN_EMAIL}>")
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, rf"commit [0-9a-f]{{12}} author: {re.escape(TOKEN_KIND)}")
        self.assertNotIn("author: email address", out)
        self.assertNotIn(TOKEN, out)

    def test_the_denylist_still_applies_to_identities(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit("fine", f"--author=Zzsentinelname <{FOREIGN_EMAIL}>")
        code, out = self.repo.scan("--history", deny="zzsentinelname")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"commit [0-9a-f]{12} author: deny-list pattern #1")
        self.assertNotIn("Zzsentinelname", out)

    def test_an_email_in_a_commit_message_still_fails(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit(f"thanks to {FOREIGN_EMAIL}")
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"commit [0-9a-f]{12} message:1: email address")

    def test_an_email_in_a_file_still_fails_in_the_tree_and_in_history(self):
        self.repo.write("a.txt", f"contact {FOREIGN_EMAIL}\n")
        code, out = self.repo.scan()
        self.assertEqual(code, 1, out)
        self.assertIn("a.txt:1: email address", out)
        self.repo.commit("add")
        self.repo.remove("a.txt")
        self.repo.commit("remove")
        code, out = self.repo.scan("--history")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"commit [0-9a-f]{12} a\.txt:1: email address")


if __name__ == "__main__":
    unittest.main()
