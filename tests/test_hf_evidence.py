"""Build step 2 of the Hugging Face source: listing records to Evidence. Pure functions,
synthetic records only (example-org names), no network."""
import unittest
from datetime import date

import _src  # noqa: F401
from repo_signals.collector.evidence import EvidenceError
from repo_signals.collector.filters import FilterReport
from repo_signals.collector.hf_evidence import (build_hf_evidence, check_owner, check_public,
                                                check_recent, hf_evidence_for_target,
                                                matching_term)
from repo_signals.collector.hf_fields import HFRecord, parse_hf_record
from repo_signals.collector.signal import HF_KINDS, Signal, Target
from repo_signals.drafting.base import Sender
from repo_signals.drafting.guard import GuardedDrafter
from repo_signals.drafting.template import TemplateDrafter
from repo_signals.models import Evidence, Person

TODAY = date(2026, 10, 26)
TARGET = Target("example.com", "example-org", True)


def rec(name="examplelib-tools", owner="example-org", modified="2026-10-20T08:30:00.000Z",
        **extra):
    raw = {"id": f"{owner}/{name}", "lastModified": modified, "private": False}
    raw.update(extra)
    return raw


def signal(kinds=HF_KINDS, days=30, terms=("examplelib",)):
    return Signal(name="synthetic-hf", terms=terms, targets=(TARGET,), source="huggingface",
                  kinds=kinds, modified_within_days=days)


def parsed(raw, kind="model"):
    record, code = parse_hf_record(raw, kind)
    assert code is None, code
    return record


def run(records_by_kind, sig=None):
    return hf_evidence_for_target(records_by_kind, TARGET, sig or signal(), TODAY)


class Allowlist(unittest.TestCase):
    def test_only_allowlisted_keys_are_read(self):
        raw = rec(author="example-org", gated=False, disabled=False,
                  description="SENTINELDESC visit www.example.com", tags=["SENTINELTAG"],
                  downloads=999, likes=5, cardData={"text": "SENTINELCARD"},
                  siblings=[{"rfilename": "SENTINELFILE"}])
        record = parsed(raw)
        self.assertEqual(record, HFRecord(kind="model", org="example-org", name="examplelib-tools",
                                          author="example-org", last_modified=date(2026, 10, 20),
                                          private=False, gated=False, disabled=False))
        evidence, _ = run({"model": [raw]})
        text = repr(evidence)
        for sentinel in ("SENTINEL", "999", "www.example.com"):
            self.assertNotIn(sentinel, text)

    def test_unusable_records_are_dropped_with_a_code(self):
        cases = [("not a dict", ["example-org/x"], "malformed_record"),
                 ("missing id", {"lastModified": "2026-10-20T00:00:00.000Z"}, "missing_id"),
                 ("empty id", rec() | {"id": ""}, "missing_id"),
                 ("missing lastModified", {"id": "example-org/examplelib"},
                  "missing_last_modified"),
                 ("no slash", rec() | {"id": "example-org"}, "bad_id"),
                 ("extra slash", rec() | {"id": "example-org/a/examplelib"}, "bad_id"),
                 ("space in name", rec(name="example lib"), "bad_id"),
                 ("dot-dot name", rec(name=".."), "bad_id"),
                 ("leading hyphen owner", rec(owner="-example-org"), "bad_id"),
                 ("odd character", rec(name="examplelib<x>"), "bad_id"),
                 ("date only", rec(modified="2026-10-20"), "bad_last_modified"),
                 ("offset not Z", rec(modified="2026-10-20T00:00:00+00:00"), "bad_last_modified"),
                 ("impossible date", rec(modified="2026-13-40T00:00:00.000Z"),
                  "bad_last_modified"),
                 ("author not text", rec(author=7), "malformed_record")]
        for label, raw, code in cases:
            with self.subTest(label=label):
                self.assertEqual(parse_hf_record(raw, "model"), (None, code))
        self.assertEqual(parse_hf_record(rec(), "weights"), (None, "bad_kind"))
        self.assertEqual(parse_hf_record(rec(modified="2026-10-20T08:30:00Z"), "model")[1], None)


