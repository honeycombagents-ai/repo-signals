"""doctor's GitHub token line follows the signal file's source. A Hugging Face file is collected
anonymously, so doctor says nothing about the GitHub token for it; every other case prints the
line exactly as before. Offline: synthetic files only."""
import io
import os
import tempfile
import unittest

import _src  # noqa: F401
from repo_signals import config, doctor, init_setup
from repo_signals.providers import Registry
from repo_signals.providers.fake import FakeProvider

TOKEN = "SENTINELTOKEN" + "x" * 30
MISSING = ("WARN REPO_SIGNALS_GITHUB_TOKEN is missing (needed before GitHub signals can be "
           "collected)")
GITHUB_TEXT = ('name = "synthetic"\nterms = ["examplelib"]\n\n[[targets]]\n'
               'domain = "example.com"\norg = "example-org"\n')
HF_TEXT = ('source = "huggingface"\nname = "synthetic-hf"\nterms = ["examplelib"]\n\n'
           '[[targets]]\ndomain = "example.com"\norg = "example-org"\nconfirm_org = true\n')


class TokenLine(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.files = {}
        for name, text in (("github", GITHUB_TEXT), ("hf", HF_TEXT), ("bad", 'name = "x"\n')):
            path = os.path.join(tmp.name, f"{name}.toml")
            with open(path, "w") as fh:
                fh.write(text)
            self.files[name] = path

    def doctor(self, signal=None, **over):
        env = dict(over)
        if signal is not None:
            env[config.SIGNAL_FILE] = self.files[signal]
        reg = Registry()
        reg.register(FakeProvider())
        out = io.StringIO()
        code = doctor.run(environ=env, registry=reg, out=out, which=lambda name: "/usr/bin/x")
        text = out.getvalue()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("SENTINEL", text.upper())
        return code, text

    @staticmethod
    def token_lines(text):
        return [l for l in text.splitlines() if config.GITHUB_TOKEN in l
                and "failing gates" not in l]

    @staticmethod
    def counts(text):
        return text.strip().splitlines()[-1]

    def test_no_token_line_for_a_hugging_face_file(self):
        code, text = self.doctor("hf")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.token_lines(text), [])
        self.assertIn("source huggingface", text)

    def test_a_token_set_with_a_hugging_face_file_prints_nothing(self):
        code, text = self.doctor("hf", **{config.GITHUB_TOKEN: TOKEN})
        self.assertEqual(code, 0, text)
        self.assertEqual(self.token_lines(text), [])

    def test_a_malformed_token_still_fails_with_a_hugging_face_file(self):
        code, text = self.doctor("hf", **{config.GITHUB_TOKEN: "SENTINEL short"})
        self.assertEqual(code, 1, text)
        self.assertEqual(self.token_lines(text),
                         ["FAIL REPO_SIGNALS_GITHUB_TOKEN is not a well-formed token"])

    def test_the_line_is_unchanged_for_a_github_file(self):
        code, text = self.doctor("github")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.token_lines(text), [MISSING])
        code, text = self.doctor("github", **{config.GITHUB_TOKEN: TOKEN})
        self.assertEqual(code, 0, text)
        self.assertEqual(self.token_lines(text), ["PASS REPO_SIGNALS_GITHUB_TOKEN is set"])

    def test_the_line_is_unchanged_without_a_signal_file(self):
        code, text = self.doctor()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.token_lines(text), [MISSING])

    def test_the_line_is_unchanged_for_an_unusable_signal_file(self):
        code, text = self.doctor("bad")
        self.assertEqual(code, 1, text)   # the signal file's own check FAILs, as before
        self.assertEqual(self.token_lines(text), [MISSING])

    def test_counts_drop_by_one_warn_and_exit_codes_match(self):
        github_code, github = self.doctor("github")
        hf_code, hf = self.doctor("hf")
        self.assertEqual(github_code, hf_code)
        g = [int(n) for n in self.counts(github).replace(",", "").split() if n.isdigit()]
        h = [int(n) for n in self.counts(hf).replace(",", "").split() if n.isdigit()]
        self.assertEqual((h[0], h[1], h[2]), (g[0], g[1] - 1, g[2]))

    def test_env_example_says_the_token_is_for_github_files_only(self):
        text = init_setup.env_example()
        self.assertIn("# secret, optional, for GitHub signal files only: set it with read -rs\n"
                      "REPO_SIGNALS_GITHUB_TOKEN\n", text)


if __name__ == "__main__":
    unittest.main()
