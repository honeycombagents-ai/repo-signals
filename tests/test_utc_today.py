"""'today' is the UTC date. Offline: a fixed clock instant and a changed local timezone."""
import io
import os
import re
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import _src  # noqa: F401
import test_collector_live as gh_fx
import test_run as run_fx
from repo_signals import clock
from repo_signals import run as run_mod
from repo_signals.collector import live
from repo_signals.collector.github_api import ALLOWED_HOSTS
from repo_signals.http import HttpClient

ROOT = Path(__file__).resolve().parent.parent
HAS_TZSET = hasattr(time, "tzset")


def fixed_clock(instant):
    """A datetime subclass whose now() is ``instant`` (aware) or its local wall time (naive)."""

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime.fromtimestamp(instant.timestamp())
            return instant.astimezone(tz)

    return Fixed


class LocalZone:
    """Temporarily set the process's local timezone (TZ plus time.tzset)."""

    def __init__(self, zone):
        self.zone = zone

    def __enter__(self):
        self.old = os.environ.get("TZ")
        os.environ["TZ"] = self.zone
        time.tzset()
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old
        time.tzset()
        return False


# Etc/GMT+12 is 12 hours BEHIND UTC; Etc/GMT-14 is 14 hours AHEAD (POSIX sign convention).
BEHIND, AHEAD = "Etc/GMT+12", "Etc/GMT-14"
UTC_MORNING = datetime(2026, 10, 22, 2, 30, tzinfo=timezone.utc)   # local date behind: Oct 21
UTC_NIGHT = datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc)    # local date ahead: Oct 22


@unittest.skipUnless(HAS_TZSET, "needs time.tzset")
class Helper(unittest.TestCase):
    def test_utc_date_whatever_the_local_zone(self):
        for zone, instant, utc_day, local_day in (
                (BEHIND, UTC_MORNING, date(2026, 10, 22), date(2026, 10, 21)),
                (AHEAD, UTC_NIGHT, date(2026, 10, 21), date(2026, 10, 22))):
            with self.subTest(zone=zone), LocalZone(zone), \
                    mock.patch.object(clock, "datetime", fixed_clock(instant)):
                self.assertEqual(clock.utc_today(), utc_day)
                # the local date really is different here, which is what the helper avoids
                self.assertEqual(clock.datetime.now().date(), local_day)

    def test_real_clock_matches_utc(self):
        for zone in (BEHIND, AHEAD):
            with self.subTest(zone=zone), LocalZone(zone):
                self.assertEqual(clock.utc_today(), datetime.now(timezone.utc).date())


class Defaults(unittest.TestCase):
    def test_run_and_live_use_the_helper(self):
        self.assertIs(run_mod.utc_today, clock.utc_today)
        self.assertIs(live.utc_today, clock.utc_today)

    def test_no_bare_local_dates_in_src(self):
        for path in (ROOT / "src/repo_signals").rglob("*.py"):
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotIn("date.today(", text)
                self.assertNotIn("utcnow(", text)
                self.assertIsNone(re.search(r"datetime\.now\(\s*\)", text))
                self.assertIsNone(re.search(r"\btime\.localtime\(", text))


@unittest.skipUnless(HAS_TZSET, "needs time.tzset")
class NotFutureDated(unittest.TestCase):
    """Local evening, UTC already tomorrow: today's repositories must be kept."""

    INSTANT = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)   # 21:00 on Oct 21 at UTC-8
    ZONE = "Etc/GMT+8"

    def repos(self):
        return {("examplelib", "example-org"): [
            gh_fx.repo("example-org", "examplelib-new", pushed_at="2026-10-22T04:30:00Z"),
            gh_fx.repo("example-org", "examplelib-late", pushed_at="2026-10-21T23:30:00Z")]}

    def test_live_keeps_todays_repositories(self):
        case = gh_fx.Case(methodName="setUp")
        case.setUp()
        self.addCleanup(case.doCleanups)
        transport = gh_fx.FakeGitHub({"example-org": gh_fx.ORG}, self.repos())
        out = gh_fx.TTYOut()
        with LocalZone(self.ZONE), \
                mock.patch.object(clock, "datetime", fixed_clock(self.INSTANT)):
            self.assertEqual(clock.datetime.now().date(), date(2026, 10, 21))  # local: Oct 21
            code = live.run_live(case.env(), stdin=gh_fx.TTYIn("yes\n"), out=out,
                                 err=io.StringIO(),
                                 http=HttpClient(ALLOWED_HOSTS, transport=transport))
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertNotIn("pushed_in_future", text)
        self.assertIn("evidence: example.com: 2", text)
        self.assertIn("examplelib-new", text)

    def test_run_keeps_todays_repositories(self):
        case = run_fx.Case(methodName="setUp")
        case.setUp()
        self.addCleanup(case.doCleanups)
        seen = []
        real_collect = run_mod.collect

        def capture(signal, client, suppression, budget_cap, today):
            seen.append(today)
            return real_collect(signal, client, suppression, budget_cap, today)

        github = gh_fx.FakeGitHub(run_fx.ORGS, {**run_fx.REPOS, **self.repos()})
        with LocalZone(self.ZONE), \
                mock.patch.object(clock, "datetime", fixed_clock(self.INSTANT)), \
                mock.patch.object(run_mod, "collect", side_effect=capture):
            out = gh_fx.TTYOut()
            code = run_mod.run(
                live=True, environ=case.env(), stdin=gh_fx.TTYIn("yes\n"), out=out,
                err=io.StringIO(), github_http=HttpClient(ALLOWED_HOSTS, transport=github),
                sheets_http=run_fx.build_http(transport=run_fx.sheets_fx.FakeGoogle(
                    header=run_fx.COLUMNS)),
                signer=run_fx.sheets_fx.FakeSigner(), clock=run_fx.fixed_clock)
        self.assertEqual(seen, [date(2026, 10, 22)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertNotIn("pushed_in_future", out.getvalue())
        self.assertIn("evidence: example.com: 2", out.getvalue())


if __name__ == "__main__":
    unittest.main()
