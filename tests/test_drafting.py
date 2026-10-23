"""Drafting tests: strict templates, grounding, the compliance guard, suppression and
fill_drafts. Offline and synthetic only; nothing is ever sent."""
import ast
import io
import os
import re
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path

import _src  # noqa: F401
from repo_signals import config
from repo_signals.config import Settings
from repo_signals.drafting import template as template_mod
from repo_signals.drafting.base import (Draft, DraftError, Drafter, DraftResult, Sender,
                                        sender_from_env)
from repo_signals.drafting.guard import (MAX_BODY_CHARS, RELATIONSHIP_PHRASES, GuardedDrafter,
                                         check_draft)
from repo_signals.drafting.llm import LlmDrafter
from repo_signals.drafting.sheet import DRAFT_COL, REVIEWED_COL, draft_for, fill_drafts
from repo_signals.drafting.suppression import (MAX_SUPPRESSION_BYTES, Suppression,
                                               load_suppression, parse_suppression,
                                               suppression_from_env)
from repo_signals.drafting.template import (ALLOWED_PLACEHOLDERS, TEMPLATE_DIR, TemplateDrafter,
                                            load_template, parse_template, render)
from repo_signals.models import Evidence, Person
from repo_signals.sheets.layout import COLUMNS

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = "acme.example.invalid"
URL = "https://code.example.com/acme/widget"
SUMMARY = "The org publishes a repo named widget that uses library gadget."
SENDER = Sender(name="SENTINELNAME Sender", role="SENTINELROLE", org_name="SENTINELORG",
                postal_address="SENTINELPOSTAL 1 Example Street\nExample City 00000",
                optout_text="SENTINELOPTOUT: reply stop and we will not write again.")
SENDER_SENTINELS = ("SENTINELNAME", "SENTINELROLE", "SENTINELORG", "SENTINELPOSTAL",
                    "SENTINELOPTOUT")
EVIDENCE = (Evidence(DOMAIN, SUMMARY, URL, "repo"),)


def addr(local, domain=DOMAIN):
    return local + "@" + domain


def person(name="Blake Birch", email="default", title="CTO", domain=DOMAIN):
    return Person(name=name, email=addr("blake") if email == "default" else email, title=title,
                  company_domain=domain, source="fake", confidence=0.8)


def everything(e):
    return "".join(traceback.format_exception(e)) + repr(e) + str(e.args)


class Raises:
    def raises(self, code, fn, *args, **kwargs):
        with self.assertRaises(DraftError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.code, code)
        text = everything(cm.exception)
        for s in SENDER_SENTINELS:
            self.assertNotIn(s, text)
        self.assertNotIn(SUMMARY, text)
        self.assertNotIn(addr("blake"), text)
        return cm.exception


class FixedDrafter:
    """Returns whatever draft it was given, to exercise the guard."""

    name = "fixed"

    def __init__(self, draft):
        self.result = draft
        self.calls = 0

    def draft(self, person, evidence, sender):
        self.calls += 1
        return self.result


def good_body(extra=""):
    return (f"Hi Blake,\n\n{SUMMARY}\nSource: {URL}\n{extra}\n\nSENTINELNAME Sender\n"
            f"{SENDER.postal_address}\n\n{SENDER.optout_text}")


def good_draft(**kw):
    fields = {"subject": f"A public signal from {DOMAIN}", "body": good_body(),
              "grounded_on": (URL,)}
    fields.update(kw)
    return Draft(**fields)


# ---- Evidence ----

