"""The sheet's duplicate key for rows without an email, Hunter's missing_email count, and no
stale dropped line after a failed lookup. Offline only: fake transports and synthetic data."""
import io
import re
import unittest
from pathlib import Path

import _src  # noqa: F401
import test_apollo as apollo_fx
import test_hunter as hunter_fx
import test_run as run_fx
import test_sheets as sheets_fx
from repo_signals import run as run_mod
from repo_signals.budget import CallBudget
from repo_signals.http import HttpClient, RawResponse
from repo_signals.models import Person
from repo_signals.providers import ProviderError, hunter
from repo_signals.sheets.layout import COLUMNS
from repo_signals.sheets.writer import WriteResult

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = sheets_fx.DOMAIN


def apollo_person(first, title, domain=DOMAIN):
    """An Apollo-shaped person: first name only, no email."""
    return Person(name=first, email=None, title=title, company_domain=domain, source="apollo",
                  confidence=0.0)


def emailed(name, local, title="CTO"):
    return Person(name=name, email=sheets_fx.addr(local), title=title, company_domain=DOMAIN,
                  source="hunter", confidence=0.9)


def sheet_row(name, email, title, domain=DOMAIN, draft="", reviewed=""):
    return [name, email, title, domain, "apollo", "0.0", "e", draft, reviewed, "r0", "t0"]


class EmaillessKey(sheets_fx.LiveCase):
    def test_same_first_name_different_titles_both_appended(self):
        google = sheets_fx.FakeGoogle(header=COLUMNS)
        batch = [(apollo_person("Avery", "CTO"), "e1"),
                 (apollo_person("Avery", "VP Engineering"), "e2")]
        self.assertEqual(self.writer(google).write(batch), WriteResult(2, 0, False))
        self.assertEqual([r[2] for r in google.rows], ["CTO", "VP Engineering"])

    def test_same_name_title_and_domain_skipped(self):
        google = sheets_fx.FakeGoogle(header=COLUMNS)
        batch = [(apollo_person("Avery", "CTO"), "e1"),
                 (apollo_person(" avery ", " cto ", DOMAIN.upper()), "e2")]
        self.assertEqual(self.writer(google).write(batch), WriteResult(1, 1, False))

    def test_rows_with_an_email_dedupe_on_email(self):
        google = sheets_fx.FakeGoogle(header=COLUMNS)
        batch = [(emailed("Avery Alder", "avery.one"), "e1"),
                 (emailed("Avery Alder", "avery.two"), "same name and title, other email"),
                 (emailed("Someone Else", "AVERY.ONE", title="VP Engineering"), "same email")]
        self.assertEqual(self.writer(google).write(batch), WriteResult(2, 1, False))
        self.assertEqual([r[1] for r in google.rows],
                         [sheets_fx.addr("avery.one"), sheets_fx.addr("avery.two")])

    def test_existing_row_with_the_same_key_skipped_across_runs(self):
        existing = [sheet_row("Avery", "", "CTO", draft="edited by a person", reviewed="yes")]
        google = sheets_fx.FakeGoogle(header=COLUMNS, rows=existing)
        w = self.writer(google)
        new = (apollo_person(" avery ", "cto ", DOMAIN.upper()), "again")
        self.assertEqual(w.write([new]), WriteResult(0, 1, False))
        self.assertEqual(w.write([new]), WriteResult(0, 1, False))
        self.assertEqual(google.rows, existing)
        self.assertNotIn("append", google.kinds())

    def test_changed_title_gives_a_second_row_and_never_updates(self):
        existing = [sheet_row("Avery", "", "CTO", draft="edited by a person", reviewed="yes"),
                    sheet_row("Blake", "", "Designer")]
        google = sheets_fx.FakeGoogle(header=COLUMNS, rows=[list(r) for r in existing])
        w = self.writer(google)
        result = w.write([(apollo_person("Avery", "Chief Technology Officer"), "new title"),
                          (apollo_person("Blake", "Designer"), "same")])
        self.assertEqual(result, WriteResult(1, 1, False))
        self.assertEqual(google.rows[:2], existing)
        self.assertEqual(google.rows[2][:3], ["Avery", "", "Chief Technology Officer"])
        self.assertNotIn("put", google.kinds())
        w.write([(apollo_person("Avery", "Chief Technology Officer"), "third run")])
        self.assertEqual(len(google.rows), 3)
        self.assertEqual(google.rows[:2], existing)


