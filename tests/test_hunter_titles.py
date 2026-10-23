"""Hunter titles filter only the first 3 results: the docs say so, doctor and the plan warn.

Offline only: doctor reads a synthetic signal file, and run --live is declined at the prompt.
"""
import io
import os
import tempfile
import unittest
from pathlib import Path

import _src  # noqa: F401
import test_run as run_fx
from repo_signals import config, doctor
from repo_signals.providers.hunter import TITLES_WARNING
from repo_signals.run import PEOPLE_LIMIT

ROOT = Path(__file__).resolve().parent.parent
KEYS = {"HUNTER_API_KEY": "SYNTHETICHUNTERKEY0123", "APOLLO_API_KEY": "SYNTHETICAPOLLOKEY0123"}
SIGNAL = ('name = "synthetic"\nterms = ["examplelib"]\n{titles}\n'
          '[[targets]]\ndomain = "example.com"\norg = "example-org"\n')


def flat(name):
    return " ".join((ROOT / name).read_text().split())


class Warning(unittest.TestCase):
    def test_the_number_in_the_warning_is_the_run_limit(self):
        self.assertIn(f"among the first {PEOPLE_LIMIT} people Hunter returns", TITLES_WARNING)


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def signal(self, titles):
        path = os.path.join(self.tmp.name, f"signal-{len(os.listdir(self.tmp.name))}.toml")
        with open(path, "w") as fh:
            fh.write(SIGNAL.format(titles=f"titles = {titles}" if titles else ""))
        return path

    def doctor(self, provider, titles=None, signal=None):
        env = {**KEYS, config.PROVIDER: provider,
               config.SIGNAL_FILE: signal if signal is not None else self.signal(titles)}
        out = io.StringIO()
        code = doctor.run(environ=env, out=out, which=lambda name: None)
        return code, out.getvalue()

    def test_warns_for_hunter_with_titles(self):
        code, out = self.doctor("hunter", '["CTO"]')
        self.assertIn(f"WARN {TITLES_WARNING}\n", out)
        self.assertEqual(code, self.doctor("hunter")[0])  # a WARN never changes the exit code

    def test_no_warning_for_hunter_without_titles(self):
        self.assertNotIn(TITLES_WARNING, self.doctor("hunter")[1])

    def test_no_warning_for_apollo_or_fake_with_titles(self):
        for provider in ("apollo", "fake"):
            with self.subTest(provider=provider):
                self.assertNotIn(TITLES_WARNING, self.doctor(provider, '["CTO"]')[1])

    def test_no_warning_and_no_crash_for_an_unusable_signal_file(self):
        missing = os.path.join(self.tmp.name, "missing.toml")
        code, out = self.doctor("hunter", signal=missing)
        self.assertNotIn(TITLES_WARNING, out)
        self.assertIn("file is not usable (signal_unreadable)", out)
        self.assertEqual(code, 1)


class Plan(run_fx.Case):
    def plan(self, provider, titles):
        extra = f"titles = {titles}" if titles else ""
        env = self.env(**{config.PROVIDER: provider, config.SIGNAL_FILE: self.signal_file(
            extra=extra)}, **KEYS)
        code = self.live(env, answer="no\n")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), (0, 0))
        return self.out.getvalue().splitlines()

    def test_plan_warns_for_hunter_with_titles_right_after_the_provider(self):
        lines = self.plan("hunter", '["CTO"]')
        i = lines.index("provider: hunter")
        self.assertEqual(lines[i + 1], TITLES_WARNING)
        self.assertEqual(lines.count(TITLES_WARNING), 1)

    def test_no_plan_line_without_titles_or_for_other_providers(self):
        for provider, titles in (("hunter", None), ("apollo", '["CTO"]'), ("fake", '["CTO"]')):
            with self.subTest(provider=provider, titles=titles):
                self.assertNotIn(TITLES_WARNING, self.plan(provider, titles))


class Docs(unittest.TestCase):
    def test_hunter_doc_states_the_first_3_limit_and_the_unchecked_cost(self):
        text = flat("docs/hunter.md")
        self.assertIn("Titles are matched only among the first 3 people Hunter returns.", text)
        self.assertIn("The order Hunter returns people in is not documented here", text)
        self.assertNotIn("can spend one credit", text)
        self.assertIn("How many credits one domain search costs", text)
        self.assertIn(f"`WARN {TITLES_WARNING}`", text)

    def test_hunter_doc_lists_missing_email_and_uses_read_rs(self):
        text = (ROOT / "docs/hunter.md").read_text()
        self.assertIn("(`missing_email`, `missing_name`, `missing_title`, `filtered_by_title`)",
                      " ".join(text.split()))
        self.assertIn('read -rs "HUNTER_API_KEY?Hunter API key: "', text)
        self.assertNotIn("export HUNTER_API_KEY=", text)

    def test_guide_and_signal_file_tell_hunter_users(self):
        for name in ("docs/setup-guide.md", "docs/signal-file.md"):
            with self.subTest(file=name):
                text = flat(name)
                self.assertIn("filtered among only the first 3 people Hunter returns", text)
                self.assertIn("Apollo filters titles on its own side", text)
        self.assertIn(f"`WARN {TITLES_WARNING}`", flat("docs/setup-guide.md"))

    def test_troubleshooting_explains_the_first_3_limit(self):
        self.assertIn("It runs on only the first 3 people Hunter returns",
                      flat("docs/troubleshooting.md"))


if __name__ == "__main__":
    unittest.main()