class EvidenceModel(unittest.TestCase):
    def test_valid(self):
        e = Evidence(DOMAIN, SUMMARY, URL, "repo")
        self.assertEqual((e.company_domain, e.summary, e.url, e.kind), (DOMAIN, SUMMARY, URL, "repo"))

    def test_invalid(self):
        cases = [("not a domain", SUMMARY, URL, "repo"), (DOMAIN, "", URL, "repo"),
                 (DOMAIN, " leading", URL, "repo"), (DOMAIN, "two\nlines", URL, "repo"),
                 (DOMAIN, "x" * 301, URL, "repo"), (DOMAIN, SUMMARY, "http://code.example.com/x",
                                                    "repo"),
                 (DOMAIN, SUMMARY, "https://code.example.com/a b", "repo"),
                 (DOMAIN, SUMMARY, "https:///nohost", "repo"), (DOMAIN, SUMMARY, URL, "Repo"),
                 (DOMAIN, SUMMARY, URL, ""), (DOMAIN, SUMMARY, None, "repo")]
        for args in cases:
            with self.subTest(args=args[1:]), self.assertRaises(ValueError):
                Evidence(*args)

    def test_person_unchanged(self):
        self.assertEqual([f for f in Person.__dataclass_fields__],
                         ["name", "email", "title", "company_domain", "source", "confidence"])


# ---- strict templates ----

class StrictRendering(Raises, unittest.TestCase):
    VALUES = {"first_name": "Blake", "title": "CTO", "postal_address": "1 Street\nCity"}

    def test_unknown_placeholders(self):
        for text in ("{foo}", "{First_Name}", "{first_name.__class__}", "{}", "{ title }",
                     "{0}", "{title!r}", "{title:>10}"):
            with self.subTest(text=text):
                self.raises("unknown_placeholder", render, text, self.VALUES)

    def test_stray_braces(self):
        for text in ("a { b", "a } b", "{{title}}", "{title"):
            with self.subTest(text=text):
                with self.assertRaises(DraftError) as cm:
                    render(text, self.VALUES)
                self.assertIn(cm.exception.code, ("template_malformed", "unknown_placeholder"))

    def test_missing_values(self):
        for value in (None, "", "   ", 5):
            with self.subTest(value=value):
                self.raises("missing_title", render, "x {title} y", {"title": value})

    def test_newline_in_single_line_field(self):
        for field in ("first_name", "title", "company_domain", "evidence_summary",
                      "evidence_url", "sender_name", "sender_role", "org_name"):
            for bad in ("a\nb", "a\rb"):
                with self.subTest(field=field, bad=bad):
                    self.raises(f"newline_in_{field}", render, "{" + field + "}", {field: bad})

    def test_multiline_fields_allow_newlines_only(self):
        self.assertEqual(render("{postal_address}", {"postal_address": "1 Street\nCity"}),
                         "1 Street\nCity")
        self.raises("control_character_in_postal_address", render, "{postal_address}",
                    {"postal_address": "1 Street\x1b[31m"})
        self.raises("control_character_in_title", render, "{title}", {"title": "CTO\x07"})

    def test_values_are_plain_text(self):
        values = {"first_name": "{title}", "title": "<b>{optout_text}</b>"}
        self.assertEqual(render("{first_name} / {title}", values),
                         "{title} / <b>{optout_text}</b>")

    def test_parse_template_failures(self):
        good = ("Subject: About {company_domain}\n\n{evidence_summary} {evidence_url}\n"
                "{postal_address}\n{optout_text}\n")
        self.assertEqual(parse_template(good).subject, "About {company_domain}")
        cases = [(good.replace("Subject: ", "Topic: "), "template_malformed"),
                 (good.replace("\n\n", "\n", 1), "template_malformed"),
                 (good.replace("{company_domain}", "{first_name}"), "subject_placeholder_not_allowed"),
                 (good.replace("{company_domain}", "{evidence_summary}"),
                  "subject_placeholder_not_allowed"),
                 (good.replace("{optout_text}", ""), "template_missing_required_placeholder"),
                 (good.replace("{postal_address}", ""), "template_missing_required_placeholder"),
                 (good.replace("{evidence_url}", ""), "template_missing_required_placeholder"),
                 (good.replace("{evidence_summary}", ""), "template_missing_required_placeholder"),
                 (good.replace("\n", "\r\n"), "template_malformed"),
                 (good + "{unknown}", "unknown_placeholder"), ("Subject: x\n\n", "template_malformed"),
                 ("Subject: \n\nbody", "template_malformed"), (None, "template_malformed")]
        for text, code in cases:
            with self.subTest(code=code, text=(text or "")[:30]):
                self.raises(code, parse_template, text)

    def test_load_template(self):
        self.raises("unknown_template", load_template, "nosuch")
        self.raises("unknown_template", load_template, "../intro")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mine.txt")
            self.raises("template_unreadable", load_template, "mine", path)
            with open(path, "wb") as fh:
                fh.write(b"Subject: Hi {company_domain}\n\n{evidence_summary}\n{evidence_url}\n"
                         b"{postal_address}\n{optout_text}\n")
            self.assertEqual(TemplateDrafter(path=path).template.subject, "Hi {company_domain}")
            with open(path, "wb") as fh:
                fh.write(b"\xff\xfe")
            self.raises("template_malformed", load_template, "mine", path)
            with open(path, "wb") as fh:
                fh.write(b"x" * (template_mod.MAX_TEMPLATE_BYTES + 1))
            self.raises("template_too_large", load_template, "mine", path)


