"""Pre-public cleanups: the intro template wording, the init docstring and the drafting docs."""
import unittest
from pathlib import Path

import _src  # noqa: F401
from repo_signals import init_setup
from repo_signals.drafting.base import Sender
from repo_signals.drafting.template import (ALLOWED_PLACEHOLDERS, TEMPLATE_DIR, TemplateDrafter,
                                            placeholders)
from repo_signals.models import Evidence, Person

ROOT = Path(__file__).resolve().parent.parent
OLD = "I came across this public signal about"
NEW = ("A public signal about {company_domain} turned up in an automated search of public "
       "repositories:")


class IntroTemplate(unittest.TestCase):
    def test_new_sentence_and_only_allowed_placeholders(self):
        text = (TEMPLATE_DIR / "intro.txt").read_text()
        self.assertIn(NEW, text)
        self.assertNotIn(OLD, text)
        self.assertNotIn("came across", text)
        self.assertTrue(set(placeholders(text)) <= set(ALLOWED_PLACEHOLDERS))

    def test_renders_and_passes_the_guard(self):
        evidence = (Evidence("example.com", "The public repository example-org/examplelib-core "
                             "matches the term examplelib, last pushed on 2026-09-30.",
                             "https://example.com/example-org/examplelib-core", "repo"),)
        sender = Sender("Avery Alder", "Engineer", "Example Org", "1 Example Street",
                        "Reply stop to opt out.")
        person = Person("Blake Birch", None, "CTO", "example.com", "fake", 0.5)
        draft = TemplateDrafter("intro").draft(person, evidence, sender)
        self.assertIn(NEW.format(company_domain="example.com"), draft.body)
        self.assertIn(evidence[0].url, draft.body)
        self.assertNotIn("{", draft.body)

    def test_old_sentence_gone_everywhere(self):
        for path in [*(ROOT / "src").rglob("*.txt"), *(ROOT / "docs").glob("*.md"),
                     ROOT / "README.md"]:
            with self.subTest(file=path.name):
                self.assertNotIn(OLD, path.read_text())


class InitDocstring(unittest.TestCase):
    def test_symlink_wording(self):
        doc = init_setup.__doc__
        self.assertIn("refuses a target path whose final component is a symlink", doc)
        self.assertIn("only that final component is\nchecked", doc)


class DraftingDocs(unittest.TestCase):
    def test_before_you_send_anything(self):
        text = (ROOT / "docs/drafting.md").read_text()
        section = text.split("## Before you send anything\n", 1)[1].split("\n## ", 1)[0]
        for phrase in ("automated search", "A person must review every draft",
                       "Email law", "platform", "data-protection law", "your responsibility"):
            self.assertIn(phrase, section)
        self.assertNotIn("—", section)


class ReadmeIntro(unittest.TestCase):
    def test_status_signal_file_and_never_sends(self):
        text = (ROOT / "README.md").read_text()
        flat = " ".join(text.split())
        self.assertNotIn("scaffold, not usable yet", text)
        self.assertIn("signal file", text)
        self.assertIn("You list the target companies in a signal file, and Repo Signals finds "
                      "matching public GitHub repositories for them. The company domains in the "
                      "signal file are sent to the people provider you choose, under your own "
                      "key.", flat)
        status = flat.split("## What it is", 1)[0]
        self.assertIn("Hunter's domain search was checked live on 2026-10-22.", status)
        self.assertIn("not checked live", status)
        self.assertTrue("never sends" in text or "Nothing is ever sent" in text)
        self.assertIn("Repo Signals itself never sends anything.", flat)


if __name__ == "__main__":
    unittest.main()
