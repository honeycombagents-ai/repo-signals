"""Collector PR 2 tests: the field allowlist, the filters, the evidence templates and the report.
Offline and synthetic only. The records below are made up; they follow the ASSUMED shape of a
GitHub repository record and deliberately carry personal and free-text fields (marked SENTINEL)
that must never reach Evidence."""
import dataclasses
import re
import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import _src  # noqa: F401
from repo_signals.collector import evidence as evidence_mod
from repo_signals.collector.evidence import (KINDS, EvidenceError, build_evidence,
                                             try_build_evidence)
from repo_signals.collector.filters import (FilterReport, apply_filters, check_full_name,
                                            check_language, check_not_archived, check_not_fork,
                                            check_not_private, check_owner, check_url,
                                            check_window, filter_records, summarize_report)
from repo_signals.collector.github_fields import (REPO_KEYS, WEB_HOST, RepoFact, parse_repo)
from repo_signals.collector.signal import parse_signal
from repo_signals.drafting.base import Sender
from repo_signals.drafting.template import TemplateDrafter
from repo_signals.models import Evidence, Person

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 21)
DOMAIN = "example.com"
SENTINEL_EMAIL = "sentinel.person" + "@example.invalid"


def web(path):
    return f"https://{WEB_HOST}/{path}"


def raw(owner="example-org", owner_type="Organization", name="widget", **over):
    record = {
        "id": 101, "node_id": "SENTINELNODE", "name": name, "full_name": f"{owner}/{name}",
        "html_url": web(f"{owner}/{name}"), "language": "Python",
        "pushed_at": "2026-09-30T12:00:00Z", "created_at": "2020-01-01T00:00:00Z",
        "fork": False, "archived": False, "private": False,
        "description": "SENTINELDESC the best widget, ask SENTINELPERSON",
        "homepage": "https://example.com/SENTINELHOME", "readme": "SENTINELREADME text",
        "topics": ["examplelib", "SENTINELTOPIC"], "license": {"name": "SENTINELLICENSE"},
        "owner": {"login": owner, "type": owner_type, "id": 7, "name": "SENTINELOWNERNAME",
                  "email": SENTINEL_EMAIL, "blog": "https://example.com/SENTINELBLOG"},
        "contributors": [{"login": "SENTINELCONTRIB", "email": SENTINEL_EMAIL}],
        "commit": {"author": {"name": "SENTINELAUTHOR", "email": SENTINEL_EMAIL}},
        "text_matches": [{"fragment": "SENTINELFRAGMENT"}],
    }
    record.update(over)
    return record


def signal(language=None, days=None, terms=("examplelib",)):
    extra = []
    if language:
        extra.append(f'language = "{language}"')
    if days:
        extra.append(f"pushed_within_days = {days}")
    terms_toml = "[" + ", ".join(f'"{t}"' for t in terms) + "]"
    return parse_signal(f'name = "synthetic"\nterms = {terms_toml}\n' + "\n".join(extra) +
                        '\n[[targets]]\ndomain = "example.com"\norg = "example-org"\n')


def fact(**over):
    base = parse_repo(raw())
    return dataclasses.replace(base, **over)


def assert_clean(test, text):
    test.assertNotIn("SENTINEL", text.upper())
    test.assertNotIn("@", text)
    test.assertNotIn("://", text)


# ---- the allowlist ----