class ShippedTemplates(unittest.TestCase):
    def test_every_template_file_uses_only_allowed_placeholders(self):
        files = sorted(TEMPLATE_DIR.glob("*"))
        self.assertEqual([f.name for f in files], ["follow_up.txt", "intro.txt"])
        for f in files:
            with self.subTest(file=f.name):
                text = f.read_text()
                names = re.findall(r"\{([^{}]*)\}", text)
                self.assertTrue(names)
                self.assertTrue(set(names) <= set(ALLOWED_PLACEHOLDERS), names)
                self.assertEqual(text.count("{"), len(names))
                self.assertEqual(text.count("}"), len(names))
                parse_template(text)

    def test_templates_are_neutral(self):
        for f in TEMPLATE_DIR.glob("*.txt"):
            with self.subTest(file=f.name):
                text = f.read_text()
                literal = re.sub(r"\{[a-z_]+\}", "", text)
                self.assertNotIn("http", literal)
                self.assertNotIn("@", literal)
                self.assertNotIn("www", literal)
                self.assertIsNone(re.search(r"[0-9]", literal))
                for phrase in RELATIONSHIP_PHRASES:
                    self.assertNotIn(phrase, text.lower())

    def test_both_render_and_pass_the_guard(self):
        for name in ("intro", "follow_up"):
            with self.subTest(name=name):
                d = TemplateDrafter(name).draft(person(), EVIDENCE, SENDER)
                self.assertEqual(d.grounded_on, (URL,))
                self.assertIn(SUMMARY, d.body)
                self.assertIn(URL, d.body)
                self.assertIn(DOMAIN, d.subject)
                self.assertIn("Hi Blake,", d.body)
                self.assertIn(SENDER.postal_address, d.body)
                self.assertIn(SENDER.optout_text, d.body)
                self.assertNotIn("{", d.subject + d.body)
                self.assertNotIn(addr("blake"), d.body)


# ---- grounding ----

class Grounding(Raises, unittest.TestCase):
    def test_no_evidence_no_draft(self):
        d = TemplateDrafter()
        self.raises("no_evidence", d.draft, person(), (), SENDER)
        other = (Evidence("other.example.invalid", SUMMARY, URL, "repo"),)
        self.raises("no_evidence", d.draft, person(), other, SENDER)
        self.raises("bad_evidence", d.draft, person(), ("not evidence",), SENDER)
        self.raises("bad_evidence", d.draft, person(), None, SENDER)

    def test_only_this_company_evidence_is_used(self):
        mixed = (Evidence("other.example.invalid", "Other fact.", "https://code.example.com/o",
                          "repo"),) + EVIDENCE
        d = TemplateDrafter().draft(person(domain=DOMAIN.upper()), mixed, SENDER)
        self.assertEqual(d.grounded_on, (URL,))
        self.assertNotIn("Other fact.", d.body)
        self.assertNotIn("code.example.com/o", d.body)

    def test_url_and_summary_present_verbatim(self):
        d = TemplateDrafter().draft(person(), EVIDENCE, SENDER)
        self.assertIn(f"\n{SUMMARY}\n", d.body)
        self.assertIn(URL, d.body)

    def test_no_draft_for_a_person_without_a_name(self):
        class Nameless:
            name, email, title, company_domain = "", addr("x"), "CTO", DOMAIN

        for p in (person(name="?"), person(name="-- --"), person(name="123"), Nameless()):
            with self.subTest(name=p.name):
                self.raises("no_name", TemplateDrafter().draft, p, EVIDENCE, SENDER)

    def test_subject_is_built_from_the_domain(self):
        self.assertEqual(TemplateDrafter("intro").draft(person(), EVIDENCE, SENDER).subject,
                         f"A public signal from {DOMAIN}")
        self.assertEqual(TemplateDrafter("follow_up").draft(person(), EVIDENCE, SENDER).subject,
                         f"{DOMAIN}: one short, optional note")

    def test_no_invented_relationship(self):
        for phrase in ("As we discussed", "great to meet you", "Following up on our call",
                       "Thanks for your time", "circling back"):
            with self.subTest(phrase=phrase):
                g = GuardedDrafter(FixedDrafter(good_draft(body=good_body(phrase))))
                self.raises("relationship_claim", g.draft, person(), EVIDENCE, SENDER)
        ev = (Evidence(DOMAIN, "The README says the project was started as discussed in "
                       "issue four.", URL, "repo"),)
        body = (f"Hi,\n\n{ev[0].summary}\n{URL}\n{SENDER.postal_address}\n{SENDER.optout_text}")
        check_draft(good_draft(body=body), person(), ev, SENDER)