class Filters(unittest.TestCase):
    def test_owner(self):
        self.assertEqual(check_owner(parsed(rec()), "example-org"), (True, None))
        self.assertEqual(check_owner(parsed(rec(owner="Example-Org")), "example-org"), (True, None))
        self.assertEqual(check_owner(parsed(rec(owner="other-org")), "example-org"),
                         (False, "owner_not_target"))
        self.assertEqual(check_owner(parsed(rec(author="other-org")), "example-org"),
                         (False, "owner_not_target"))

    def test_public(self):
        self.assertEqual(check_public(parsed(rec())), (True, None))
        self.assertEqual(check_public(parsed(rec(gated=False, disabled=False))), (True, None))
        for extra, code in (({"private": True}, "private"), ({"private": None}, "private"),
                            ({"gated": True}, "gated"), ({"gated": "auto"}, "gated"),
                            ({"disabled": True}, "disabled")):
            with self.subTest(extra=extra):
                self.assertEqual(check_public(parsed(rec(**extra))), (False, code))
        no_private = rec()
        del no_private["private"]
        self.assertEqual(check_public(parsed(no_private)), (False, "private"))

    def test_window_boundaries(self):
        for modified, result in (("2026-10-26T23:59:59.999Z", (True, None)),
                                 ("2026-09-26T00:00:00.000Z", (True, None)),       # 30 days
                                 ("2026-09-25T23:59:59.000Z", (False, "outside_window")),
                                 ("2026-10-27T00:00:00.000Z", (False, "modified_in_future"))):
            with self.subTest(modified=modified):
                self.assertEqual(check_recent(parsed(rec(modified=modified)), 30, TODAY), result)
        with self.assertRaises(TypeError):
            check_recent(parsed(rec()), 30, "2026-10-26")

    def test_term_must_be_in_the_name_part(self):
        self.assertEqual(matching_term(parsed(rec(name="My-ExampleLib-Server")), ("examplelib",)),
                         "examplelib")
        self.assertIsNone(matching_term(parsed(rec(owner="examplelib-org", name="tools")),
                                        ("examplelib",)))
        self.assertEqual(matching_term(parsed(rec(name="b-lib")), ("a-lib", "b-lib")), "b-lib")


class EvidenceOutput(unittest.TestCase):
    def test_url_and_summary_per_kind(self):
        for kind, url, noun in (("model", "https://huggingface.co/example-org/examplelib-tools",
                                 "model"),
                                ("dataset",
                                 "https://huggingface.co/datasets/example-org/examplelib-tools",
                                 "dataset"),
                                ("space",
                                 "https://huggingface.co/spaces/example-org/examplelib-tools",
                                 "Space")):
            with self.subTest(kind=kind):
                ev = build_hf_evidence(parsed(rec(), kind), "examplelib", "example.com", TODAY,
                                       terms=("examplelib",))
                self.assertEqual(ev, Evidence(
                    "example.com",
                    f"The public Hugging Face {noun} example-org/examplelib-tools matches the term "
                    "examplelib, last modified on 2026-10-20.", url, f"hf-{kind}"))

    def test_summary_rules(self):
        long_name = "examplelib-" + "x" * 85
        ev = build_hf_evidence(parsed(rec(name=long_name)), "examplelib", "example.com", TODAY,
                               terms=("examplelib",))
        self.assertLessEqual(len(ev.summary), 300)
        self.assertTrue(ev.summary.isprintable())
        self.assertNotIn("\n", ev.summary)
        self.assertNotIn("://", ev.summary)
        self.assertNotIn("@", ev.summary)
        with self.assertRaises(EvidenceError) as cm:
            build_hf_evidence(parsed(rec()), "otherterm", "example.com", TODAY,
                              terms=("examplelib",))
        self.assertEqual(cm.exception.code, "term_not_in_signal")
        with self.assertRaises(EvidenceError) as cm:
            build_hf_evidence(parsed(rec(name="www.examplelib")), "examplelib", "example.com",
                              TODAY, terms=("examplelib",))
        self.assertEqual(cm.exception.code, "summary_has_url")

    def test_the_drafting_guard_accepts_hugging_face_evidence(self):
        ev = build_hf_evidence(parsed(rec(), "dataset"), "examplelib", "example.com", TODAY,
                               terms=("examplelib",))
        sender = Sender("Avery Alder", "Engineer", "Example Org", "1 Example Street",
                        "Reply stop to opt out.")
        person = Person("Blake Birch", None, "CTO", "example.com", "fake", 0.5)
        draft = GuardedDrafter(TemplateDrafter("intro")).draft(person, (ev,), sender)
        self.assertIn(ev.summary, draft.body)
        self.assertIn(ev.url, draft.body)