class Allowlist(unittest.TestCase):
    def test_reads_only_allowlisted_keys(self):
        names = [f.name for f in dataclasses.fields(RepoFact)]
        self.assertEqual(names, ["owner_login", "owner_type", "name", "full_name", "html_url",
                                 "language", "pushed_at", "fork", "archived", "private"])
        self.assertEqual(set(REPO_KEYS), {"name", "full_name", "html_url", "language",
                                          "pushed_at", "fork", "archived", "private", "owner"})
        f = parse_repo(raw())
        self.assertEqual(f, RepoFact("example-org", "Organization", "widget", "example-org/widget",
                                     web("example-org/widget"), "Python", date(2026, 9, 30),
                                     False, False, False))
        text = repr(f) + repr(dataclasses.asdict(f))
        self.assertNotIn("SENTINEL", text.upper())
        self.assertNotIn(SENTINEL_EMAIL, text)

    def test_extra_keys_never_matter(self):
        bare = {k: raw()[k] for k in REPO_KEYS}
        bare["owner"] = {"login": "example-org", "type": "Organization"}
        self.assertEqual(parse_repo(bare), parse_repo(raw()))

    def test_personal_and_free_text_fields_never_reach_evidence(self):
        facts, report = filter_records([raw()], signal(), TODAY)
        self.assertEqual(report.kept, 1)
        for kind in KINDS:
            with self.subTest(kind=kind):
                e = build_evidence(facts[0], "examplelib", kind, DOMAIN, TODAY,
                                   terms=("examplelib",))
                assert_clean(self, e.summary)
                self.assertNotIn("SENTINEL", repr(e).upper())
                self.assertNotIn(SENTINEL_EMAIL, repr(e))
                self.assertNotIn("best widget", e.summary)

    def test_malformed_records_give_none_and_never_raise(self):
        class Exploding(dict):
            def get(self, *a):
                raise RuntimeError("SENTINEL")

        bad = [None, [], "x", 5, {}, Exploding(raw()), raw(owner=None),
               {**raw(), "owner": "example-org"}, {**raw(), "owner": {"login": "example-org"}},
               {**raw(), "fork": "false"}, {**raw(), "archived": None}, {**raw(), "private": 0},
               {**raw(), "pushed_at": "2026-09-30"}, {**raw(), "pushed_at": "2026-02-30T00:00:00Z"},
               {**raw(), "pushed_at": 20260930}, {**raw(), "name": ""}, {**raw(), "name": 5},
               {**raw(), "name": "x" * 101}, {**raw(), "full_name": None},
               {**raw(), "html_url": ["x"]}, {**raw(), "language": 3},
               {**raw(), "language": ""}, {**raw(), "owner": {"login": "x" * 40,
                                                            "type": "Organization"}},
               {**raw(), "owner": {"login": object(), "type": "Organization"}}]
        for record in bad:
            with self.subTest(record=type(record).__name__):
                self.assertIsNone(parse_repo(record))
        missing = raw()
        del missing["pushed_at"]
        self.assertIsNone(parse_repo(missing))
        self.assertIsNone(parse_repo({**raw(), "language": None}).language)


# ---- filters ----

