"""HunterProvider tests. Offline and synthetic only: every response here is made up and
follows the ASSUMED Hunter response shape in providers/hunter.py (unverified)."""
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
from urllib.parse import parse_qs, urlsplit

import _src  # noqa: F401
from repo_signals import config, doctor
from repo_signals.budget import CallBudget
from repo_signals.config import Settings
from repo_signals.http import HttpClient, RawResponse
from repo_signals.models import Person
from repo_signals.providers import (PLANNED, PeopleProvider, ProviderError, Registry,
                                    find_people_within, get, list_providers)
from repo_signals.providers import hunter
from repo_signals.providers.fake import FakeProvider
from repo_signals.providers.hunter import API_KEY_ENV, DRY_RUN_NOTICE, HunterProvider

KEY = "SENTINELHUNTERKEY0123456789"
DOMAIN = "acme.example.invalid"


def addr(local):
    return local + "@" + "example.invalid"


def entry(first, last, position, score=90, email=None):
    return {"value": email if email is not None else addr(f"{first}.{last}".lower()),
            "type": "personal", "confidence": score, "first_name": first, "last_name": last,
            "position": position}


ROWS = [entry("Avery", "Alder", "CTO"), entry("Blake", "Birch", "VP Engineering", 80),
        entry("Casey", "Cedar", "Co-founder & cto", 70), entry("Devon", "Hazel", "Designer", 60),
        entry("Emery", "Maple", "Head of Engineering", 50)]


def body(rows=ROWS, meta=None, **extra):
    return {"data": {"domain": DOMAIN, "emails": list(rows)},
            "meta": meta if meta is not None else {"results": len(rows)}, **extra}


def raw(data=None, status=200, headers=None, text=None):
    payload = text if text is not None else json.dumps(body() if data is None else data).encode()
    return RawResponse(status, headers or {}, payload)


class FakeTransport:
    def __init__(self, *responses, on_call=None):
        self.responses = list(responses)
        self.calls = []
        self.on_call = on_call

    def __call__(self, url, headers, timeout, max_bytes):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if self.on_call:
            self.on_call()
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r


def live(*responses, budget=None, environ=None, on_call=None, **kw):
    t = FakeTransport(*(responses or (raw(),)), on_call=on_call)
    b = budget if budget is not None else CallBudget(10)
    p = HunterProvider(b, dry_run=False, http=HttpClient((hunter.API_HOST,), transport=t),
                       environ={API_KEY_ENV: KEY} if environ is None else environ, **kw)
    return p, t, b


def everything(e):
    return "".join(traceback.format_exception(e)) + repr(e) + str(e.args)