# ---- the compliance guard ----

class Compliance(Raises, unittest.TestCase):
    FIELDS = {"name": config.SENDER_NAME, "role": config.SENDER_ROLE,
              "org_name": config.ORG_NAME, "postal_address": config.POSTAL_ADDRESS,
              "optout_text": config.OPTOUT_TEXT}

    def without(self, field, value=None):
        values = {f: getattr(SENDER, f) for f in self.FIELDS}
        values[field] = value
        return Sender(**values)

    def test_each_missing_sender_variable_refuses_by_name(self):
        for field, var in self.FIELDS.items():
            for value in (None, "", "  "):
                with self.subTest(field=field, value=value):
                    for drafter in (TemplateDrafter(), GuardedDrafter(FixedDrafter(good_draft()))):
                        e = self.raises("sender_missing", drafter.draft, person(), EVIDENCE,
                                        self.without(field, value))
                        self.assertEqual(e.var, var)
                        self.assertEqual(str(e), f"sender_missing: {var}")

    def test_not_a_sender(self):
        self.raises("sender_missing", TemplateDrafter().draft, person(), EVIDENCE, None)

    def test_sender_from_env(self):
        env = {config.SENDER_NAME: "SENTINELNAME A", config.SENDER_ROLE: "SENTINELROLE",
               config.ORG_NAME: "SENTINELORG", config.POSTAL_ADDRESS: "SENTINELPOSTAL\nCity",
               config.OPTOUT_TEXT: "SENTINELOPTOUT"}
        s = sender_from_env(env)
        self.assertEqual(s.postal_address, "SENTINELPOSTAL\nCity")
        self.assertNotIn("SENTINEL", repr(s) + str(s))
        missing = sender_from_env({k: v for k, v in env.items() if k != config.OPTOUT_TEXT})
        self.assertIsNone(missing.optout_text)
        for var, bad in ((config.SENDER_NAME, "SENTINELNAME\nX"),
                         (config.ORG_NAME, " SENTINELORG"),
                         (config.POSTAL_ADDRESS, "SENTINELPOSTAL\n\nCity"),
                         (config.OPTOUT_TEXT, "SENTINELOPTOUT\x1b")):
            with self.subTest(var=var):
                e = self.raises("sender_malformed", sender_from_env, {**env, var: bad})
                self.assertEqual(e.var, var)

    def test_draft_error_never_holds_a_value(self):
        for code, var in (("SENTINELNAME bad", None), ("ok", "SENTINELNAME"),
                          ("ok", "REPO_SIGNALS_X; SENTINEL"), ("ok", "HOME")):
            e = DraftError(code, var)
            self.assertNotIn("SENTINEL", str(e) + repr(e))
            self.assertNotIn("HOME", str(e))


# ---- the guard on any drafter's output ----

