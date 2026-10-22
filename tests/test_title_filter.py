"""Title filtering and counted drops in the Apollo and Hunter providers, and the run's dropped line.

Offline only: fake transports and synthetic data, shaped like the responses seen in the live
checks on 2026-10-22. Nothing here makes a real request.
"""
import io
import json
import re
import unittest
from pathlib import Path

import _src  # noqa: F401
import test_apollo as apollo_fx
import test_hunter as hunter_fx
import test_run as run_fx
from repo_signals import config
from repo_signals import run as run_mod
from repo_signals.budget import CallBudget
from repo_signals.http import HttpClient
from repo_signals.providers import apollo, hunter
from repo_signals.providers.fake import FakeProvider

ROOT = Path(__file__).resolve().parent.parent
SENTINELS = ("sentinelname", "sentineltitle", "sentinelmail")
EV = run_fx.EV


def apollo_row(first, title, n):
    """A search result as Apollo returned it live: first name, title, id and has_* flags."""
    row = {"id": f"synthetic-{n}", "has_email": True, "has_city": True}
    if first is not None:
        row["first_name"] = first
    if title is not None:
        row["title"] = title
    return row


def apollo_body(rows):
    return {"people": rows, "pagination": {"page": 1, "per_page": 25}}


class ApolloTitles(unittest.TestCase):
    def test_server_side_title_match_is_trusted(self):
        rows = [apollo_row("Avery", "Co-Founder & Chief Executive Officer", 1),
                apollo_row("Blake", "Chief Executive Officer", 2),
                apollo_row("Casey", "Founder and Managing Director", 3)]
        p, t, _ = apollo_fx.live(apollo_fx.raw(apollo_body(rows)))
        found = p.find_people(apollo_fx.DOMAIN, ("CEO",), 10)
        self.assertEqual([(x.name, x.title, x.email) for x in found],
                         [("Avery", "Co-Founder & Chief Executive Officer", None),
                          ("Blake", "Chief Executive Officer", None),
                          ("Casey", "Founder and Managing Director", None)])
        self.assertEqual(p.last_dropped, {})
        sent = json.loads(t.calls[0]["body"])
        self.assertEqual(sent[apollo.BODY_TITLES], ["CEO"])
        self.assertEqual(sent[apollo.BODY_DOMAINS], [apollo_fx.DOMAIN])

    def test_missing_name_and_title_are_counted(self):
        rows = [apollo_row(None, "CTO", 1), apollo_row("Avery", None, 2),
                apollo_row("Blake", "", 3), apollo_row("Casey", "CTO", 4)]
        p, _, _ = apollo_fx.live(apollo_fx.raw(apollo_body(rows)))
        self.assertEqual([x.name for x in p.find_people(apollo_fx.DOMAIN, (), 10)], ["Casey"])
        self.assertEqual(p.last_dropped, {"missing_name": 1, "missing_title": 2})

    def test_last_dropped_resets_on_every_lookup(self):
        dropping = apollo_fx.raw(apollo_body([apollo_row(None, "CTO", 1)]))
        clean = apollo_fx.raw(apollo_body([apollo_row("Casey", "CTO", 2)]))
        p, _, _ = apollo_fx.live(dropping, clean)
        self.assertEqual(p.last_dropped, {})
        p.find_people(apollo_fx.DOMAIN, (), 10)
        self.assertEqual(p.last_dropped, {"missing_name": 1})
        p.find_people(apollo_fx.DOMAIN, (), 10)
        self.assertEqual(p.last_dropped, {})


class HunterTitles(unittest.TestCase):
    def test_substring_filter_counts_what_it_drops(self):
        rows = [hunter_fx.entry("Avery", "Alder", "CTO"),
                hunter_fx.entry("Blake", "Birch", "Chief Technology Officer"),
                hunter_fx.entry("Casey", "Cedar", "Co-founder & cto"),
                hunter_fx.entry("Devon", "Hazel", "Designer")]
        p, _, _ = hunter_fx.live(hunter_fx.raw(hunter_fx.body(rows)))
        found = p.find_people(hunter_fx.DOMAIN, ("CTO",), 10)
        self.assertEqual([x.title for x in found], ["CTO", "Co-founder & cto"])
        self.assertEqual(p.last_dropped, {"filtered_by_title": 2})

    def test_missing_name_and_position_are_counted(self):
        rows = [hunter_fx.entry("", "", "CTO", email=hunter_fx.addr("anon")),
                hunter_fx.entry("Avery", "Alder", ""),
                hunter_fx.entry("Blake", "Birch", "CTO")]
        p, _, _ = hunter_fx.live(hunter_fx.raw(hunter_fx.body(rows)))
        self.assertEqual([x.name for x in p.find_people(hunter_fx.DOMAIN, (), 10)],
                         ["Blake Birch"])
        self.assertEqual(p.last_dropped, {"missing_name": 1, "missing_title": 1})

    def test_last_dropped_resets_on_every_lookup(self):
        p, _, _ = hunter_fx.live(hunter_fx.raw(hunter_fx.body(hunter_fx.ROWS)))
        self.assertEqual(p.last_dropped, {})
        p.find_people(hunter_fx.DOMAIN, ("CTO",), 10)
        self.assertEqual(p.last_dropped, {"filtered_by_title": 3})
        p.find_people(hunter_fx.DOMAIN, (), 10)
        self.assertEqual(p.last_dropped, {})