class Filters(unittest.TestCase):
    def test_user_owned_repos_dropped(self):
        f = parse_repo(raw(owner="SENTINELUSER", owner_type="User"))
        self.assertEqual(check_owner(f), (False, "owner_not_org"))
        for t in ("Bot", "organization", ""):
            self.assertEqual(check_owner(fact(owner_type=t)), (False, "owner_not_org"))
        self.assertEqual(check_owner(fact()), (True, None))

    def test_fork_archived_private_dropped(self):
        self.assertEqual(check_not_fork(fact(fork=True)), (False, "fork"))
        self.assertEqual(check_not_archived(fact(archived=True)), (False, "archived"))
        self.assertEqual(check_not_private(fact(private=True)), (False, "private"))
        for check in (check_not_fork, check_not_archived, check_not_private):
            self.assertEqual(check(fact()), (True, None))

    def test_url_shapes(self):
        cases = [("http://" + WEB_HOST + "/example-org/widget", "url_not_https"),
                 ("ftp://" + WEB_HOST + "/example-org/widget", "url_not_https"),
                 ("https://example.com/example-org/widget", "url_other_host"),
                 ("https://evil." + WEB_HOST + "/example-org/widget", "url_other_host"),
                 ("https://" + WEB_HOST + ":8443/example-org/widget", "url_other_host"),
                 ("https://" + WEB_HOST.upper() + "/example-org/widget", "url_other_host"),
                 ("https://user" + "@" + WEB_HOST + "/example-org/widget", "url_other_host"),
                 (web("example-org/widget/tree/main"), "url_extra_path"),
                 (web("example-org/widget/"), "url_extra_path"),
                 (web("example-org"), "url_extra_path"),
                 (web("example-org/widget?tab=readme"), "url_extra_path"),
                 (web("example-org/widget#readme"), "url_extra_path"),
                 (web("example-org/other"), "url_full_name_mismatch"),
                 (web("Example-Org/widget"), "url_full_name_mismatch"),
                 (web("example-labs/widget"), "url_full_name_mismatch"),
                 ("https://[bad/example-org/widget", "url_malformed")]
        for url, code in cases:
            with self.subTest(url=url):
                self.assertEqual(check_url(fact(html_url=url)), (False, code))
        self.assertEqual(check_url(fact()), (True, None))

    def test_full_name_pattern(self):
        for full_name, owner, name in (("example-org/wid get", "example-org", "wid get"),
                                       ("example-org/..", "example-org", ".."),
                                       ("-bad/widget", "-bad", "widget"),
                                       ("example-org/widget", "example-org", "other"),
                                       ("a/b/c", "a", "b/c"), ("x" * 40 + "/w", "x" * 40, "w"),
                                       ("example-org/" + "w" * 101, "example-org", "w" * 101)):
            with self.subTest(full_name=full_name[:30]):
                self.assertEqual(check_full_name(fact(full_name=full_name, owner_login=owner,
                                                      name=name)), (False, "bad_full_name"))
        self.assertEqual(check_full_name(fact(full_name="a-b/c.d_e-f", owner_login="a-b",
                                              name="c.d_e-f")), (True, None))

    def test_window_boundaries(self):
        f365 = fact(pushed_at=TODAY - timedelta(days=365))
        f366 = fact(pushed_at=TODAY - timedelta(days=366))
        self.assertEqual(check_window(f365, 365, TODAY), (True, None))
        self.assertEqual(check_window(f366, 365, TODAY), (False, "outside_window"))
        self.assertEqual(check_window(fact(pushed_at=TODAY), 1, TODAY), (True, None))
        self.assertEqual(check_window(fact(pushed_at=TODAY - timedelta(days=1)), 1, TODAY),
                         (True, None))
        self.assertEqual(check_window(fact(pushed_at=TODAY - timedelta(days=2)), 1, TODAY),
                         (False, "outside_window"))
        self.assertEqual(check_window(fact(pushed_at=TODAY + timedelta(days=1)), 365, TODAY),
                         (False, "pushed_in_future"))
        with self.assertRaises(TypeError):
            check_window(fact(), 365, "2026-10-21")
        self.assertEqual(apply_filters(f366, signal(), TODAY), (False, "outside_window"))
        self.assertEqual(apply_filters(f365, signal(), TODAY), (True, None))
        self.assertEqual(apply_filters(fact(pushed_at=TODAY - timedelta(days=31)),
                                       signal(days=30), TODAY), (False, "outside_window"))

    def test_language(self):
        self.assertEqual(check_language(fact(language="Python"), None), (True, None))
        self.assertEqual(check_language(fact(language=None), None), (True, None))
        self.assertEqual(check_language(fact(language="Python"), "python"), (True, None))
        self.assertEqual(check_language(fact(language="PYTHON"), "Python"), (True, None))
        self.assertEqual(check_language(fact(language="Go"), "Python"),
                         (False, "language_mismatch"))
        self.assertEqual(check_language(fact(language=None), "Python"),
                         (False, "language_mismatch"))
        self.assertEqual(apply_filters(fact(language="Go"), signal(language="Python"), TODAY),
                         (False, "language_mismatch"))

    def test_every_drop_code_counted(self):
        records = [raw(), raw(name="second"), None, {"name": "x"},
                   raw(owner="SENTINELUSER", owner_type="User"), raw(fork=True),
                   raw(archived=True), raw(private=True),
                   {**raw(), "full_name": "example-org/other"},
                   {**raw(), "html_url": "http://" + WEB_HOST + "/example-org/widget"},
                   {**raw(), "html_url": "https://example.com/example-org/widget"},
                   {**raw(), "html_url": web("example-org/widget/tree")},
                   {**raw(), "full_name": "example-org/widget", "name": "widget",
                    "html_url": web("example-org/widget2")},
                   {**raw(), "pushed_at": "2025-01-01T00:00:00Z"},
                   {**raw(), "pushed_at": "2026-12-01T00:00:00Z"},
                   {**raw(), "language": "Go"}]
        facts, report = filter_records(records, signal(language="Python"), TODAY)
        self.assertEqual(len(facts), 2)
        self.assertEqual(report.kept, 2)
        self.assertEqual(report.dropped, Counter({
            "malformed_record": 2, "owner_not_org": 1, "fork": 1, "archived": 1, "private": 1,
            "bad_full_name": 1, "url_not_https": 1, "url_other_host": 1, "url_extra_path": 1,
            "url_full_name_mismatch": 1, "outside_window": 1, "pushed_in_future": 1,
            "language_mismatch": 1}))
        self.assertEqual(sum(report.dropped.values()) + report.kept, len(records))
        lines = summarize_report(report)
        self.assertEqual(lines[0], "kept: 2")
        codes = [line.split(": ", 1)[1] for line in lines[1:]]
        self.assertEqual(codes, sorted(codes))
        self.assertIn("dropped 2: malformed_record", lines)
        self.assertIn("dropped 1: owner_not_org", lines)
        for line in lines:
            self.assertRegex(line, r"\A(kept: [0-9]+|dropped [0-9]+: [a-z_]+)\Z")
        self.assertNotIn("SENTINEL", repr(report).upper())