class Guard(Raises, unittest.TestCase):
    def guarded(self, **kw):
        self.inner = FixedDrafter(good_draft(**kw))
        return GuardedDrafter(self.inner)

    def test_good_draft_passes(self):
        self.assertEqual(self.guarded().draft(person(), EVIDENCE, SENDER).grounded_on, (URL,))

    def test_foreign_urls_rejected(self):
        for extra in ("See https://evil.example.com/x", "visit www.evil.example.com",
                      "http://code.example.com/acme/widget2", f"{URL}/../other",
                      "HTTPS://EVIL.EXAMPLE.COM"):
            with self.subTest(extra=extra):
                self.raises("foreign_url", self.guarded(body=good_body(extra)).draft, person(),
                            EVIDENCE, SENDER)
        self.raises("url_in_subject", self.guarded(subject=f"{DOMAIN} https://x.example.com")
                    .draft, person(), EVIDENCE, SENDER)

    def test_optout_url_is_allowed(self):
        sender = Sender(SENDER.name, SENDER.role, SENDER.org_name, SENDER.postal_address,
                        "Opt out at https://example.com/optout.")
        body = f"{SUMMARY}\n{URL}\n{sender.postal_address}\n{sender.optout_text}"
        check_draft(good_draft(body=body), person(), EVIDENCE, sender)

    def test_missing_optout_or_postal_address(self):
        self.raises("optout_missing", self.guarded(
            body=good_body().replace(SENDER.optout_text, "")).draft, person(), EVIDENCE, SENDER)
        self.raises("postal_address_missing", self.guarded(
            body=good_body().replace(SENDER.postal_address, "")).draft, person(), EVIDENCE,
            SENDER)

    def test_length_caps(self):
        filler = "a" * (MAX_BODY_CHARS - len(good_body()))
        self.assertEqual(len(good_body(filler)), MAX_BODY_CHARS)
        self.guarded(body=good_body(filler)).draft(person(), EVIDENCE, SENDER)
        self.raises("body_too_long", self.guarded(body=good_body(filler + "a")).draft, person(),
                    EVIDENCE, SENDER)
        self.raises("subject_too_long", self.guarded(subject=DOMAIN + " " + "s" * 120).draft,
                    person(), EVIDENCE, SENDER)
        g = GuardedDrafter(FixedDrafter(good_draft()), max_body_chars=50)
        self.raises("body_too_long", g.draft, person(), EVIDENCE, SENDER)

    def test_grounding_checks(self):
        cases = [({"grounded_on": ()}, "ungrounded"),
                 ({"grounded_on": ("https://code.example.com/other",)}, "ungrounded"),
                 ({"grounded_on": [URL]}, "bad_draft"),
                 ({"body": good_body().replace(URL, "the link")}, "evidence_url_missing"),
                 ({"body": good_body().replace(SUMMARY, "Something we made up.")},
                  "evidence_summary_missing"),
                 ({"subject": "A great opportunity"}, "subject_ungrounded"),
                 ({"subject": f"{DOMAIN}\nBcc: x"}, "subject_malformed"),
                 ({"subject": ""}, "subject_malformed"),
                 ({"body": good_body("\x1b[31m")}, "body_malformed")]
        for kw, code in cases:
            with self.subTest(code=code):
                self.raises(code, self.guarded(**kw).draft, person(), EVIDENCE, SENDER)
        self.raises("bad_draft", GuardedDrafter(FixedDrafter("text")).draft, person(), EVIDENCE,
                    SENDER)

    def test_subject_may_use_the_evidence_kind(self):
        self.guarded(subject="A public repo signal").draft(person(), EVIDENCE, SENDER)

    def test_inner_drafter_not_called_when_refused(self):
        for p, ev, s, code in ((person(), (), SENDER, "no_evidence"),
                               (person(name="?"), EVIDENCE, SENDER, "no_name"),
                               (person(), EVIDENCE, Sender(), "sender_missing")):
            with self.subTest(code=code):
                g = self.guarded()
                self.raises(code, g.draft, p, ev, s)
                self.assertEqual(self.inner.calls, 0)

    def test_llm_extension_point_is_interface_only(self):
        self.assertIsInstance(FixedDrafter(good_draft()), LlmDrafter)
        self.assertIsInstance(TemplateDrafter(), Drafter)
        src = (ROOT / "src/repo_signals/drafting/llm.py").read_text()
        tree = ast.parse(src)
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        self.assertEqual(classes, ["LlmDrafter"])
        for word in ("api_key", "API_KEY", "https://", "environ", "getenv"):
            self.assertNotIn(word, src)


