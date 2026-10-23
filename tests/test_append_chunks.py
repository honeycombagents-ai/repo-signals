"""Sheet appends split under the request cap. Offline: a fake Google and synthetic people only."""
import json
import unittest
from pathlib import Path

import _src  # noqa: F401
import test_run as run_fx
import test_sheets as sheets_fx
from repo_signals.budget import CallBudget
from repo_signals.config import Settings
from repo_signals.drafting.base import Sender
from repo_signals.http import MAX_REQUEST_BYTES, RawResponse, json_body_size
from repo_signals.models import Evidence, Person
from repo_signals.run import draft_stage
from repo_signals.sheets.auth import ServiceAccountAuth
from repo_signals.sheets.client import SheetsClient, build_http
from repo_signals.sheets.layout import COLUMNS
from repo_signals.sheets.writer import (APPEND_CHUNK_BYTES, AppendStopped, SheetWriter,
                                        WriteResult, split_appends)

ROOT = Path(__file__).resolve().parent.parent
SENDER = Sender("Morgan Ellis", "Head of Developer Relations", "Example Analytics Inc.",
                "Example Analytics Inc.\n500 Example Avenue, Suite 1200\nExample City, CA 94000",
                "If you would rather not hear from us, reply with the word stop and we will not "
                "contact you again.")
FakeGoogle = sheets_fx.FakeGoogle


def typical_records(n, start=0):
    """``n`` synthetic people with real drafts: rows of about 1.3 KB, all on example.invalid."""
    people = []
    for i in range(start, start + n):
        domain = f"company{i:03d}.example.invalid"
        person = Person(f"Jordan Rivera{i}", f"jordan.rivera{i}@{domain}", "VP Engineering",
                        domain, "fake", 0.9)
        evidence = Evidence(domain, f"The public repository company{i:03d}-org/examplelib-server "
                                    "matches the term examplelib and its primary language on "
                                    "GitHub is Python, last pushed on 2026-10-14.",
                            f"https://example.invalid/company{i:03d}-org/examplelib-server", "repo")
        people.append((person, (evidence,)))
    records, counts = draft_stage(people, SENDER, None)
    assert counts["drafted"] == n
    return records


def oversize_record():
    """One record whose row alone is over 64 KB: two cells of 40,000 characters each."""
    domain = "huge.example.invalid"
    return (Person("N" * 40000, "huge" + "@" + domain, "T" * 40000, domain, "fake", 0.5), "x", "")


def appends(google):
    return [c for c in google.calls if c["kind"] == "append"]


def names(google):
    return [r[0] for r in google.rows]


class FailOnAppend(FakeGoogle):
    """A fake Google whose append number ``fail_at`` (1-based) answers 500."""

    def __init__(self, fail_at, **kw):
        super().__init__(**kw)
        self.fail_at = fail_at
        self.seen = 0

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        if sheets_fx.classify(method, url) == "append":
            self.seen += 1
            if self.seen == self.fail_at:
                self.calls.append({"kind": "append-failed", "method": method, "url": url,
                                   "headers": dict(headers), "body": body})
                return RawResponse(500, {}, b"{}")
        return super().__call__(url, headers, timeout, max_bytes, method, body)


