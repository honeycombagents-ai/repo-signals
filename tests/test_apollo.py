"""ApolloProvider tests. Offline and synthetic only: every response here is made up and
follows the ASSUMED Apollo response shape in providers/apollo.py (unverified)."""
import io
import json
import os
import re
import subprocess
import sys
import traceback
import unittest
from pathlib import Path
from unittest import mock

import _src  # noqa: F401
from repo_signals import config, doctor
from repo_signals.budget import CallBudget
from repo_signals.config import Settings
from repo_signals.http import HttpClient, RawResponse
from repo_signals.models import Person
from repo_signals.providers import (PLANNED, PeopleProvider, ProviderError, Registry,
                                    find_people_within, get, list_providers)
from repo_signals.providers import apollo
from repo_signals.providers.apollo import (API_KEY_ENV, DRY_RUN_NOTICE, REVEAL_NOTE,
                                           ApolloProvider)
from repo_signals.providers.fake import FakeProvider

ROOT = Path(__file__).resolve().parent.parent
KEY = "SENTINELAPOLLOKEY0123456789"
DOMAIN = "acme.example.invalid"
LOCKED = "email_not_unlocked" + "@" + "example.invalid"


def addr(local):
    return local + "@" + "example.invalid"


OMIT = object()


def person(name, title, email="default", status="verified", **extra):
    local = name.lower().replace(" ", ".") if isinstance(name, str) and name else "anon"
    row = {"id": "synthetic-" + local, "name": name, "title": title,
           "email": addr(local) if email == "default" else email,
           "email_status": status, **extra}
    return {k: v for k, v in row.items() if v is not OMIT}

ROWS = [person("Avery Alder", "CTO"), person("Blake Birch", "VP Engineering", status="guessed"),
        person("Casey Cedar", "Co-founder & cto", email=LOCKED, status="locked"),
        person("Devon Hazel", "Designer"), person("Emery Maple", "Head of Engineering")]


def body(rows=ROWS, **extra):
    return {"people": list(rows), "pagination": {"page": 1, "per_page": 25}, **extra}


def raw(data=None, status=200, headers=None, text=None):
    payload = text if text is not None else json.dumps(body() if data is None else data).encode()
    return RawResponse(status, headers or {}, payload)


class FakeTransport:
    def __init__(self, *responses, on_call=None):
        self.responses = list(responses)
        self.calls = []
        self.on_call = on_call

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout,
                           "method": method, "body": body})
        if self.on_call:
            self.on_call()
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r


def live(*responses, budget=None, environ=None, on_call=None):
    t = FakeTransport(*(responses or (raw(),)), on_call=on_call)
    b = budget if budget is not None else CallBudget(10)
    p = ApolloProvider(b, dry_run=False, http=HttpClient((apollo.API_HOST,), transport=t),
                       environ={API_KEY_ENV: KEY} if environ is None else environ)
    return p, t, b


def everything(e):
    return "".join(traceback.format_exception(e)) + repr(e) + str(e.args)


def run_find(provider, *args):
    try:
        return provider.find_people(*args)
    except ProviderError:
        return None