class ForTarget(unittest.TestCase):
    def test_each_drop_code_is_counted(self):
        records = {"model": [rec(), rec(owner="other-org"), rec(private=True), rec(gated="manual"),
                             rec(disabled=True), rec(modified="2026-01-01T00:00:00.000Z"),
                             rec(modified="2026-11-01T00:00:00.000Z"), rec(name="unrelated"),
                             "not a record", rec(name="examplelib-tools")],
                   "dataset": [rec(name="examplelib-data")], "space": []}
        evidence, report = run(records)
        self.assertEqual([e.url for e in evidence],
                         ["https://huggingface.co/example-org/examplelib-tools",
                          "https://huggingface.co/datasets/example-org/examplelib-data"])
        self.assertEqual(report.kept, 2)
        self.assertEqual(dict(report.dropped), {
            "owner_not_target": 1, "private": 1, "gated": 1, "disabled": 1,
            "outside_window": 1, "modified_in_future": 1, "term_not_in_name": 1,
            "malformed_record": 1, "duplicate_url": 1})

    def test_cap_of_ten_and_only_asked_kinds(self):
        records = {"model": [rec(name=f"examplelib-{i}") for i in range(12)],
                   "space": [rec(name="examplelib-space")]}
        evidence, report = run(records, signal(kinds=("model",)))
        self.assertEqual(len(evidence), 10)
        self.assertEqual(report.dropped["over_target_cap"], 2)
        self.assertTrue(all(e.kind == "hf-model" for e in evidence))
        self.assertIsInstance(report, FilterReport)

    def test_a_space_without_a_gated_key_yields_evidence(self):
        space = rec(name="examplelib-demo", disabled=False)    # as observed: no gated key
        self.assertNotIn("gated", space)
        evidence, report = run({"space": [space]}, signal(kinds=("space",)))
        url = "https://huggingface.co/spaces/example-org/examplelib-demo"
        self.assertEqual([(e.kind, e.url) for e in evidence], [("hf-space", url)])
        self.assertEqual((report.kept, dict(report.dropped)), (1, {}))

    def test_a_gated_model_is_still_dropped(self):
        for gated in ("auto", "manual", True):
            with self.subTest(gated=gated):
                model = rec(name="examplelib-7b", author="example-org", gated=gated,
                            disabled=False)
                evidence, report = run({"model": [model]}, signal(kinds=("model",)))
                self.assertEqual(evidence, ())
                self.assertEqual(dict(report.dropped), {"gated": 1})

    def test_needs_a_hugging_face_signal(self):
        github = Signal(name="synthetic", terms=("examplelib",), targets=(TARGET,))
        with self.assertRaises(TypeError):
            hf_evidence_for_target({}, TARGET, github, TODAY)


if __name__ == "__main__":
    unittest.main()