class Splitting(unittest.TestCase):
    def test_chunks_are_ordered_full_and_under_the_margin(self):
        rows = [SheetWriter(Settings(), run_id="r").build_row(p, s, "t", d)
                for p, s, d in typical_records(120)]
        chunks, stop = split_appends(rows)
        self.assertIsNone(stop)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual([r for c in chunks for r in c], rows)
        for i, chunk in enumerate(chunks):
            self.assertLessEqual(json_body_size({"values": chunk}), APPEND_CHUNK_BYTES)
            if i + 1 < len(chunks):  # no needless split: the next row would not have fitted
                self.assertGreater(json_body_size({"values": chunk + chunks[i + 1][:1]}),
                                   APPEND_CHUNK_BYTES)

    def test_margin_is_below_the_cap(self):
        self.assertEqual(APPEND_CHUNK_BYTES, 60 * 1024)
        self.assertLess(APPEND_CHUNK_BYTES, MAX_REQUEST_BYTES)

    def test_a_row_between_the_margin_and_the_cap_goes_alone(self):
        big = ["x" * 62000] + [""] * (len(COLUMNS) - 1)
        small = ["a"] * len(COLUMNS)
        chunks, stop = split_appends([small, big, small])
        self.assertIsNone(stop)
        self.assertEqual(chunks, [[small], [big], [small]])

    def test_an_oversize_row_stops_the_split(self):
        small = ["a"] * len(COLUMNS)
        huge = ["x" * 40000, "y" * 40000] + [""] * (len(COLUMNS) - 2)
        self.assertEqual(split_appends([small, small, huge, small]), ([[small, small]], 2))


class Appends(sheets_fx.LiveCase):
    def test_120_typical_rows_in_order_in_two_or_more_appends_under_60_kb(self):
        records = typical_records(120)
        google = FakeGoogle(header=COLUMNS)
        result = self.writer(google).write(records)
        self.assertEqual(result, WriteResult(120, 0, False))
        sent = appends(google)
        self.assertGreaterEqual(len(sent), 2)
        for call in sent:
            self.assertLessEqual(len(call["body"]), APPEND_CHUNK_BYTES)
        self.assertEqual(names(google), [p.name for p, _, _ in records])
        self.assertEqual(len({r[1] for r in google.rows}), 120)
        self.assertEqual(self.out.getvalue(), f"sheets: appended 120 row(s) in {len(sent)} "
                                              "appends, skipped 0 duplicate(s)\n")

    def test_budget_charged_once_per_sent_append(self):
        seen = []
        budget = CallBudget(50)
        google = FakeGoogle(header=COLUMNS, on_call=lambda kind: seen.append((kind, budget.used)))
        self.writer(google, budget=budget).write(typical_records(120))
        n = len(appends(google))
        self.assertEqual(seen, [("token", 1), ("header", 2), ("rows", 3)]
                         + [("append", 4 + i) for i in range(n)])
        self.assertEqual(budget.used, 3 + n)

    def test_header_written_once_before_the_first_append(self):
        google = FakeGoogle()
        self.writer(google).write(typical_records(120))
        kinds = google.kinds()
        self.assertEqual(kinds.count("put"), 1)
        self.assertEqual(kinds[:4], ["token", "header", "rows", "put"])
        self.assertTrue(all(k == "append" for k in kinds[4:]))
        self.assertGreaterEqual(len(kinds[4:]), 2)
        self.assertEqual(google.header, list(COLUMNS))

    def test_failure_on_the_second_append_keeps_the_first_and_reports_counts(self):
        records = typical_records(120)
        google = FailOnAppend(2, header=COLUMNS)
        e = self.raises("server_error", self.writer(google).write, records)
        self.assertIsInstance(e, AppendStopped)
        first = len(google.rows)
        self.assertGreater(first, 0)
        self.assertEqual((e.written, e.not_written), (first, 120 - first))
        self.assertEqual(names(google), [p.name for p, _, _ in records[:first]])
        self.assertEqual(self.out.getvalue(), f"sheets: appended {first} row(s), {120 - first} "
                                              "not written (server_error), skipped 0 "
                                              "duplicate(s)\n")
        self.assertEqual(google.kinds()[-1], "append-failed")

    def test_rerun_after_a_partial_write_skips_written_rows_and_writes_the_rest(self):
        records = typical_records(120)
        google = FailOnAppend(2, header=COLUMNS)
        self.raises("server_error", self.writer(google).write, records)
        first = len(google.rows)
        google.fail_at = 0
        result = self.writer(google).write(records)
        self.assertEqual(result, WriteResult(120 - first, first, False))
        self.assertEqual(names(google), [p.name for p, _, _ in records])
        self.assertEqual(len({r[1] for r in google.rows}), 120)

    def test_budget_running_out_part_way_keeps_earlier_appends(self):
        google = FakeGoogle(header=COLUMNS)
        e = self.raises("budget_exhausted", self.writer(google, budget=CallBudget(4)).write,
                        typical_records(120))
        self.assertEqual(len(appends(google)), 1)
        self.assertEqual((e.written, e.not_written), (len(google.rows), 120 - len(google.rows)))

    def test_single_oversize_row_raises_row_too_large_and_keeps_earlier_appends(self):
        before = typical_records(60)
        records = before + [oversize_record()] + typical_records(10, start=500)
        budget = CallBudget(50)
        google = FakeGoogle(header=COLUMNS)
        e = self.raises("row_too_large", self.writer(google, budget=budget).write, records)
        self.assertIsInstance(e, AppendStopped)
        self.assertEqual(names(google), [p.name for p, _, _ in before])
        self.assertEqual((e.written, e.not_written), (60, 11))
        n = len(appends(google))
        self.assertGreaterEqual(n, 2)
        self.assertEqual(budget.used, 3 + n)  # nothing charged for the oversize row
        for call in appends(google):
            self.assertLessEqual(len(call["body"]), APPEND_CHUNK_BYTES)

    def test_oversize_row_first_writes_nothing_and_charges_no_append(self):
        budget = CallBudget(50)
        google = FakeGoogle(header=COLUMNS)
        e = self.raises("row_too_large", self.writer(google, budget=budget).write,
                        [oversize_record()] + typical_records(3))
        self.assertEqual((e.written, e.not_written), (0, 4))
        self.assertEqual(google.kinds(), ["token", "header", "rows"])
        self.assertEqual(budget.used, 3)


