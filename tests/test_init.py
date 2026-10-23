"""``repo-signals init`` tests. Offline: temporary directories outside any git work tree."""
import ast
import io
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _src  # noqa: F401
from repo_signals import config
from repo_signals import init_setup
from repo_signals.cli import main
from repo_signals.collector.signal import parse_signal
from repo_signals.drafting.suppression import load_suppression

ROOT = Path(__file__).resolve().parent.parent
FILES = ("signal.toml", "env.example", "sender.example", "suppression.txt", ".gitignore")


class TTYIn:
    def __init__(self, answer):
        self.answer = answer
        self.reads = 0

    def isatty(self):
        return True

    def readline(self):
        self.reads += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class TTYOut(io.StringIO):
    def isatty(self):
        return True


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.target = self.base / "cfg"

    def init(self, directory=None, answer="yes\n", environ=None, stdin=None, out=None,
             cwd=None):
        self.stdin = stdin if stdin is not None else TTYIn(answer)
        self.out = out if out is not None else TTYOut()
        self.err = io.StringIO()
        code = init_setup.run_init(str(directory if directory is not None else self.target),
                                   environ=environ if environ is not None else {},
                                   stdin=self.stdin, out=self.out, err=self.err, cwd=cwd,
                                   which=str)
        self.text = self.out.getvalue() + self.err.getvalue()
        return code


class Writes(Case):
    def test_writes_every_file_with_exact_modes(self):
        old = os.umask(0)
        try:
            self.assertEqual(self.init(), 0, self.text)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o700)
        self.assertEqual(sorted(os.listdir(self.target)), sorted(FILES))
        for name in FILES:
            with self.subTest(name=name):
                self.assertEqual(stat.S_IMODE(os.stat(self.target / name).st_mode), 0o600)
        self.assertIn(f"init: would create in {self.target}:", self.text)
        self.assertIn(f"init: created 5 files in {self.target}", self.text)
        self.assertIn("read -rs NAME; export NAME", self.text)
        self.assertIn("repo-signals doctor", self.text)
        self.assertIn("repo-signals run --live", self.text)
        self.assertIn("init: checklist (variable names only, no values)", self.text)
        self.assertEqual(self.stdin.reads, 1)

    def test_generated_files_pass_the_real_parsers(self):
        self.init()
        signal = parse_signal((self.target / "signal.toml").read_bytes())
        self.assertEqual(signal.targets[0].domain, "example.com")
        listed = load_suppression(self.target / "suppression.txt")
        self.assertEqual((len(listed.emails), len(listed.domains)), (0, 0))
        self.assertEqual((self.target / ".gitignore").read_text(), "*\n")

    def test_env_example_has_names_only_and_covers_every_variable(self):
        self.init()
        text = (self.target / "env.example").read_text()
        self.assertNotIn("=", text)
        names = [l for l in text.splitlines() if l and not l.startswith("#")]
        for name in names:
            self.assertRegex(name, r"\A[A-Z][A-Z0-9_]*\Z")
        source = (ROOT / "src/repo_signals/config.py").read_text()
        expected = set(re.findall(r'"(REPO_SIGNALS_[A-Z0-9_]+)"', source))
        expected |= {"HUNTER_API_KEY", "APOLLO_API_KEY"}
        self.assertEqual(set(names), expected)
        self.assertEqual(len(names), len(set(names)))
        sender = (self.target / "sender.example").read_text()
        self.assertEqual([l for l in sender.splitlines() if l and not l.startswith("#")],
                         list(config.SENDER_VARS))
        self.assertNotIn("=", sender)

    def test_default_directory(self):
        self.assertEqual(self.init(directory="", cwd=self.base), 0, self.text)
        self.assertTrue((self.base / "repo-signals-config" / "signal.toml").exists())

    def test_existing_private_directory_is_fine(self):
        self.target.mkdir(mode=0o700)
        os.chmod(self.target, 0o700)
        self.assertEqual(self.init(), 0, self.text)


class Secrets(Case):
    def test_never_prompts_for_or_echoes_a_secret(self):
        creds = self.base / "key.json"
        creds.write_text('{"private_key": "SENTINELKEYMATERIAL"}')
        env = {config.GITHUB_TOKEN: "SENTINELTOKEN" + "x" * 20, "HUNTER_API_KEY": "SENTINELHUNTER",
               "APOLLO_API_KEY": "SENTINELAPOLLO", config.SENDER_NAME: "SENTINELNAME",
               config.POSTAL_ADDRESS: "SENTINELPOSTAL", config.GOOGLE_CREDENTIALS: str(creds),
               config.SHEET_ID: "SENTINELSHEETID_abcdefghij", config.PROVIDER: "hunter"}
        self.assertEqual(self.init(environ=env), 0, self.text)
        self.assertNotIn("SENTINEL", self.text.upper())
        self.assertNotIn(str(creds), self.text)
        for name in FILES:
            self.assertNotIn("SENTINEL", (self.target / name).read_text().upper())
        self.assertEqual(self.stdin.reads, 1)
        for word in ("token:", "password", "api key:", "secret:"):
            self.assertNotIn(word, self.out.getvalue().lower())


