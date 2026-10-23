"""Setup guide fixes from a cold run: the key path, the organisation name, confirm_org, a run
that writes nothing, and the sheets budget headroom."""
import unittest
from pathlib import Path

import _src  # noqa: F401
from repo_signals.collector.github_fields import WEB_HOST

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs/setup-guide.md"


def flat(text):
    return " ".join(text.split())


class Guide(unittest.TestCase):
    def setUp(self):
        self.text = GUIDE.read_text()
        self.flat = flat(self.text)

    def test_key_path_tilde_is_expanded_before_the_move(self):
        expand = 'KEY_DOWNLOAD="${KEY_DOWNLOAD/#\\~/$HOME}"\n'
        self.assertIn(expand, self.text)
        prompt = self.text.index('read -r "KEY_DOWNLOAD?Path of the downloaded key: "')
        move = self.text.index('mv "$KEY_DOWNLOAD"')
        self.assertLess(prompt, self.text.index(expand))
        self.assertLess(self.text.index(expand), move)
        self.assertIn("type the key's full path or drag the downloaded file", self.flat)

    def test_organisation_name_prompt_and_domain_refusal(self):
        self.assertIn(f'read -r "TARGET_ORG?GitHub organisation name (the NAME in {WEB_HOST}/NAME, '
                      'not a domain): "', self.text)
        self.assertNotIn("GitHub organisation login: ", self.text)
        self.assertIn("is refused there with `signal_target_1_bad_org`", self.flat)
        self.assertIn("nothing is requested", self.flat)

    def test_confirm_org_is_explained_and_optional(self):
        self.assertIn('printf \'%s\\n\' "confirm_org = true" >> "$CONFIG_DIR/first-run.toml"',
                      self.text)
        self.assertIn("Set it only for an organisation you know belongs to the company.", self.flat)
        self.assertIn("website_mismatch: excluded; set confirm_org = true to include", self.flat)
        heredoc = self.text.split('cat > "$CONFIG_DIR/first-run.toml" <<EOF\n', 1)[1]
        self.assertNotIn("confirm_org", heredoc.split("\nEOF\n", 1)[0])

    def test_a_run_can_end_with_nothing_written(self):
        for phrase in ("A first live run can also end with nothing written, and that is not an "
                       "error", "`sheet: nothing to write`", "No sheet append is made",
                       "no provider call was made (no credit spent)",
                       "`dropped 1: term_not_in_name` and `people skipped: no_evidence 1`",
                       "the provider was asked once (that call may spend a credit)"):
            self.assertIn(phrase, self.flat)

    def test_sheets_budget_headroom(self):
        self.assertIn("`REPO_SIGNALS_SHEETS_MAX_CALLS=6` is exactly enough for a first run on an "
                      "empty sheet", self.flat)
        self.assertIn("The second run in step 13 needs fewer (4:", self.flat)
        self.assertIn("still requires at least 6", self.flat)
        self.assertIn("Use 8 if you want headroom", self.flat)


class SignalFileAndTroubleshooting(unittest.TestCase):
    def test_signal_file_says_what_confirm_org_does(self):
        text = flat((ROOT / "docs/signal-file.md").read_text())
        for phrase in ("What it does:", "`website_mismatch` or `no_website`",
                       "the match line says `confirmed`",
                       "Set `confirm_org = true` only for an org you know belongs to the company."):
            self.assertIn(phrase, text)

    def test_troubleshooting_covers_a_domain_as_the_organisation_name(self):
        text = flat((ROOT / "docs/troubleshooting.md").read_text())
        self.assertIn("**A domain typed as the organisation name.**", text)
        self.assertIn("for example a domain such as `example.com`", text)


if __name__ == "__main__":
    unittest.main()