def sentinel_rows():
    """One kept person and three dropped ones whose name, title and email are sentinels."""
    mail = hunter_fx.addr("sentinelmail.one")
    return [hunter_fx.entry("Kept", "Person", "CTO"),
            hunter_fx.entry("Sentinelname", "Dropone", "SENTINELTITLE Designer", email=mail),
            hunter_fx.entry("Sentinelname", "Droptwo", "SENTINELTITLE Sales",
                            email=hunter_fx.addr("sentinelmail.two")),
            hunter_fx.entry("Sentinelname", "Dropthree", "", email=hunter_fx.addr("sentinelmail.x")),
            hunter_fx.entry("", "", "SENTINELTITLE CTO", email=hunter_fx.addr("sentinelmail.y"))]


class RunDroppedLine(run_fx.Case):
    def hunter_factory(self, rows):
        def factory(name, budget, registry):
            t = hunter_fx.FakeTransport(hunter_fx.raw(hunter_fx.body(rows)))
            return hunter.HunterProvider(
                budget, dry_run=False, http=HttpClient((hunter.API_HOST,), transport=t),
                environ={hunter.API_KEY_ENV: hunter_fx.KEY}), True
        return factory

    def test_code_only_line_per_domain(self):
        env = self.env(**{config.SIGNAL_FILE: self.signal_file(extra='titles = ["CTO"]')})
        self.assertIn(self.live(env, provider_factory=self.hunter_factory(sentinel_rows())),
                      (0, 3), self.text)
        for domain in ("example.com", "example.invalid"):
            self.assertIn(f"people dropped for {domain}: filtered_by_title 2, missing_name 1, "
                          "missing_title 1\n", self.text)
        lowered = self.text.lower()
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, lowered)

    def test_no_line_when_nothing_dropped(self):
        rows = [hunter_fx.entry("Avery", "Alder", "CTO"), hunter_fx.entry("Blake", "Birch", "CEO")]
        self.live(self.env(), provider_factory=self.hunter_factory(rows))
        self.assertIn("people: 2 domain(s) looked up", self.text)
        self.assertNotIn("people dropped", self.text)

    def test_dry_run_output_has_no_dropped_line(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(run_mod.run(environ={}, out=out, err=err), 0)
        self.assertNotIn("people dropped", out.getvalue() + err.getvalue())

    def test_only_codes_and_whole_numbers_reach_the_line(self):
        class Dropping(FakeProvider):
            last_dropped = {"Sentinelname Person": 4, "missing_name": 1, "filtered_by_title": True,
                            "missing_title": -1, "sentinelmail" + "@example.invalid": 2}

        out = io.StringIO()
        run_mod.people_stage({"example.com": EV}, Dropping(), False, CallBudget(5), (), None,
                             out=out)
        self.assertEqual(out.getvalue(), "people dropped for example.com: missing_name 1\n")


class Docs(unittest.TestCase):
    def test_first_names_and_checked_live(self):
        for name in ("README.md", "docs/apollo.md"):
            with self.subTest(file=name):
                self.assertIn("first names and titles", (ROOT / name).read_text())
        apollo_doc = " ".join((ROOT / "docs/apollo.md").read_text().split())
        self.assertIn("Checked live on 2026-10-22:", apollo_doc)
        self.assertIn("hides last names and returns only a `has_email` flag", apollo_doc)
        unchecked = apollo_doc.split("Still unchecked", 1)[1]
        for item in ("what `has_email` means", "`MAX_PER_PAGE`", "error ids", "`Retry-After`"):
            self.assertIn(item, unchecked)
        hunter_doc = " ".join((ROOT / "docs/hunter.md").read_text().split())
        self.assertIn('"CTO" will not match "Chief Technology Officer"', hunter_doc)

    def test_forbidden_keyword_absent(self):
        word = re.compile("lam" + "bda", re.IGNORECASE)
        for name in ("src/repo_signals/providers/apollo.py", "src/repo_signals/providers/hunter.py",
                     "src/repo_signals/run.py", "docs/apollo.md", "docs/hunter.md", "README.md",
                     "tests/test_title_filter.py", "tests/test_apollo.py"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))


if __name__ == "__main__":
    unittest.main()