class Confirmation(Case):
    def test_only_exact_yes(self):
        for answer in ("y\n", "YES\n", "yes \n", " yes\n", "\n", "", "no\n", KeyboardInterrupt()):
            with self.subTest(answer=repr(answer)):
                self.assertEqual(self.init(answer=answer), 1)
                self.assertFalse(self.target.exists())
                self.assertIn("init: not confirmed; nothing written", self.text)

    def test_terminal_required(self):
        self.assertEqual(self.init(stdin=io.StringIO("yes\n")), 2)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.init(out=io.StringIO()), 2)
        self.assertFalse(self.target.exists())
        self.assertIn("stdin and stdout must be a terminal", self.text)

    def test_no_yes_flag(self):
        for argv in (["init", "--yes"], ["init", "-y"], ["init", "--force"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(argv)
        text = (ROOT / "src/repo_signals/init_setup.py").read_text()
        self.assertNotIn("--yes", text)
        self.assertNotIn("--force", text)


class Refusals(Case):
    def test_any_existing_target_refuses_everything(self):
        for name in FILES:
            with self.subTest(name=name):
                target = self.base / f"cfg-{name.strip('.')}"
                target.mkdir(mode=0o700)
                os.chmod(target, 0o700)
                (target / name).write_text("SENTINEL keep me")
                self.assertEqual(self.init(directory=target), 2)
                self.assertEqual(os.listdir(target), [name])
                self.assertEqual((target / name).read_text(), "SENTINEL keep me")
                self.assertIn(f"target_exists ({name})", self.text)

    def test_symlinked_target_file_refuses(self):
        self.target.mkdir(mode=0o700)
        os.chmod(self.target, 0o700)
        os.symlink(self.base / "elsewhere", self.target / "signal.toml")
        self.assertEqual(self.init(), 2)
        self.assertFalse((self.base / "elsewhere").exists())

    def test_inside_a_git_work_tree(self):
        (self.base / ".git").mkdir()
        self.assertEqual(self.init(directory=self.base / "a" / "b" / "cfg"), 2)
        self.assertFalse((self.base / "a").exists())
        self.assertIn("inside_a_git_work_tree", self.text)
        other = self.base / "sub"
        other.mkdir()
        self.assertEqual(self.init(directory=self.base / "sub" / "cfg"), 2)

    def test_git_file_counts_too(self):
        project = self.base / "worktree"
        project.mkdir()
        (project / ".git").write_text("gitdir: elsewhere\n")
        self.assertEqual(self.init(directory=project / "cfg"), 2)
        self.assertFalse((project / "cfg").exists())

    @unittest.skipUnless((ROOT / ".git").exists(), "needs a git checkout")
    def test_this_repository_is_refused(self):
        self.assertEqual(self.init(directory="", cwd=ROOT), 2)
        self.assertFalse((ROOT / "repo-signals-config").exists())

    def test_symlink_and_file_paths(self):
        real = self.base / "real"
        real.mkdir(mode=0o700)
        link = self.base / "link"
        os.symlink(real, link)
        self.assertEqual(self.init(directory=link), 2)
        self.assertIn("path_is_a_symlink", self.text)
        self.assertEqual(os.listdir(real), [])
        afile = self.base / "afile"
        afile.write_text("x")
        self.assertEqual(self.init(directory=afile), 2)
        self.assertIn("path_is_a_file", self.text)
        self.assertEqual(afile.read_text(), "x")

    def test_open_existing_directory_refused(self):
        self.target.mkdir()
        os.chmod(self.target, 0o755)
        self.assertEqual(self.init(), 2)
        self.assertIn("directory_open_to_other_users", self.text)
        self.assertEqual(os.listdir(self.target), [])


class Source(unittest.TestCase):
    def test_no_network_or_subprocess_imports(self):
        tree = ast.parse((ROOT / "src/repo_signals/init_setup.py").read_text())
        bad = ("socket", "ssl", "http", "urllib", "subprocess", "smtplib", "requests", "asyncio")
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if node.level:
                    names = [n for n in names if n.startswith(("http", "collector.github_api"))]
            for name in names:
                self.assertNotIn(name.split(".")[0], bad, name)

    def test_cli_dispatch(self):
        with mock.patch.object(init_setup, "run_init", return_value=0) as run_init:
            self.assertEqual(main(["init", "--dir", "/somewhere"]), 0)
            self.assertEqual(main(["init"]), 0)
        self.assertEqual([c.args for c in run_init.call_args_list], [("/somewhere",), (None,)])

    def test_docs_have_no_em_dashes(self):
        self.assertNotIn("—", (ROOT / "docs/deployment.md").read_text())


if __name__ == "__main__":
    unittest.main()