# ---- evidence ----

class EvidenceTemplates(unittest.TestCase):
    TERMS = ("examplelib", "example-sdk")

    def build(self, f=None, term="examplelib", kind="repo", domain=DOMAIN, today=TODAY,
              terms=None):
        return build_evidence(f or fact(), term, kind, domain, today,
                              terms=self.TERMS if terms is None else terms)

    def drops(self, code, **kw):
        with self.assertRaises(EvidenceError) as cm:
            self.build(**kw)
        self.assertEqual(cm.exception.code, code)
        self.assertNotIn("SENTINEL", repr(cm.exception).upper() + str(cm.exception.args).upper())

    def test_both_kinds_and_both_language_clauses(self):
        cases = [("repo", "Python", "The public repository example-org/widget matches the term "
                  "examplelib and its primary language on GitHub is Python, last pushed on "
                  "2026-09-30."),
                 ("repo", None, "The public repository example-org/widget matches the term "
                  "examplelib, last pushed on 2026-09-30."),
                 ("topic", "C++", "The public repository example-org/widget is tagged with the "
                  "topic examplelib and its primary language on GitHub is C++, last pushed on "
                  "2026-09-30."),
                 ("topic", None, "The public repository example-org/widget is tagged with the "
                  "topic examplelib, last pushed on 2026-09-30.")]
        for kind, language, summary in cases:
            with self.subTest(kind=kind, language=language):
                e = self.build(fact(language=language), kind=kind)
                self.assertEqual(e, Evidence(DOMAIN, summary, web("example-org/widget"), kind))
        self.assertEqual(KINDS, ("repo", "topic"))

    def test_templates_match_the_design_doc(self):
        doc = (ROOT / "docs/collector-design.md").read_text()
        for kind, template in evidence_mod.TEMPLATES.items():
            self.assertIn(f"`{template}`", doc)
        self.assertIn(f"`{evidence_mod.LANGUAGE_CLAUSE}`", doc)

    def test_term_must_be_one_of_the_signals_terms(self):
        self.drops("term_not_in_signal", term="otherlib")
        self.drops("term_not_in_signal", term="ExampleLib")
        self.drops("term_not_in_signal", term="examplelib", terms=("otherlib",))
        self.drops("term_not_in_signal", term="examplelib", terms=["examplelib"])
        self.drops("term_not_in_signal", term="bad term", terms=("bad term",))
        self.drops("term_not_in_signal", term=None, terms=(None,))
        self.assertEqual(self.build(term="example-sdk").summary.count("example-sdk"), 1)

    def test_no_file_kind(self):
        for kind in ("file", "topics", "Repo", "", None):
            with self.subTest(kind=kind):
                self.drops("bad_kind", kind=kind)

    def test_language_allowlist(self):
        for language in ("Jupyter Notebook", "Objective-C", "F#", "C++", "Vim Script"):
            with self.subTest(language=language):
                self.assertIn(language, self.build(fact(language=language)).summary)
        for language in ("SENTINEL<b>", "x" * 33, "Py\nthon", "Py  thon", " Python",
                         "Python ", "SENTINEL" + "@lang", "1C", "SENTINEL://x"):
            with self.subTest(language=language):
                self.drops("bad_language", f=fact(language=language))

    def test_dates(self):
        self.assertIn("last pushed on 2026-10-21.", self.build(fact(pushed_at=TODAY)).summary)
        self.drops("bad_pushed_date", f=fact(pushed_at=TODAY + timedelta(days=1)))
        with self.assertRaises(TypeError):
            self.build(today="2026-10-21")

    def test_length_cap(self):
        owner, name = "o" * 39, "n" * 100
        long_fact = fact(owner_login=owner, name=name, full_name=f"{owner}/{name}",
                         html_url=web(f"{owner}/{name}"), language="L" * 32)
        term = "t" * 60
        self.drops("summary_too_long", f=long_fact, term=term, terms=(term,), kind="topic")
        short = self.build(fact(language=None), kind="repo")
        self.assertLessEqual(len(short.summary), 300)

    def test_no_at_and_no_url_inside_a_summary(self):
        for kind in KINDS:
            assert_clean(self, self.build(kind=kind).summary)
        with mock.patch.dict(evidence_mod.TEMPLATES, {"repo": "Contact {full_name} @ "
                                                     "{term}{language_clause} {pushed_date}."}):
            self.drops("summary_has_at")
        with mock.patch.dict(evidence_mod.TEMPLATES, {"repo": "See https://example.com/ "
                                                     "{full_name} {term}{language_clause} "
                                                     "{pushed_date}."}):
            self.drops("summary_has_url")
        with mock.patch.dict(evidence_mod.TEMPLATES, {"repo": "See www.example.com "
                                                     "{full_name} {term}{language_clause} "
                                                     "{pushed_date}."}):
            self.drops("summary_has_url")

    def test_defence_in_depth(self):
        self.drops("owner_not_org", f=fact(owner_type="User"))
        self.drops("bad_full_name", f=fact(full_name="SENTINEL USER/x"))
        self.drops("url_not_https", f=fact(html_url="http://" + WEB_HOST + "/example-org/widget"))
        self.drops("url_other_host", f=fact(html_url="https://example.com/example-org/widget"))
        self.drops("bad_evidence", domain="not a domain")
        self.drops("bad_fact", f="SENTINEL")

    def test_try_build_counts(self):
        report = FilterReport()
        good = try_build_evidence(fact(), "examplelib", "repo", DOMAIN, TODAY,
                                  terms=self.TERMS, report=report)
        bad = try_build_evidence(fact(), "examplelib", "file", DOMAIN, TODAY,
                                 terms=self.TERMS, report=report)
        self.assertIsInstance(good, Evidence)
        self.assertIsNone(bad)
        self.assertEqual((report.kept, report.dropped), (1, Counter({"bad_kind": 1})))

    def test_evidence_passes_the_drafting_guard(self):
        e = self.build(kind="topic")
        sender = Sender("Avery Alder", "Engineer", "Example Org", "1 Example Street",
                        "Reply stop to opt out.")
        p = Person(name="Blake Birch", email=None, title="CTO", company_domain=DOMAIN,
                   source="fake", confidence=0.5)
        draft = TemplateDrafter().draft(p, (e,), sender)
        self.assertIn(e.summary, draft.body)
        self.assertEqual(draft.grounded_on, (web("example-org/widget"),))


class Source(unittest.TestCase):
    def test_forbidden_keyword_absent(self):
        word = re.compile("lam" + "bda", re.IGNORECASE)
        for name in ("src/repo_signals/collector/github_fields.py",
                     "src/repo_signals/collector/filters.py",
                     "src/repo_signals/collector/evidence.py",
                     "tests/test_collector_evidence.py", "docs/evidence.md",
                     "docs/collector-design.md"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))

    def test_web_host_written_once(self):
        host = WEB_HOST
        hits = [p for p in (ROOT / "src").rglob("*.py") if host in p.read_text()]
        self.assertEqual([p.name for p in hits], ["github_fields.py"])
        for p in (ROOT / "tests").glob("*.py"):
            self.assertNotIn(host, p.read_text(), p.name)


if __name__ == "__main__":
    unittest.main()