class Request(unittest.TestCase):
    def test_one_search_with_key_in_header_only(self):
        p, t, _ = live()
        p.find_people(DOMAIN, ("CTO", " VP Engineering "), 5)
        self.assertEqual(len(t.calls), 1)
        call = t.calls[0]
        self.assertEqual((call["url"], call["method"]), (apollo.PEOPLE_SEARCH_URL, "POST"))
        self.assertEqual(json.loads(call["body"]), {
            "q_organization_domains_list": [DOMAIN], "person_titles": ["CTO", "VP Engineering"],
            "per_page": 5, "page": 1})
        self.assertEqual(call["headers"][apollo.API_KEY_HEADER], KEY)
        self.assertEqual([k for k, v in call["headers"].items() if KEY in v],
                         [apollo.API_KEY_HEADER])
        self.assertNotIn(KEY, call["url"])
        self.assertNotIn(KEY.encode(), call["body"])
        self.assertNotIn(b"SENTINEL", call["body"])
        self.assertEqual(call["timeout"], apollo.REQUEST_TIMEOUT_SECONDS)

    def test_no_titles_sent_when_none_given(self):
        p, t, _ = live()
        p.find_people(DOMAIN, (), 3)
        self.assertNotIn("person_titles", json.loads(t.calls[0]["body"]))

    def test_per_page_capped(self):
        p, t, _ = live()
        p.find_people(DOMAIN, (), 500)
        self.assertEqual(json.loads(t.calls[0]["body"])["per_page"], apollo.MAX_PER_PAGE)

    def test_input_validation_before_any_call(self):
        p, t, b = live()
        for args in (("", (), 1), ("not a domain", (), 1), ("https://acme.example.com", (), 1),
                     (DOMAIN, ["CTO"], 1), (DOMAIN, (" ",), 1), (DOMAIN, (), 0),
                     (DOMAIN, (), True), (None, (), 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                p.find_people(*args)
        self.assertEqual((t.calls, b.used), ([], 0))

    def test_missing_or_bad_key(self):
        for env, code in (({}, "missing_key"), ({API_KEY_ENV: ""}, "missing_key"),
                          ({API_KEY_ENV: "  "}, "missing_key"),
                          ({API_KEY_ENV: KEY + "\nX: y"}, "bad_key"),
                          ({API_KEY_ENV: KEY + " x"}, "bad_key")):
            with self.subTest(code=code, n=len(env.get(API_KEY_ENV, ""))):
                p, t, b = live(environ=env)
                with self.assertRaises(ProviderError) as cm:
                    p.find_people(DOMAIN, (), 1)
                self.assertEqual(cm.exception.code, code)
                self.assertNotIn("SENTINEL", everything(cm.exception))
                self.assertEqual((t.calls, b.used), ([], 0))

    def test_reads_os_environ_by_default(self):
        t = FakeTransport(raw())
        p = ApolloProvider(CallBudget(2), dry_run=False,
                           http=HttpClient((apollo.API_HOST,), transport=t))
        with mock.patch.dict(os.environ, {API_KEY_ENV: KEY}):
            p.find_people(DOMAIN, (), 1)
        self.assertEqual(t.calls[0]["headers"][apollo.API_KEY_HEADER], KEY)


class SearchOnly(unittest.TestCase):
    """No code path calls a second endpoint: exactly one request per find_people."""

    RESPONSES = [raw(), raw(body([person(f"P{i} Q", "CTO", email=LOCKED, status="locked")
                                  for i in range(5)])),
                 raw(body([person("A B", "CTO", email=None, status="unavailable")])),
                 raw(body([])), raw(status=402), raw(status=429), raw(status=500),
                 raw(text=b"{bad")]

    def test_exactly_one_request_per_lookup(self):
        for i, response in enumerate(self.RESPONSES):
            for titles, limit in (((), 1), ((), 5), (("cto",), 3)):
                with self.subTest(i=i, titles=titles, limit=limit):
                    p, t, b = live(response)
                    run_find(p, DOMAIN, titles, limit)
                    self.assertEqual(len(t.calls), 1)
                    self.assertEqual(t.calls[0]["url"], apollo.PEOPLE_SEARCH_URL)
                    self.assertEqual(b.used, 1)

    def test_many_lookups_one_request_each(self):
        p, t, _ = live(budget=CallBudget(20))
        for n in range(1, 6):
            p.find_people(DOMAIN, (), 5)
            self.assertEqual(len(t.calls), n)
        self.assertEqual({c["url"] for c in t.calls}, {apollo.PEOPLE_SEARCH_URL})

    def test_module_names_one_endpoint(self):
        urls = {v for v in vars(apollo).values() if isinstance(v, str) and "://" in v}
        self.assertEqual(urls, {apollo.PEOPLE_SEARCH_URL})

    def test_reveal_is_documented_as_not_implemented(self):
        src = Path(apollo.__file__).read_text()
        self.assertIn("Email reveal is intentionally NOT implemented", src)
        self.assertIn("explicit opt-in setting plus its own separate budget", src)


class Parsing(unittest.TestCase):
    def test_maps_people(self):
        p, _, _ = live()
        people = p.find_people(DOMAIN, (), 10)
        self.assertEqual(len(people), 5)
        self.assertTrue(all(isinstance(x, Person) and x.source == "apollo"
                            and x.company_domain == DOMAIN for x in people))
        self.assertEqual((people[0].name, people[0].email, people[0].title, people[0].confidence),
                         ("Avery Alder", addr("avery.alder"), "CTO", 0.9))
        self.assertEqual((people[1].email, people[1].confidence), (addr("blake.birch"), 0.5))
        self.assertEqual((people[2].name, people[2].email, people[2].confidence),
                         ("Casey Cedar", None, 0.0))

    def test_missing_empty_and_locked_emails_become_none(self):
        cases = [OMIT, None, "", "   ", LOCKED, LOCKED.upper(), " " + LOCKED + " ",
                 "Email_Not_Unlocked" + "@" + "example.com", "no-at-sign", 42, ["x"],
                 "two" + "@" + "at" + "@" + "example.invalid", "sp ace" + "@" + "example.invalid"]
        rows = [person(f"Avery Alder{i}", "CTO", email=e) for i, e in enumerate(cases)]
        for status in apollo.NO_ADDRESS_EMAIL_STATUSES:
            rows.append(person("Blake " + status, "CTO", email=addr("real"), status=status))
        p, _, _ = live(raw(body(rows)))
        people = p.find_people(DOMAIN, (), 50)
        self.assertEqual(len(people), len(rows))
        for x in people:
            with self.subTest(name=x.name):
                self.assertIsNone(x.email)
                self.assertEqual(x.confidence, 0.0)
                self.assertEqual(x.title, "CTO")

    def test_confidence_from_email_status(self):
        cases = [("verified", 0.9), ("VERIFIED", 0.9), ("likely_to_engage", 0.7),
                 ("guessed", 0.5), ("extrapolated", 0.5), ("mystery", 0.0), (None, 0.0),
                 (OMIT, 0.0), (["verified"], 0.0), (7, 0.0)]
        rows = [person(f"Avery Alder{i}", "CTO", email=addr(f"a{i}"), status=s)
                for i, (s, _) in enumerate(cases)]
        p, _, _ = live(raw(body(rows)))
        got = [(x.email is not None, x.confidence) for x in p.find_people(DOMAIN, (), 50)]
        self.assertEqual(got, [(True, c) for _, c in cases])

    def test_name_from_parts_and_unusable_rows_skipped(self):
        rows = [person(OMIT, "CTO", first_name="Avery", last_name="Alder"),
                person("", "CTO", first_name="Blake", last_name=None),
                person(None, "CTO"), person("No Title", ""), person("No Title2", None),
                person("Kept Person", "CTO")]
        p, _, _ = live(raw(body(rows)))
        self.assertEqual([x.name for x in p.find_people(DOMAIN, (), 50)],
                         ["Avery Alder", "Blake", "Kept Person"])

    def test_apollo_returns_all_rows_titles_are_filtered_by_apollo(self):
        p, _, _ = live()
        every = ["CTO", "VP Engineering", "Co-founder & cto", "Designer", "Head of Engineering"]
        self.assertEqual([x.title for x in p.find_people(DOMAIN, ("cto",), 10)], every)
        self.assertEqual([x.title for x in p.find_people(DOMAIN, ("VP ENGINEERING",), 10)],
                         every)
        self.assertEqual([x.title for x in p.find_people(DOMAIN, ("engineering", "DESIGNER"), 10)],
                         every)
        self.assertEqual([x.title for x in p.find_people(DOMAIN, ("Chief Llama",), 10)], every)

    def test_limit_caps_results(self):
        for limit, n in ((1, 1), (2, 2), (3, 3), (5, 5), (50, 5)):
            with self.subTest(limit=limit):
                p, _, _ = live()
                self.assertEqual(len(p.find_people(DOMAIN, (), limit)), n)
        p, _, _ = live()
        self.assertEqual([x.name for x in p.find_people(DOMAIN, ("cto",), 1)], ["Avery Alder"])

    def test_unexpected_shapes(self):
        for data in ([], {}, {"people": {}}, {"people": "x"}, {"people": [1]},
                     {"people": [None]}, {"contacts": []}, "text", 7, None):
            with self.subTest(data=data):
                p, _, _ = live(raw(data, text=json.dumps(data).encode()))
                with self.assertRaises(ProviderError) as cm:
                    p.find_people(DOMAIN, (), 5)
                self.assertEqual(cm.exception.code, "unexpected_shape")

    def test_malformed_json(self):
        for text in (b"", b"{not json", b"<html>SENTINELBODY</html>", b"\xff"):
            with self.subTest(text=text):
                p, _, _ = live(raw(text=text))
                with self.assertRaises(ProviderError) as cm:
                    p.find_people(DOMAIN, (), 5)
                self.assertEqual(cm.exception.code, "malformed_json")
                self.assertNotIn("SENTINEL", everything(cm.exception))


class Statuses(unittest.TestCase):
    CASES = [(401, {}, "auth_failed"), (403, {}, "auth_failed"), (402, {}, "plan_exhausted"),
             (429, {"Retry-After": "7"}, "retry_after_7"),
             (429, {"Retry-After": "0"}, "retry_after_0"),
             (429, {"Retry-After": "60"}, "retry_after_60"),
             (429, {"Retry-After": " 12 "}, "retry_after_12"),
             (429, {"Retry-After": "61"}, "rate_limited"),
             (429, {"Retry-After": "86400"}, "rate_limited"),
             (429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, "rate_limited"),
             (429, {"Retry-After": "-3"}, "rate_limited"), (429, {}, "rate_limited"),
             (400, {}, "bad_request"), (422, {}, "bad_request"), (500, {}, "server_error"),
             (502, {}, "server_error"), (503, {"Retry-After": "5"}, "server_error"),
             (599, {}, "server_error"), (404, {}, "unexpected_status")]

    def run_status(self, status, headers, data=None):
        # A VALID success body, so a status wrongly treated as success would return people.
        p, t, b = live(raw(data if data is not None else body(), status=status, headers=headers))
        with self.assertRaises(ProviderError) as cm:
            p.find_people(DOMAIN, (), 5)
        self.assertEqual((len(t.calls), b.used), (1, 1))
        text = everything(cm.exception)
        for leak in (KEY, "SENTINEL", "example.invalid", apollo.API_HOST):
            self.assertNotIn(leak, text)
        return cm.exception.code

    def test_status_mapping(self):
        for status, headers, code in self.CASES:
            with self.subTest(status=status, headers=headers):
                self.assertEqual(self.run_status(status, headers), code)

    def test_204_is_unexpected_shape(self):
        p, _, _ = live(raw(status=204, text=b"{}"))
        with self.assertRaises(ProviderError) as cm:
            p.find_people(DOMAIN, (), 5)
        self.assertEqual(cm.exception.code, "unexpected_shape")

    def test_non_json_error_bodies_still_map(self):
        for status, code in ((401, "auth_failed"), (429, "rate_limited"), (500, "server_error"),
                             (402, "plan_exhausted")):
            with self.subTest(status=status):
                p, _, _ = live(raw(status=status, text=b"<html>SENTINELBODY</html>"))
                with self.assertRaises(ProviderError) as cm:
                    p.find_people(DOMAIN, (), 5)
                self.assertEqual(cm.exception.code, code)

    def test_plan_exhausted_equivalent(self):
        for status in (403, 422, 429, 400):
            for err in apollo.PLAN_EXHAUSTED_ERROR_CODES:
                with self.subTest(status=status, err=err):
                    data = {"error": "SENTINELBODY", "error_code": err}
                    self.assertEqual(self.run_status(status, {}, data), "plan_exhausted")
        data = {"error_code": apollo.PLAN_EXHAUSTED_ERROR_CODES[0]}
        self.assertEqual(self.run_status(401, {}, data), "auth_failed")
        self.assertEqual(self.run_status(422, {}, {"error_code": ["x"]}), "bad_request")

    def test_rate_limit_never_sleeps_or_retries(self):
        with mock.patch("time.sleep", side_effect=AssertionError("slept")):
            self.assertEqual(self.run_status(429, {"Retry-After": "30"}), "retry_after_30")

    def test_transport_timeout(self):
        p, t, b = live(ProviderError("timeout"))
        with self.assertRaises(ProviderError) as cm:
            p.find_people(DOMAIN, (), 5)
        self.assertEqual((cm.exception.code, len(t.calls), b.used), ("timeout", 1, 1))


class Budget(unittest.TestCase):
    def test_consumed_before_the_call(self):
        seen = []
        budget = CallBudget(3)

        def record():
            seen.append(budget.used)

        p, _, b = live(budget=budget, on_call=record)
        p.find_people(DOMAIN, (), 1)
        p.find_people(DOMAIN, (), 1)
        self.assertEqual((seen, b.used), ([1, 2], 2))

    def test_consumed_even_when_the_call_fails(self):
        p, _, b = live(raw(status=500))
        with self.assertRaises(ProviderError):
            p.find_people(DOMAIN, (), 1)
        self.assertEqual(b.used, 1)

    def test_over_the_cap_never_reaches_transport(self):
        p, t, b = live(budget=CallBudget(2))
        p.find_people(DOMAIN, (), 1)
        p.find_people(DOMAIN, (), 1)
        for _ in range(2):
            with self.assertRaises(ProviderError) as cm:
                p.find_people(DOMAIN, (), 1)
            self.assertEqual(cm.exception.code, "budget_exhausted")
            self.assertIsNone(cm.exception.__cause__)
            self.assertTrue(cm.exception.__suppress_context__)
        self.assertEqual((len(t.calls), b.used), (2, 2))

    def test_exhausted_budget_makes_zero_calls(self):
        b = CallBudget(1)
        b.consume(1)
        p, t, _ = live(budget=b)
        with self.assertRaises(ProviderError) as cm:
            p.find_people(DOMAIN, (), 1)
        self.assertEqual((cm.exception.code, t.calls), ("budget_exhausted", []))

    def test_live_needs_a_budget_it_does_not_create(self):
        with self.assertRaises(ValueError):
            ApolloProvider(dry_run=False)
        with self.assertRaises(TypeError):
            ApolloProvider(25, dry_run=False)
        with self.assertRaises(TypeError):
            ApolloProvider(CallBudget(1), dry_run=0)
        b = CallBudget(4)
        self.assertIs(ApolloProvider(b, dry_run=False)._budget, b)

    def test_through_find_people_within(self):
        p, t, b = live(budget=CallBudget(4))
        self.assertEqual(len(find_people_within(b, p, DOMAIN, ("cto",), 1)), 1)
        self.assertEqual((len(t.calls), b.used), (1, 2))  # one guard charge, one network charge


class DryRun(unittest.TestCase):
    def test_default_is_dry_run_and_makes_zero_calls(self):
        t = FakeTransport(raw())
        out = io.StringIO()
        b = CallBudget(5)
        p = ApolloProvider(b, http=HttpClient((apollo.API_HOST,), transport=t),
                           environ={API_KEY_ENV: KEY}, out=out)
        self.assertTrue(p.dry_run)
        people = p.find_people(DOMAIN, ("CTO",), 2)
        self.assertEqual((t.calls, b.used), ([], 0))
        self.assertEqual(people, FakeProvider().find_people(DOMAIN, ("CTO",), 2))
        self.assertEqual(out.getvalue(), DRY_RUN_NOTICE + "\n")
        self.assertNotIn(KEY, out.getvalue())

    def test_bare_default_builds_no_client(self):
        p = ApolloProvider(out=io.StringIO())
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=AssertionError("network")):
            p.find_people(DOMAIN, (), 1)
        self.assertIsNone(p._http)

    def test_notice_goes_to_stderr_by_default(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            ApolloProvider().find_people(DOMAIN, (), 1)
        self.assertEqual(err.getvalue(), DRY_RUN_NOTICE + "\n")


class RegistryAndDoctor(unittest.TestCase):
    def test_registered_and_not_planned(self):
        self.assertIn("apollo", list_providers())
        p = get("apollo")
        self.assertIsInstance(p, ApolloProvider)
        self.assertIsInstance(p, PeopleProvider)
        self.assertTrue(p.dry_run)
        self.assertIsNone(p._budget)
        self.assertEqual((p.name, p.required_env), ("apollo", ("APOLLO_API_KEY",)))
        self.assertNotIn("apollo", PLANNED)

    def test_docs_no_longer_list_apollo_as_planned(self):
        for doc in ("README.md", "docs/adapters.md"):
            with self.subTest(doc=doc):
                text = (ROOT / doc).read_text()
                self.assertIsNone(re.search(r"(?i)planned[^.]*apollo|apollo[^.]*planned", text))

    def run_doctor(self, env, **kw):
        reg = Registry()
        reg.register(FakeProvider())
        reg.register(ApolloProvider())
        out = io.StringIO()
        code = doctor.run(environ={config.PROVIDER: "apollo", **env}, registry=reg, out=out, **kw)
        return code, out.getvalue()

    def test_key_set(self):
        code, out = self.run_doctor({API_KEY_ENV: KEY})
        self.assertEqual(code, 0, out)
        self.assertIn("PASS APOLLO_API_KEY is set", out)
        self.assertIn("WARN apollo: live use is not enabled until dry run is turned off", out)
        self.assertEqual([l for l in out.splitlines() if "reveal" in l], ["PASS " + REVEAL_NOTE])
        self.assertNotIn(KEY, out)
        self.assertNotIn("SENTINEL", out)

    def test_key_missing_fails(self):
        for env in ({}, {API_KEY_ENV: ""}, {API_KEY_ENV: "  "}):
            with self.subTest(env=env):
                code, out = self.run_doctor(env)
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL APOLLO_API_KEY is missing", out)

    def test_no_live_warning_when_dry_run_off(self):
        _, out = self.run_doctor({API_KEY_ENV: KEY}, settings=Settings(dry_run=False))
        self.assertNotIn("live use is not enabled", out)
        self.assertIn(REVEAL_NOTE, out)

    def test_doctor_stays_offline(self):
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=AssertionError("network")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket")):
            code, _ = self.run_doctor({API_KEY_ENV: KEY})
        self.assertEqual(code, 0)

    def test_cli_doctor_prints_no_value(self):
        r = subprocess.run(
            [sys.executable, "-m", "repo_signals", "doctor", "--online"],
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_src.SRC),
                 "PYTHONDONTWRITEBYTECODE": "1", config.PROVIDER: "apollo", API_KEY_ENV: KEY},
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("PASS APOLLO_API_KEY is set", r.stdout)
        self.assertIn(REVEAL_NOTE, r.stdout)
        self.assertNotIn("SENTINEL", r.stdout + r.stderr)

    def test_repr_hides_internals(self):
        p, _, _ = live()
        self.assertNotIn(KEY, repr(p))


class Source(unittest.TestCase):
    def test_unverified_note_on_the_constants_block(self):
        text = Path(apollo.__file__).read_text()
        self.assertIn("verify against Apollo's current API documentation before first live use",
                      text)
        head = text.split("# ---", 2)[1]
        for const in ("PEOPLE_SEARCH_URL", "API_KEY_HEADER", "BODY_DOMAINS", "BODY_TITLES",
                      "BODY_PER_PAGE", "FIELD_EMAIL", "FIELD_EMAIL_STATUS",
                      "LOCKED_EMAIL_LOCAL_PARTS", "PLAN_EXHAUSTED_ERROR_CODES"):
            self.assertIn(const + " =", head)

    def test_people_search_url_is_the_api_search_path(self):
        self.assertTrue(apollo.PEOPLE_SEARCH_URL.startswith("https://" + apollo.API_HOST + "/"))
        self.assertTrue(apollo.PEOPLE_SEARCH_URL.endswith("/mixed_people/api_search"))

    def test_docs_state_search_returns_no_email(self):
        note = ("Apollo's people search returns first names and titles, and no email addresses. "
                "In search-only mode Apollo rows therefore normally have no email. Emails need a "
                "separate, credit-spending enrichment call, which is not implemented.")
        for name in ("docs/apollo.md", "README.md"):
            with self.subTest(file=name):
                text = " ".join((ROOT / name).read_text().split())
                self.assertIn(note, text)

    def test_audience_and_email_opt_in_documented(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("\n## Who this is for\n", readme)
        doc = (ROOT / "docs/apollo.md").read_text()
        heading = "\n## Emails need an opt-in and their own budget\n"
        self.assertIn(heading, doc)
        section = doc.split(heading, 1)[1].split("\n## ", 1)[0]
        self.assertIn("own call budget", " ".join(section.split()))


if __name__ == "__main__":
    unittest.main()