class RefusedBody(sheets_fx.LiveCase):
    def test_oversize_body_is_not_charged_and_not_sent(self):
        google = FakeGoogle(header=COLUMNS)
        budget = CallBudget(20)
        http = build_http(transport=google)
        auth = ServiceAccountAuth(self.creds.path, http=http, budget=budget,
                                  signer=sheets_fx.FakeSigner())
        client = SheetsClient(sheets_fx.SHEET, auth=auth, budget=budget, http=http)
        row = ["x" * 7000] * len(COLUMNS)
        self.assertGreater(json_body_size({"values": [row]}), MAX_REQUEST_BYTES)
        self.raises("payload_too_large", client.append_rows, [row])
        self.assertEqual(budget.used, 0)
        self.assertEqual(google.calls, [])
        client.append_rows([["a"] * len(COLUMNS)])  # a normal body still goes out
        self.assertEqual(google.kinds(), ["token", "append"])
        self.assertEqual(budget.used, 2)

    def test_the_client_measures_with_json_body_size(self):
        payload = {"values": [["é" * 10]]}
        self.assertEqual(json_body_size(payload), len(json.dumps(payload, separators=(",", ":"))))


class RunReport(run_fx.Case):
    def test_a_stopped_append_prints_counts_and_ends_partial(self):
        google = FailOnAppend(1, header=COLUMNS)
        code = self.live(self.env(), google=google)
        self.assertEqual(code, 3)
        self.assertIn("sheet: stopped (server_error): 0 row(s) written, 6 not written", self.text)
        self.assertIn("run: partial (sheet: server_error)", self.text)


class Docs(unittest.TestCase):
    def test_troubleshooting_and_sheet_docs_name_both_codes_and_the_read_limit(self):
        trouble = (ROOT / "docs/troubleshooting.md").read_text()
        for code in ("payload_too_large", "row_too_large"):
            self.assertIn(f"| `{code}` |", trouble)
        sheets = " ".join((ROOT / "docs/google-sheets.md").read_text().split())
        self.assertIn("`row_too_large`", sheets)
        self.assertIn("roughly 750 to 800 typical rows", sheets)
        self.assertIn("## Appends of at most 60 KB", (ROOT / "docs/google-sheets.md").read_text())


if __name__ == "__main__":
    unittest.main()