class HunterMissingEmail(unittest.TestCase):
    def test_missing_email_counted(self):
        rows = [hunter_fx.entry("Avery", "Alder", "CTO"),
                hunter_fx.entry("Blake", "Birch", "CTO", email=""),
                hunter_fx.entry("Casey", "Cedar", "CTO", email="not-an-address"),
                {**hunter_fx.entry("Devon", "Hazel", "CTO"), "value": None}]
        p, _, _ = hunter_fx.live(hunter_fx.raw(hunter_fx.body(rows)))
        self.assertEqual([x.name for x in p.find_people(hunter_fx.DOMAIN, (), 10)],
                         ["Avery Alder"])
        self.assertEqual(p.last_dropped, {"missing_email": 3})


def dropping_rows():
    return [hunter_fx.entry("Avery", "Alder", "CTO"),
            hunter_fx.entry("Blake", "Birch", "Designer"),
            hunter_fx.entry("Casey", "Cedar", "", email=hunter_fx.addr("casey"))]


class NoStaleCounts(unittest.TestCase):
    def test_providers_reset_before_anything_can_raise(self):
        p, _, _ = hunter_fx.live(hunter_fx.raw(hunter_fx.body(dropping_rows())),
                                 RawResponse(500, {}, b"{}"))
        p.find_people(hunter_fx.DOMAIN, ("CTO",), 10)
        self.assertEqual(p.last_dropped, {"filtered_by_title": 1, "missing_title": 1})
        with self.assertRaises(ProviderError):
            p.find_people(hunter_fx.DOMAIN, ("CTO",), 10)
        self.assertEqual(p.last_dropped, {})
        p.last_dropped = {"missing_name": 1}
        with self.assertRaises(ValueError):
            p.find_people("not a domain", (), 10)
        self.assertEqual(p.last_dropped, {})

        rows = [{"id": "synthetic-1", "title": "CTO"}]
        a, _, _ = apollo_fx.live(apollo_fx.raw({"people": rows}), RawResponse(500, {}, b"{}"))
        a.find_people(apollo_fx.DOMAIN, (), 10)
        self.assertEqual(a.last_dropped, {"missing_name": 1})
        with self.assertRaises(ProviderError):
            a.find_people(apollo_fx.DOMAIN, (), 10)
        self.assertEqual(a.last_dropped, {})

    def test_no_dropped_line_after_a_failed_lookup(self):
        t = hunter_fx.FakeTransport(hunter_fx.raw(hunter_fx.body(dropping_rows())),
                                    RawResponse(500, {}, b"{}"))
        provider = hunter.HunterProvider(
            CallBudget(5), dry_run=False, http=HttpClient((hunter.API_HOST,), transport=t),
            environ={hunter.API_KEY_ENV: hunter_fx.KEY})
        out = io.StringIO()
        _, counts, _ = run_mod.people_stage({"example.com": run_fx.EV, "example.invalid": run_fx.EV},
                                            provider, True, CallBudget(5), ("CTO",), None, out=out)
        self.assertEqual(counts["server_error"], 1)
        self.assertEqual(out.getvalue(),
                         "people dropped for example.com: filtered_by_title 1, missing_title 1\n")

    def test_stale_counts_on_a_raising_provider_are_not_printed(self):
        class Stale:
            name, required_env = "stale", ()
            last_dropped = {"missing_name": 2}

            def find_people(self, domain, titles, limit):
                raise ProviderError("server_error")

        out = io.StringIO()
        run_mod.people_stage({"example.com": run_fx.EV}, Stale(), True, CallBudget(5), (), None,
                             out=out)
        self.assertEqual(out.getvalue(), "")


class Source(unittest.TestCase):
    def test_apollo_test_renamed(self):
        names = set(vars(apollo_fx.Parsing))
        self.assertIn("test_apollo_returns_all_rows_titles_are_filtered_by_apollo", names)
        self.assertNotIn("test_title_filter_case_insensitive", names)

    def test_docs_say_apollo_titles_are_approximate(self):
        doc = " ".join((ROOT / "docs/apollo.md").read_text().split())
        self.assertIn("Apollo's title search is approximate, so a search for one title can return "
                      "adjacent roles.", doc)
        self.assertIn("Review titles before drafting.", doc)

    def test_forbidden_keyword_absent(self):
        word = re.compile("lam" + "bda", re.IGNORECASE)
        for name in ("src/repo_signals/sheets/writer.py", "src/repo_signals/providers/hunter.py",
                     "src/repo_signals/providers/apollo.py", "src/repo_signals/run.py",
                     "docs/google-sheets.md", "docs/apollo.md", "docs/run.md", "README.md",
                     "tests/test_row_key.py", "tests/test_apollo.py"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))


if __name__ == "__main__":
    unittest.main()
