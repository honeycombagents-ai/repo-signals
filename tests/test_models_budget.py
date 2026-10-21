import unittest

import _src  # noqa: F401
from repo_signals.budget import (DEFAULT_MAX_CALLS, HARD_MAX_CALLS, BudgetExceeded,
                                 CallBudget)
from repo_signals.models import Person


def person(**over):
    base = dict(name="Avery Alder", email=None, title="CTO", company_domain="acme.example.invalid",
                source="fake", confidence=0.7)
    base.update(over)
    return Person(**base)


class PersonTests(unittest.TestCase):
    def test_email_may_be_none(self):
        self.assertIsNone(person().email)

    def test_is_frozen(self):
        p = person()
        with self.assertRaises(Exception):
            p.name = "other"

    def test_confidence_bounds(self):
        self.assertEqual(person(confidence=0).confidence, 0.0)
        self.assertEqual(person(confidence=1).confidence, 1.0)
        self.assertIsInstance(person(confidence=1).confidence, float)
        for bad in (-0.01, 1.01, True, "0.5", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                person(confidence=bad)

    def test_rejects_empty_fields_and_bad_email(self):
        for field in ("name", "title", "company_domain", "source"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                person(**{field: " "})
        for bad in ("no-at-sign", "a" + "@" + "b" + "@" + "c.invalid", "a b" + "@" + "x.invalid"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                person(email=bad)


class BudgetTests(unittest.TestCase):
    def test_consumes_up_to_the_cap_exactly(self):
        b = CallBudget(3)
        b.consume()
        b.consume(2)
        self.assertEqual((b.used, b.remaining), (3, 0))
        with self.assertRaises(BudgetExceeded):
            b.consume()

    def test_over_cap_consumes_nothing(self):
        b = CallBudget(2)
        b.consume()
        with self.assertRaises(BudgetExceeded):
            b.consume(2)
        self.assertEqual(b.used, 1)
        b.consume()
        self.assertEqual(b.remaining, 0)

    def test_cap_of_one(self):
        b = CallBudget(1)
        b.consume()
        with self.assertRaises(BudgetExceeded):
            b.consume()

    def test_hard_bound(self):
        self.assertEqual(HARD_MAX_CALLS, 500)
        self.assertEqual(CallBudget(HARD_MAX_CALLS).max_calls, 500)
        with self.assertRaises(ValueError):
            CallBudget(HARD_MAX_CALLS + 1)
        self.assertLessEqual(DEFAULT_MAX_CALLS, HARD_MAX_CALLS)

    def test_rejects_invalid_max_calls(self):
        for bad in (0, -1, 1.5, "10", None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CallBudget(bad)

    def test_rejects_invalid_n(self):
        b = CallBudget(5)
        for bad in (0, -1, 1.0, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                b.consume(bad)
        self.assertEqual(b.used, 0)


if __name__ == "__main__":
    unittest.main()
