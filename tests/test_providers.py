import os
import subprocess
import sys
import unittest

import _src  # noqa: F401
from repo_signals.budget import BudgetExceeded, CallBudget
from repo_signals.models import Person
from repo_signals.providers import (PLANNED, DuplicateProviderError, PeopleProvider,
                                    ProviderError, Registry, RegistryError,
                                    UnknownProviderError, find_people_within, get,
                                    list_providers)
from repo_signals.providers.fake import FAKE_EMAIL_DOMAIN, FakeProvider


class Stub:
    def __init__(self, name="stub", required_env=(), people=None):
        self.name = name
        self.required_env = required_env
        self._people = people if people is not None else []
        self.calls = 0

    def find_people(self, company_domain, titles, limit):
        self.calls += 1
        return list(self._people)


class RegistryTests(unittest.TestCase):
    def test_register_get_list(self):
        r = Registry()
        s = Stub()
        r.register(s)
        self.assertIs(r.get("stub"), s)
        self.assertEqual(r.names(), ("stub",))
        self.assertIn("stub", r)

    def test_duplicate_name_refused(self):
        r = Registry()
        r.register(Stub())
        with self.assertRaises(DuplicateProviderError):
            r.register(Stub())
        self.assertEqual(r.names(), ("stub",))

    def test_unknown_name_raises_clear_error(self):
        r = Registry()
        r.register(Stub())
        with self.assertRaises(UnknownProviderError) as cm:
            r.get("nosuch")
        msg = str(cm.exception)
        self.assertIn("unknown provider", msg)
        self.assertIn("registered: stub", msg)
        self.assertNotIn("nosuch", msg)

    def test_planned_names_are_reserved_not_implemented(self):
        self.assertEqual(PLANNED, ("hunter", "apollo"))
        for name in PLANNED:
            with self.subTest(name=name):
                self.assertNotIn(name, list_providers())
                with self.assertRaises(UnknownProviderError) as cm:
                    get(name)
                self.assertIn("not implemented yet", str(cm.exception))

    def test_default_registry_has_fake(self):
        self.assertEqual(list_providers(), ("fake",))
        self.assertIsInstance(get("fake"), FakeProvider)

    def test_rejects_malformed_providers(self):
        r = Registry()
        for bad in (Stub(name="Bad Name"), Stub(name=""), Stub(required_env=["X"]),
                    Stub(required_env=("lower",))):
            with self.subTest(name=bad.name, env=bad.required_env), \
                    self.assertRaises(RegistryError):
                r.register(bad)
        no_method = type("P", (), {"name": "p", "required_env": ()})()
        with self.assertRaises(RegistryError):
            r.register(no_method)

    def test_stub_satisfies_protocol(self):
        self.assertIsInstance(Stub(), PeopleProvider)
        self.assertIsInstance(FakeProvider(), PeopleProvider)


class FakeProviderTests(unittest.TestCase):
    ARGS = ("acme.example.invalid", ("CTO", "VP Engineering"), 3)

    def test_deterministic_in_process(self):
        self.assertEqual(FakeProvider().find_people(*self.ARGS),
                         FakeProvider().find_people(*self.ARGS))

    def test_deterministic_across_processes(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from repo_signals.providers.fake import FakeProvider;"
                "print(repr(FakeProvider().find_people('acme.example.invalid',"
                " ('CTO', 'VP Engineering'), 3)))")
        outs = set()
        for seed in ("1", "2", "random"):
            env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}
            outs.add(subprocess.run([sys.executable, "-c", code, str(_src.SRC)], env=env,
                                    capture_output=True, text=True, check=True).stdout)
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs.pop().strip(), repr(FakeProvider().find_people(*self.ARGS)))

    def test_synthetic_people(self):
        people = FakeProvider().find_people(*self.ARGS)
        self.assertEqual(len(people), 3)
        for p in people:
            self.assertIsInstance(p, Person)
            self.assertTrue(p.email.endswith("@" + FAKE_EMAIL_DOMAIN))
            self.assertEqual(p.source, "fake")
            self.assertEqual(p.company_domain, "acme.example.invalid")
            self.assertTrue(0.0 <= p.confidence <= 1.0)
        self.assertEqual([p.title for p in people], ["CTO", "VP Engineering", "CTO"])

    def test_different_inputs_differ(self):
        a = FakeProvider().find_people("a.example.invalid", ("CTO",), 3)
        b = FakeProvider().find_people("b.example.invalid", ("CTO",), 3)
        self.assertNotEqual([p.name for p in a], [p.name for p in b])

    def test_limit_and_validation(self):
        self.assertEqual(len(FakeProvider().find_people("x.example.invalid", (), 1)), 1)
        for args in (("", ("CTO",), 1), ("x.example.invalid", ["CTO"], 1),
                     ("x.example.invalid", ("CTO",), 0), ("x.example.invalid", ("CTO",), True)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                FakeProvider().find_people(*args)

    def test_needs_no_env(self):
        self.assertEqual(FakeProvider().required_env, ())


class ProviderErrorTests(unittest.TestCase):
    def test_message_is_the_code(self):
        e = ProviderError("rate_limited")
        self.assertEqual(str(e), "rate_limited")
        self.assertEqual(e.code, "rate_limited")

    def test_non_code_messages_are_dropped(self):
        leaky = ["https://api.example.invalid/v2?api_key=SENTINELKEY", "Bearer SENTINELKEY",
                 '{"error": "SENTINELBODY"}', "Rate limited", "", None]
        for msg in leaky:
            with self.subTest(msg=msg):
                e = ProviderError(msg)
                self.assertEqual(str(e), "unspecified")
                self.assertNotIn("SENTINEL", repr(e) + str(e.args))


class GuardedCallTests(unittest.TestCase):
    def test_every_call_is_charged(self):
        b, s = CallBudget(2), Stub()
        find_people_within(b, s, "x.example.invalid", ("CTO",), 5)
        find_people_within(b, s, "x.example.invalid", ("CTO",), 5)
        with self.assertRaises(BudgetExceeded):
            find_people_within(b, s, "x.example.invalid", ("CTO",), 5)
        self.assertEqual(s.calls, 2)  # the over-budget call never reached the provider

    def test_bad_responses_rejected(self):
        good = FakeProvider().find_people("x.example.invalid", ("CTO",), 3)
        for people in (good, [object()], "nope"):
            with self.subTest(n=len(people)), self.assertRaises(ProviderError):
                find_people_within(CallBudget(5), Stub(people=people), "x.example.invalid",
                                   ("CTO",), 2 if people is good else 5)

    def test_fake_through_the_guard(self):
        b = CallBudget(1)
        people = find_people_within(b, FakeProvider(), "x.example.invalid", ("CTO",), 2)
        self.assertEqual(len(people), 2)
        self.assertEqual(b.remaining, 0)


if __name__ == "__main__":
    unittest.main()