# ---- suppression ----

class SuppressionList(Raises, unittest.TestCase):
    def test_by_email_and_by_domain(self):
        s = parse_suppression("# synthetic list\n\n" + addr("BLAKE") + "\nblocked.example.com\n")
        self.assertTrue(s.matches(person()))
        self.assertFalse(s.matches(person(email=addr("casey"))))
        self.assertTrue(s.matches(person(email=addr("x", "blocked.example.com"))))
        self.assertTrue(s.matches(person(email=addr("x", "eu.blocked.example.com"))))
        self.assertFalse(s.matches(person(email=addr("x", "notblocked.example.com"))))
        self.assertTrue(s.matches(person(email=None, domain="blocked.example.com")))
        self.assertTrue(s.matches(person(email=addr("x", "other.example.com"),
                                         domain="blocked.example.com")))
        self.assertNotIn("blake", repr(s))

    def test_strict_parsing(self):
        for line in ("not an entry", "two@at" + "@example.com", "*.example.com",
                     "example", "http://example.com", "a b" + "@example.com",
                     "example.com; drop", "-bad.example.com"):
            with self.subTest(line=line):
                e = self.raises("suppression_malformed_line_2", parse_suppression,
                                "example.com\n" + line + "\n")
                self.assertNotIn(line, str(e))

    def test_files(self):
        with tempfile.TemporaryDirectory(prefix="SENTINELPATH-") as tmp:
            path = os.path.join(tmp, "s.txt")
            self.raises("suppression_unreadable", load_suppression, path)
            for raw, code in ((b"x" * (MAX_SUPPRESSION_BYTES + 1), "suppression_too_large"),
                              (b"\xff\xfe", "suppression_malformed"),
                              (b"a.example.com\rb.example.com", "suppression_malformed")):
                with self.subTest(code=code):
                    with open(path, "wb") as fh:
                        fh.write(raw)
                    e = self.raises(code, load_suppression, path)
                    self.assertNotIn("SENTINELPATH", everything(e))
            with open(path, "wb") as fh:
                fh.write(b"a.example.com\r\nb.example.com\r\n")
            self.assertEqual(load_suppression(path).domains,
                             frozenset({"a.example.com", "b.example.com"}))
            self.assertEqual(suppression_from_env({config.SUPPRESSION_FILE: path}).domains,
                             frozenset({"a.example.com", "b.example.com"}))
        self.assertIsNone(suppression_from_env({}))
        e = self.raises("suppression_path_malformed", suppression_from_env,
                        {config.SUPPRESSION_FILE: " SENTINELPATH"})
        self.assertEqual(e.var, config.SUPPRESSION_FILE)

    def test_suppressed_person_gets_no_draft(self):
        drafter = GuardedDrafter(TemplateDrafter())
        for listed in (addr("blake"), DOMAIN, "example.invalid"):
            with self.subTest(listed=listed):
                r = draft_for(person(), EVIDENCE, drafter, SENDER, parse_suppression(listed))
                self.assertEqual(r, DraftResult("suppressed", None, "suppressed"))
        r = draft_for(person(), EVIDENCE, drafter, SENDER, parse_suppression("other.example.com"))
        self.assertEqual(r.status, "drafted")
        r = draft_for(person(), (), drafter, SENDER, None)
        self.assertEqual((r.status, r.code), ("refused", "no_evidence"))
        self.raises("sender_missing", draft_for, person(), EVIDENCE, drafter, Sender())


# ---- fill_drafts ----

def row(name="Blake Birch", email="default", domain=DOMAIN, draft="", reviewed="", extra=()):
    em = addr("blake") if email == "default" else email
    return [name, em, "CTO", domain, "fake", "0.8", "evidence", draft, reviewed, "run-1",
            "2026-10-21T12:00:00Z", *extra]


