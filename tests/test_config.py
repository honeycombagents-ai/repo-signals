import unittest

import _src  # noqa: F401
from repo_signals import config
from repo_signals.budget import DEFAULT_MAX_CALLS, HARD_MAX_CALLS
from repo_signals.config import ConfigError, Settings, load_settings

GOOD_SHEET = "S" * 10 + "_-" + "9" * 20
GOOD_TOKEN = "t0k" + "x" * 30


class Defaults(unittest.TestCase):
    def test_empty_environment(self):
        s = load_settings({})
        self.assertEqual(s.provider, "fake")
        self.assertEqual(s.max_calls, DEFAULT_MAX_CALLS)
        self.assertIsNone(s.sheet_id)
        self.assertIsNone(s.github_token)
        self.assertIs(s.dry_run, True)

    def test_dry_run_is_the_default(self):
        self.assertIs(Settings().dry_run, True)

    def test_empty_strings_count_as_unset(self):
        env = {v: "" for v in (config.PROVIDER, config.MAX_CALLS, config.SHEET_ID,
                               config.GITHUB_TOKEN)}
        self.assertEqual(load_settings(env), load_settings({}))

    def test_reads_only_its_own_variables(self):
        self.assertEqual(load_settings({"GITHUB_TOKEN": GOOD_TOKEN, "SHEET_ID": GOOD_SHEET}),
                         load_settings({}))


class Provider(unittest.TestCase):
    def test_valid(self):
        for name in ("fake", "my-provider", "p_2"):
            with self.subTest(name=name):
                self.assertEqual(load_settings({config.PROVIDER: name}).provider, name)

    def test_invalid(self):
        for bad in ("Fake", " fake", "fake ", "a b", "1abc", "x" * 33, "fake;rm"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as cm:
                load_settings({config.PROVIDER: bad})
            self.assertEqual(cm.exception.var, config.PROVIDER)


class MaxCalls(unittest.TestCase):
    def test_valid(self):
        for raw, want in (("1", 1), ("25", 25), (str(HARD_MAX_CALLS), HARD_MAX_CALLS)):
            with self.subTest(raw=raw):
                self.assertEqual(load_settings({config.MAX_CALLS: raw}).max_calls, want)

    def test_invalid(self):
        for bad in ("0", "-1", "+5", " 5", "5 ", "5.0", "1e2", "05", "abc", "1_0",
                    str(HARD_MAX_CALLS + 1), "9" * 7, "５"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as cm:
                load_settings({config.MAX_CALLS: bad})
            self.assertEqual(cm.exception.var, config.MAX_CALLS)

    def test_error_names_variable_not_value(self):
        with self.assertRaises(ConfigError) as cm:
            load_settings({config.MAX_CALLS: "SENTINEL77"})
        self.assertIn(config.MAX_CALLS, str(cm.exception))
        self.assertNotIn("SENTINEL77", str(cm.exception))


class SheetId(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(load_settings({config.SHEET_ID: GOOD_SHEET}).sheet_id, GOOD_SHEET)

    def test_invalid(self):
        for bad in ("short", "x" * 19, "x" * 129, GOOD_SHEET + " ", "a/b" + "c" * 20,
                    "https" + "://docs.google.com/spreadsheets/d/" + GOOD_SHEET):
            with self.subTest(bad=bad[:20]), self.assertRaises(ConfigError) as cm:
                load_settings({config.SHEET_ID: bad})
            self.assertEqual(cm.exception.var, config.SHEET_ID)


class GithubToken(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(load_settings({config.GITHUB_TOKEN: GOOD_TOKEN}).github_token,
                         GOOD_TOKEN)

    def test_invalid(self):
        for bad in ("short", " " + GOOD_TOKEN, GOOD_TOKEN + "\n", "a b" + "c" * 30,
                    "x" * 256, "é" * 30):
            with self.subTest(n=len(bad)), self.assertRaises(ConfigError) as cm:
                load_settings({config.GITHUB_TOKEN: bad})
            self.assertEqual(cm.exception.var, config.GITHUB_TOKEN)
            self.assertNotIn(bad.strip() or "x", str(cm.exception).replace(config.GITHUB_TOKEN, ""))


class Repr(unittest.TestCase):
    def test_repr_and_str_hide_values(self):
        s = Settings(provider="zzsentinelprov", max_calls=437, sheet_id="SENTINELSHEET" * 3,
                     github_token="SENTINELTOKEN" * 3)
        for text in (repr(s), str(s), f"{s}", "%s" % (s,), str([s])):
            with self.subTest(text=text[:30]):
                for secret in ("zzsentinelprov", "437", "SENTINELSHEET", "SENTINELTOKEN"):
                    self.assertNotIn(secret, text)
        self.assertIn("sheet_id=<set>", repr(s))
        self.assertIn("github_token=<unset>", repr(Settings()))


if __name__ == "__main__":
    unittest.main()