class Request(unittest.TestCase):
    def test_one_domain_search_with_key_in_header_only(self):
        p, t, _ = live()
        p.find_people(DOMAIN, (), 5)
        self.assertEqual(len(t.calls), 1)
        call = t.calls[0]
        parts = urlsplit(call["url"])
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", hunter.DOMAIN_SEARCH_URL)
        self.assertEqual(parse_qs(parts.query), {"domain": [DOMAIN], "limit": ["5"]})
        self.assertNotIn(KEY, call["url"])
        self.assertNotIn("SENTINEL", call["url"])
        self.assertEqual(call["headers"][hunter.API_KEY_HEADER], KEY)
        self.assertEqual([v for v in call["headers"].values() if KEY in v], [KEY])
        self.assertEqual(call["timeout"], hunter.REQUEST_TIMEOUT_SECONDS)

    def test_seniority_and_department(self):
        p, t, _ = live(seniority="executive", department="it")
        p.find_people(DOMAIN, (), 3)
        q = parse_qs(urlsplit(t.calls[0]["url"]).query)
        self.assertEqual(q["seniority"], ["executive"])
        self.assertEqual(q["department"], ["it"])
        for kw in ({"seniority": "boss"}, {"department": "SENTINEL"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                HunterProvider(**kw)

    def test_limit_parameter_capped(self):
        p, t, _ = live()
        p.find_people(DOMAIN, (), 500)
        self.assertEqual(parse_qs(urlsplit(t.calls[0]["url"]).query)["limit"],
                         [str(hunter.MAX_LIMIT)])

    def test_input_validation_before_any_call(self):
        p, t, b = live()
        for args in (("", (), 1), ("not a domain", (), 1), ("https://acme.example.com", (), 1),
                     ("a" * 300 + ".example.com", (), 1), (DOMAIN, ["CTO"], 1),
                     (DOMAIN, ("",), 1), (DOMAIN, (), 0), (DOMAIN, (), True), (None, (), 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                p.find_people(*args)
        self.assertEqual((t.calls, b.used), ([], 0))

    def test_missing_or_bad_key(self):
        for env, code in (({}, "missing_key"), ({API_KEY_ENV: ""}, "missing_key"),
                          ({API_KEY_ENV: "   "}, "missing_key"),
                          ({API_KEY_ENV: KEY + "\r\nX: y"}, "bad_key"),
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
        p = HunterProvider(CallBudget(2), dry_run=False,
                           http=HttpClient((hunter.API_HOST,), transport=t))
        with mock.patch.dict(os.environ, {API_KEY_ENV: KEY}):
            p.find_people(DOMAIN, (), 1)
        self.assertEqual(t.calls[0]["headers"][hunter.API_KEY_HEADER], KEY)


class Parsing(unittest.TestCase):
    def test_maps_people(self):
        p, _, _ = live()
        people = p.find_people(DOMAIN, (), 10)
        self.assertEqual(len(people), 5)
        first = people[0]
        self.assertIsInstance(first, Person)
        self.assertEqual((first.name, first.email, first.title, first.company_domain,
                          first.source, first.confidence),
                         ("Avery Alder", addr("avery.alder"), "CTO", DOMAIN, "hunter", 0.9))

    def test_title_filter_case_insensitive(self):
        p, _, _ = live()
        self.assertEqual([x.title for x in p.find_people(DOMAIN, ("cto",), 10)],
                         ["CTO", "Co-founder & cto"])
        self.assertEqual([x.title for x in p.find_people(DOMAIN, ("VP ENGINEERING",), 10)],
                         ["VP Engineering"])
        self.assertEqual([x.title for x in p.find_people(DOMAIN, ("Engineering", "designer"), 10)],
                         ["VP Engineering", "Designer", "Head of Engineering"])
        self.assertEqual(p.find_people(DOMAIN, ("Chief Llama",), 10), [])

    def test_limit_caps_results(self):
        for limit, n in ((1, 1), (2, 2), (5, 5), (50, 5)):
            with self.subTest(limit=limit):
                p, _, _ = live()
                self.assertEqual(len(p.find_people(DOMAIN, (), limit)), n)
        p, _, _ = live()
        self.assertEqual([x.title for x in p.find_people(DOMAIN, ("cto",), 1)], ["CTO"])

    def test_score_scaling(self):
        cases = [(90, 0.9), (100, 1.0), (0, 0.0), (45.5, 0.455), (None, 0.0), (150, 0.0),
                 (-5, 0.0), ("90", 0.0), (True, 0.0), (float("nan"), 0.0)]
        rows = [entry("Avery", f"Alder{i}", "CTO", score) for i, (score, _) in enumerate(cases)]
        rows.append({k: v for k, v in entry("Blake", "Birch", "CTO").items() if k != "confidence"})
        p, _, _ = live(raw(body(rows)))
        got = [x.confidence for x in p.find_people(DOMAIN, (), 50)]
        self.assertEqual(got, [want for _, want in cases] + [0.0])

    def test_unusable_entries_skipped(self):
        rows = [entry("Avery", "Alder", "CTO", email=""),
                {**entry("Blake", "Birch", "CTO"), "value": None},
                entry("Casey", "Cedar", "CTO", email="no-at-sign"),
                entry("Devon", "Hazel", "CTO", email="two" + "@" + "at" + "@" + "example.invalid"),
                entry("Emery", "Maple", "CTO", email="sp ace" + "@" + "example.invalid"),
                {**entry("", "", "CTO"), "value": addr("anon")},
                entry("Finley", "Rowan", ""), {**entry("Harper", "Linden", "CTO"), "position": None},
                {**entry("Jordan", "Willow", "CTO"), "first_name": None},
                entry("Kept", "Person", "CTO")]
        p, _, _ = live(raw(body(rows)))
        people = p.find_people(DOMAIN, (), 50)
        self.assertEqual([(x.name, x.title) for x in people],
                         [("Willow", "CTO"), ("Kept Person", "CTO")])

    def test_unexpected_shapes(self):
        for data in ([], {}, {"data": []}, {"data": {}}, {"data": {"emails": {}}},
                     {"data": {"emails": [1]}}, {"data": {"emails": ["x"]}}, "text", 7, None):
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

    def test_empty_result(self):
        p, _, _ = live(raw(body([])))
        self.assertEqual(p.find_people(DOMAIN, ("CTO",), 5), [])


class Statuses(unittest.TestCase):
    CASES = [(401, {}, "auth_failed"), (403, {}, "auth_failed"), (402, {}, "plan_exhausted"),
             (429, {"Retry-After": "7"}, "retry_after_7"),
             (429, {"Retry-After": "0"}, "retry_after_0"),
             (429, {"Retry-After": "60"}, "retry_after_60"),
             (429, {"Retry-After": " 12 "}, "retry_after_12"),
             (429, {"Retry-After": "61"}, "rate_limited"),
             (429, {"Retry-After": "3600"}, "rate_limited"),
             (429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, "rate_limited"),
             (429, {"Retry-After": "-1"}, "rate_limited"), (429, {}, "rate_limited"),
             (400, {}, "bad_request"), (422, {}, "bad_request"), (500, {}, "server_error"),
             (502, {}, "server_error"), (503, {"Retry-After": "5"}, "server_error"),
             (599, {}, "server_error"), (418, {}, "unexpected_status"),
             (204, {}, "unexpected_shape")]

    def run_status(self, status, headers, data=None):
        # The body is a VALID success body, so a status treated as success would return people.
        p, t, b = live(raw(data if data is not None else body(), status=status, headers=headers))
        with self.assertRaises(ProviderError) as cm:
            p.find_people(DOMAIN, (), 5)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(b.used, 1)
        text = everything(cm.exception)
        self.assertNotIn(KEY, text)
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn("example.invalid", text)
        return cm.exception.code

    def test_status_mapping(self):
        for status, headers, code in self.CASES:
            with self.subTest(status=status, headers=headers):
                if status == 204:
                    p, _, _ = live(raw(status=204, text=b"{}"))
                    with self.assertRaises(ProviderError) as cm:
                        p.find_people(DOMAIN, (), 5)
                    self.assertEqual(cm.exception.code, code)
                    continue
                self.assertEqual(self.run_status(status, headers), code)

    def test_non_json_error_bodies_still_map(self):
        for status, code in ((401, "auth_failed"), (429, "rate_limited"), (500, "server_error")):
            with self.subTest(status=status):
                p, _, _ = live(raw(status=status, text=b"<html>SENTINELBODY</html>"))
                with self.assertRaises(ProviderError) as cm:
                    p.find_people(DOMAIN, (), 5)
                self.assertEqual(cm.exception.code, code)

    def test_plan_exhausted_error_id(self):
        for status in (403, 429, 400):
            for err_id in hunter.PLAN_EXHAUSTED_ERROR_IDS:
                with self.subTest(status=status, err_id=err_id):
                    data = {"errors": [{"id": err_id, "code": status, "details": "SENTINELBODY"}]}
                    self.assertEqual(self.run_status(status, {}, data), "plan_exhausted")
        data = {"errors": [{"id": hunter.PLAN_EXHAUSTED_ERROR_IDS[0]}]}
        self.assertEqual(self.run_status(401, {}, data), "auth_failed")

    def test_rate_limit_never_sleeps_or_retries(self):
        with mock.patch("time.sleep", side_effect=AssertionError("slept")):
            code = self.run_status(429, {"Retry-After": "30"})
        self.assertEqual(code, "retry_after_30")

    def test_transport_timeout(self):
        p, t, b = live(ProviderError("timeout"))
        with self.assertRaises(ProviderError) as cm:
            p.find_people(DOMAIN, (), 5)
        self.assertEqual((cm.exception.code, len(t.calls), b.used), ("timeout", 1, 1))

    def test_error_code_function_direct(self):
        self.assertEqual(hunter.error_code(429, None, None), "rate_limited")
        self.assertEqual(hunter.error_code(429, {"errors": "x"}, {"retry-after": 5}),
                         "rate_limited")
        self.assertEqual(hunter.error_code(404, None, {}), "unexpected_status")


class Budget(unittest.TestCase):
    def test_consumed_before_the_call(self):
        seen = []
        budget = CallBudget(3)
        def record():
            seen.append(budget.used)
        p, t, b = live(budget=budget, on_call=record)
        p.find_people(DOMAIN, (), 1)
        p.find_people(DOMAIN, (), 1)
        self.assertEqual(seen, [1, 2])
        self.assertEqual(b.used, 2)

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
            HunterProvider(dry_run=False)
        with self.assertRaises(TypeError):
            HunterProvider(25, dry_run=False)
        with self.assertRaises(TypeError):
            HunterProvider(CallBudget(1), dry_run="no")
        b = CallBudget(4)
        self.assertIs(HunterProvider(b, dry_run=False)._budget, b)

    def test_through_find_people_within(self):
        p, t, b = live(budget=CallBudget(4))
        people = find_people_within(b, p, DOMAIN, ("cto",), 1)
        self.assertEqual(len(people), 1)
        self.assertEqual((len(t.calls), b.used), (1, 2))  # one guard charge, one network charge


class DryRun(unittest.TestCase):
    def test_default_is_dry_run_and_makes_zero_calls(self):
        t = FakeTransport(raw())
        out = io.StringIO()
        b = CallBudget(5)
        p = HunterProvider(b, http=HttpClient((hunter.API_HOST,), transport=t),
                           environ={API_KEY_ENV: KEY}, out=out)
        self.assertTrue(p.dry_run)
        people = p.find_people(DOMAIN, ("CTO",), 2)
        self.assertEqual(t.calls, [])
        self.assertEqual(b.used, 0)
        self.assertEqual(people, FakeProvider().find_people(DOMAIN, ("CTO",), 2))
        self.assertEqual(out.getvalue(), DRY_RUN_NOTICE + "\n")
        self.assertNotIn(KEY, out.getvalue())

    def test_bare_default_builds_no_client(self):
        p = HunterProvider(out=io.StringIO())
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=AssertionError("network")):
            p.find_people(DOMAIN, (), 1)
        self.assertIsNone(p._http)

    def test_notice_goes_to_stderr_by_default(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            HunterProvider().find_people(DOMAIN, (), 1)
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("dry run", err.getvalue())


class Credits(unittest.TestCase):
    def test_recorded_from_assumed_fields(self):
        for meta, n in (({"requests_remaining": 42}, 42), ({"credits_remaining": 0}, 0),
                        ({"calls": {"available": 9}}, 9)):
            with self.subTest(meta=meta):
                p, _, _ = live(raw(body(meta=meta)))
                self.assertIsNone(p.credits_remaining)
                p.find_people(DOMAIN, (), 1)
                self.assertEqual(p.credits_remaining, n)
                self.assertIsInstance(p.credits_remaining, int)

    def test_ignored_when_absent_or_bad(self):
        for meta in ({}, {"requests_remaining": "42"}, {"requests_remaining": -1},
                     {"requests_remaining": True}, {"requests_remaining": 4.5}):
            with self.subTest(meta=meta):
                p, _, _ = live(raw(body(meta=meta)))
                p.find_people(DOMAIN, (), 1)
                self.assertIsNone(p.credits_remaining)

    def test_kept_across_calls_and_read_from_error_bodies(self):
        p, _, _ = live(raw(body(meta={"requests_remaining": 5})), raw(body(meta={})),
                       raw({"meta": {"credits_remaining": 0}, "errors": []}, status=402))
        p.find_people(DOMAIN, (), 1)
        p.find_people(DOMAIN, (), 1)
        self.assertEqual(p.credits_remaining, 5)
        with self.assertRaises(ProviderError):
            p.find_people(DOMAIN, (), 1)
        self.assertEqual(p.credits_remaining, 0)

    def test_dry_run_records_nothing(self):
        p = HunterProvider(out=io.StringIO())
        p.find_people(DOMAIN, (), 1)
        self.assertIsNone(p.credits_remaining)


class RegistryAndDoctor(unittest.TestCase):
    def test_registered(self):
        self.assertIn("hunter", list_providers())
        p = get("hunter")
        self.assertIsInstance(p, HunterProvider)
        self.assertIsInstance(p, PeopleProvider)
        self.assertTrue(p.dry_run)
        self.assertIsNone(p._budget)
        self.assertEqual((p.name, p.required_env), ("hunter", ("HUNTER_API_KEY",)))
        self.assertNotIn("hunter", PLANNED)

    def run_doctor(self, env, provider=None, **kw):
        reg = Registry()
        reg.register(FakeProvider())
        reg.register(provider or HunterProvider())
        out = io.StringIO()
        code = doctor.run(environ={config.PROVIDER: "hunter", **env}, registry=reg, out=out, **kw)
        return code, out.getvalue()

    def test_key_set(self):
        code, out = self.run_doctor({API_KEY_ENV: KEY})
        self.assertEqual(code, 0, out)
        self.assertIn("PASS HUNTER_API_KEY is set", out)
        self.assertIn("WARN hunter: live use is not enabled until dry run is turned off", out)
        self.assertNotIn(KEY, out)
        self.assertNotIn("SENTINEL", out)
        self.assertNotIn("credits", out)

    def test_key_missing_fails(self):
        for env in ({}, {API_KEY_ENV: ""}, {API_KEY_ENV: "  "}):
            with self.subTest(env=env):
                code, out = self.run_doctor(env)
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL HUNTER_API_KEY is missing", out)

    def test_no_live_warning_when_dry_run_off(self):
        _, out = self.run_doctor({API_KEY_ENV: KEY}, settings=Settings(dry_run=False))
        self.assertNotIn("live use is not enabled", out)

    def test_credits_exposed(self):
        p = HunterProvider()
        p.credits_remaining = 17
        _, out = self.run_doctor({API_KEY_ENV: KEY}, provider=p)
        self.assertIn("PASS hunter: 17 requests or credits remaining at the last response", out)

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
                 "PYTHONDONTWRITEBYTECODE": "1", config.PROVIDER: "hunter", API_KEY_ENV: KEY},
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("PASS HUNTER_API_KEY is set", r.stdout)
        self.assertNotIn("SENTINEL", r.stdout + r.stderr)
        self.assertNotIn(KEY, r.stdout + r.stderr)

    def test_repr_hides_internals(self):
        p, _, _ = live()
        self.assertNotIn(KEY, repr(p))


class Source(unittest.TestCase):
    def test_unverified_note_on_the_constants_block(self):
        text = (Path(hunter.__file__)).read_text()
        self.assertIn("verify against Hunter's current API documentation before first live use",
                      text)
        head = text.split("# ---", 2)[1]
        for const in ("DOMAIN_SEARCH_URL", "API_KEY_HEADER", "PARAM_DOMAIN", "PARAM_LIMIT",
                      "FIELD_EMAIL", "FIELD_SCORE", "CREDITS_REMAINING_PATHS"):
            self.assertIn(const + " =", head)

    def test_live_check_recorded(self):
        root = Path(__file__).resolve().parent.parent
        doc = (root / "docs/hunter.md").read_text()
        self.assertIn("Checked live on 2026-10-22 (one lookup, three results, emails present):", doc)
        unchecked = doc.split("Still unchecked", 1)[1].split("\n## ", 1)[0]
        for const in ("PARAM_SENIORITY", "PARAM_DEPARTMENT", "CREDITS_REMAINING_PATHS",
                      "PLAN_EXHAUSTED_ERROR_IDS"):
            self.assertIn(const, unchecked)
        # The live-checked values, pinned so a change has to update the docs as well.
        self.assertEqual((hunter.DOMAIN_SEARCH_URL, hunter.API_KEY_HEADER, hunter.PARAM_DOMAIN,
                          hunter.FIELD_EMAIL, hunter.FIELD_POSITION, hunter.FIELD_FIRST_NAME,
                          hunter.FIELD_LAST_NAME, hunter.FIELD_SCORE),
                         ("https://api.hunter.io/v2/domain-search", "X-API-KEY", "domain",
                          "value", "position", "first_name", "last_name", "confidence"))
        word = re.compile(r"\b" + "lam" + "bda" + r"\b", re.IGNORECASE)
        for name in ("src/repo_signals/providers/hunter.py", "docs/hunter.md",
                     "tests/test_hunter.py"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((root / name).read_text()))

    def test_limit_checked_and_max_limit_unchecked(self):
        root = Path(__file__).resolve().parent.parent
        doc = (root / "docs/hunter.md").read_text()
        checked = doc.split("Checked live on 2026-10-22", 1)[1].split("Still unchecked", 1)[0]
        unchecked = doc.split("Still unchecked", 1)[1].split("\n## ", 1)[0]
        self.assertIn("- [x] `PARAM_LIMIT`", checked)
        self.assertIn("- [x] `RESP_DATA`, `RESP_EMAILS`", checked)
        self.assertNotIn("MAX_LIMIT", checked)
        self.assertIn("- [ ] `MAX_LIMIT`", unchecked)
        self.assertNotIn("PARAM_LIMIT", unchecked)
        flat = " ".join(doc.split())
        self.assertIn("In a live check on 2026-10-22 the credits-remaining value was not found at "
                      "any path the code reads, so the tool shows no remaining credits.", flat)
        self.assertIn("A domain search can spend one credit per call.", flat)
        self.assertNotIn("Until the credits-remaining field is checked", flat)
        # The newly checked values, pinned so a change has to update the docs as well.
        self.assertEqual((hunter.PARAM_LIMIT, hunter.RESP_DATA, hunter.RESP_EMAILS),
                         ("limit", "data", "emails"))
        word = re.compile(r"\b" + "lam" + "bda" + r"\b", re.IGNORECASE)
        for name in ("src/repo_signals/providers/hunter.py", "docs/hunter.md",
                     "tests/test_hunter.py"):
            with self.subTest(file=name):
                self.assertIsNone(word.search((root / name).read_text()))

    def test_import_order_independent(self):
        for first in ("repo_signals.http", "repo_signals.providers.hunter",
                      "repo_signals.providers", "repo_signals.doctor"):
            with self.subTest(first=first):
                code = (f"import sys; sys.path.insert(0, sys.argv[1]); import {first}; "
                        "from repo_signals.providers import list_providers; "
                        "print(list_providers())")
                r = subprocess.run([sys.executable, "-c", code, str(_src.SRC)],
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                   capture_output=True, text=True)
                self.assertEqual(r.stdout.strip(), "('apollo', 'fake', 'hunter')", r.stderr)


if __name__ == "__main__":
    unittest.main()