class FillDrafts(Raises, unittest.TestCase):
    def fill(self, rows, live=True, lookup=None, suppression=None, sender=SENDER, drafter=None):
        self.out = io.StringIO()
        return fill_drafts(rows, drafter or TemplateDrafter(),
                           lookup if lookup is not None else {DOMAIN: EVIDENCE},
                           sender=sender, suppression=suppression,
                           settings=Settings(dry_run=not live), environ={}, out=self.out)

    def test_fills_only_eligible_rows(self):
        rows = [row(), row(name="Casey Cedar", email=addr("casey"), domain="none.example.invalid"),
                row(name="?"), row(email=addr("devon"), extra=("my notes",)),
                ["", "", "", "", ""]]
        before = [list(r) for r in rows]
        out = self.fill(rows)
        self.assertEqual(rows, before)  # the input is never mutated
        self.assertTrue(out[0][DRAFT_COL].startswith(f"Subject: A public signal from {DOMAIN}\n\n"))
        self.assertIn(URL, out[0][DRAFT_COL])
        self.assertEqual(out[1], rows[1])
        self.assertEqual(out[2], rows[2])
        self.assertNotEqual(out[3][DRAFT_COL], "")
        self.assertEqual(out[3][len(COLUMNS)], "my notes")
        self.assertEqual(out[4], rows[4])
        self.assertIn("filled 2 draft(s) (0 suppressed, 2 refused", self.out.getvalue())
        self.assertIn("1 unusable row(s)", self.out.getvalue())

    def test_never_overwrites_a_non_empty_draft(self):
        for existing in ("Hand-written draft, do not touch.", " ", "x"):
            with self.subTest(existing=existing):
                rows = [row(draft=existing)]
                self.assertEqual(self.fill(rows)[0], rows[0])
                self.assertIn("1 already drafted", self.out.getvalue())

    def test_never_touches_reviewed(self):
        rows = [row(reviewed="TRUE"), row(email=addr("e"), reviewed=""),
                row(email=addr("f"))[:DRAFT_COL], row(email=addr("g"))[:DRAFT_COL + 1]]
        out = self.fill(rows)
        self.assertEqual(out[0][REVIEWED_COL], "TRUE")
        self.assertEqual(out[1][REVIEWED_COL], "")
        self.assertEqual(len(out[2]), DRAFT_COL + 1)  # no reviewed cell is created
        self.assertEqual(len(out[3]), DRAFT_COL + 1)
        for before, after in zip(rows, out):
            self.assertEqual(before[:DRAFT_COL], after[:DRAFT_COL])
            self.assertEqual(before[DRAFT_COL + 1:], after[DRAFT_COL + 1:])

    def test_suppressed_rows_get_no_draft(self):
        rows = [row(), row(name="Casey Cedar", email=addr("casey", "blocked.example.com"))]
        lookup = {DOMAIN: EVIDENCE}
        out = self.fill(rows, lookup=lookup, suppression=parse_suppression(
            addr("blake") + "\nblocked.example.com"))
        self.assertEqual(out, rows)
        self.assertIn("2 suppressed", self.out.getvalue())

    def test_suppression_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.txt")
            with open(path, "w") as fh:
                fh.write(DOMAIN + "\n")
            out = io.StringIO()
            res = fill_drafts([row()], TemplateDrafter(), {DOMAIN: EVIDENCE}, sender=SENDER,
                              settings=Settings(dry_run=False),
                              environ={config.SUPPRESSION_FILE: path}, out=out)
        self.assertEqual(res, [row()])
        self.assertIn("1 suppressed", out.getvalue())

    def test_dry_run_writes_nothing_and_masks_email(self):
        rows = [row(), row(email=addr("casey"))]
        out = self.fill(rows, live=False)
        self.assertEqual(out, rows)
        text = self.out.getvalue()
        self.assertIn("drafting: dry run, would fill 2 draft(s)", text)
        self.assertIn("nothing written", text)
        self.assertIn("To: b***@" + DOMAIN, text)
        self.assertEqual(text.count("--- sample draft ---"), 1)
        self.assertNotIn(addr("blake"), text)
        self.assertNotIn(addr("casey"), text)
        self.assertIn(URL, text)

    def test_dry_run_is_the_default(self):
        out = io.StringIO()
        res = fill_drafts([row()], TemplateDrafter(), {DOMAIN: EVIDENCE}, sender=SENDER,
                          environ={}, out=out)
        self.assertEqual(res, [row()])
        self.assertIn("dry run", out.getvalue())

    def test_dry_run_without_drafts_shows_no_sample(self):
        self.fill([row(domain="none.example.invalid")], live=False)
        self.assertNotIn("sample", self.out.getvalue())

    def test_lookup_forms_and_guard_wrapping(self):
        def lookup(domain):
            return list(EVIDENCE) if domain == DOMAIN else None

        self.assertNotEqual(self.fill([row()], lookup=lookup)[0][DRAFT_COL], "")
        with self.assertRaises(TypeError):
            self.fill([row()], lookup=42)
        bad = FixedDrafter(good_draft(body=good_body("https://evil.example.com")))
        self.assertEqual(self.fill([row()], drafter=bad), [row()])
        self.assertIn("1 refused", self.out.getvalue())

    def test_sender_missing_refuses_everything(self):
        e = self.raises("sender_missing", self.fill, [row()], sender=Sender(
            SENDER.name, SENDER.role, SENDER.org_name, SENDER.postal_address, None))
        self.assertEqual(e.var, config.OPTOUT_TEXT)
        e = self.raises("sender_missing", fill_drafts, [row()], TemplateDrafter(),
                        {DOMAIN: EVIDENCE}, environ={}, out=io.StringIO())
        self.assertEqual(e.var, config.SENDER_NAME)

    def test_formula_quoted_sheet_values_are_read_back(self):
        out = self.fill([row(name="'=Blake Birch")])
        self.assertIn("Hi =Blake,", out[0][DRAFT_COL])
        self.assertFalse(out[0][DRAFT_COL].startswith(("=", "+", "-", "@")))

    def test_bad_rows(self):
        with self.assertRaises(TypeError):
            self.fill(["not a row"])


# ---- no network, no sending ----

FORBIDDEN_MODULES = {"smtplib", "socket", "ssl", "urllib", "http", "subprocess", "asyncio",
                     "ftplib", "poplib", "imaplib", "telnetlib", "requests", "email"}
FORBIDDEN_RELATIVE = ("http", "providers", "sheets.client", "sheets.auth", "sheets.writer",
                      "sheets.google_api")


class NoNetwork(unittest.TestCase):
    def test_drafting_imports_nothing_that_talks_to_a_network(self):
        files = sorted((ROOT / "src/repo_signals/drafting").glob("*.py"))
        self.assertEqual(len(files), 7)
        for f in files:
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                with self.subTest(file=f.name, line=getattr(node, "lineno", 0)):
                    if isinstance(node, ast.Import):
                        for a in node.names:
                            self.assertNotIn(a.name.split(".")[0], FORBIDDEN_MODULES)
                    elif isinstance(node, ast.ImportFrom):
                        mod = node.module or ""
                        if node.level == 0:
                            self.assertNotIn(mod.split(".")[0], FORBIDDEN_MODULES)
                        else:
                            self.assertFalse(mod.startswith(FORBIDDEN_RELATIVE), mod)
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, ("exec", "eval", "__import__"))

    def test_importing_drafting_loads_no_network_module(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "import repo_signals.drafting.base, repo_signals.drafting.guard,"
                " repo_signals.drafting.template, repo_signals.drafting.suppression,"
                " repo_signals.drafting.llm, repo_signals.drafting.sheet;"
                "bad = [m for m in ('repo_signals.http', 'smtplib', 'socket', 'ssl',"
                " 'http.client', 'urllib.request', 'subprocess') if m in sys.modules];"
                "print(bad)")
        r = subprocess.run([sys.executable, "-c", code, str(_src.SRC)],
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "[]", r.stderr)


class Source(unittest.TestCase):
    def test_templates_are_packaged(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('"repo_signals.drafting" = ["templates/*.txt"]', text)


if __name__ == "__main__":
    unittest.main()
