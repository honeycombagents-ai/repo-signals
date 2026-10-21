"""Google Sheets auth, client and writer tests. Offline and synthetic only: a fake Signer, a
fake transport standing in for Google, and synthetic credentials with no real key."""
import base64
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import traceback
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import _src  # noqa: F401
from repo_signals.budget import CallBudget
from repo_signals.config import Settings
from repo_signals.http import HttpClient, RawResponse
from repo_signals.models import Person
from repo_signals.providers import ProviderError
from repo_signals.sheets import auth as auth_mod
from repo_signals.sheets import google_api as g
from repo_signals.sheets.auth import (OpenSSLSigner, ServiceAccountAuth, b64url,
                                      inspect_credentials, load_credentials)
from repo_signals.sheets.client import SheetsClient, build_http
from repo_signals.sheets.layout import COLUMNS, LAST_COLUMN
from repo_signals.sheets.writer import SheetWriter, WriteResult, guard_cell, mask_email

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "SENTINELTOKEN.ya29-synthetic"
SHEET = "SENTINELSHEETID_abcdefghij"
SA_EMAIL = "svc-SENTINELSA" + "@" + "example.invalid"
KEY = ("-----BEGIN " + "PRIVATE" + " KEY-----\nSENTINELKEYMATERIAL\n"
       "-----END " + "PRIVATE" + " KEY-----\n")
SIG = b"synthetic-signature-\xff\x00"
DOMAIN = "acme.example.invalid"
FIXED = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
SENTINELS = ("SENTINELTOKEN", "SENTINELSHEETID", "SENTINELSA", "SENTINELKEYMATERIAL",
             "SENTINELPATH")


def addr(local):
    return local + "@" + "example.invalid"


def fixed_clock():
    return FIXED


class FakeSigner:
    def __init__(self, result=SIG):
        self.calls = []
        self.result = result

    def sign(self, message, private_key_pem):
        self.calls.append((message, private_key_pem))
        return self.result


class Creds:
    """A temp dir whose path contains SENTINELPATH, holding a synthetic credentials file."""

    def __init__(self, data=None, raw=None, mode=0o600):
        self.dir = tempfile.TemporaryDirectory(prefix="SENTINELPATH-")
        self.path = os.path.join(self.dir.name, "sa.json")
        if data is None and raw is None:
            data = {"type": "service_account", "client_email": SA_EMAIL, "private_key": KEY,
                    "project_id": "synthetic"}
        with open(self.path, "wb") as fh:
            fh.write(raw if raw is not None else json.dumps(data).encode())
        os.chmod(self.path, mode)

    def close(self):
        self.dir.cleanup()


def everything(e):
    return "".join(traceback.format_exception(e)) + repr(e) + str(e.args)


def assert_no_sentinel(test, text):
    for s in SENTINELS:
        test.assertNotIn(s, text)
    test.assertNotIn(SA_EMAIL, text)


def b64decode(seg):
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


# ---- a fake Google behind the transport ----

ALLOWED_KINDS = ("token", "header", "rows", "put", "append")


def classify(method, url):
    if url == g.TOKEN_URL and method == "POST":
        return "token"
    parts = urlsplit(url)
    path = unquote(parts.path)
    prefix = f"/v4/spreadsheets/{SHEET}/values/'Sheet1'!"
    if parts.netloc != g.SHEETS_HOST or not path.startswith(prefix):
        return "forbidden"
    cells = path[len(prefix):]
    if method == "GET" and cells == "1:1":
        return "header"
    if method == "GET" and cells == f"A2:{LAST_COLUMN}":
        return "rows"
    if method == "PUT" and re.fullmatch(r"[A-Z][0-9]+:[A-Z][0-9]+", cells):
        return "put"
    if method == "POST" and cells == f"A1:{LAST_COLUMN}:append":
        return "append"
    return "forbidden"


class FakeGoogle:
    def __init__(self, header=None, rows=None, fail=None, on_call=None):
        self.header = list(header) if header else []
        self.rows = [list(r) for r in (rows or [])]
        self.fail = dict(fail or {})
        self.on_call = on_call
        self.calls = []

    def kinds(self):
        return [c["kind"] for c in self.calls]

    def __call__(self, url, headers, timeout, max_bytes, method="GET", body=None):
        kind = classify(method, url)
        self.calls.append({"kind": kind, "method": method, "url": url, "headers": dict(headers),
                           "body": body})
        if self.on_call:
            self.on_call(kind)
        if kind in self.fail:
            r = self.fail[kind]
            if isinstance(r, BaseException):
                raise r
            return r
        if kind == "token":
            return ok({"access_token": TOKEN, "expires_in": 3600, "token_type": "Bearer"})
        if kind == "header":
            return ok({"range": "x", "values": [self.header]} if self.header else {"range": "x"})
        if kind == "rows":
            return ok({"range": "x", "values": self.rows} if self.rows else {"range": "x"})
        payload = json.loads(body)
        if kind == "put":
            self.header = payload["values"][0]
            return ok({"updatedRows": 1})
        if kind == "append":
            self.rows.extend(payload["values"])
            return ok({"updates": {"updatedRows": len(payload["values"])}})
        return RawResponse(400, {}, b"{}")


def ok(data, status=200, headers=None):
    return RawResponse(status, headers or {}, json.dumps(data).encode())


def person(name, email="default", title="CTO", domain=DOMAIN, source="fake", confidence=0.8):
    em = addr(name.lower().replace(" ", ".")) if email == "default" else email
    return Person(name=name, email=em, title=title, company_domain=domain, source=source,
                  confidence=confidence)


PEOPLE = [(person("Avery Alder"), "pushed 12 commits to example/repo"),
          (person("Blake Birch", title="VP Engineering"), "opened 3 issues"),
          (person("Casey Cedar", email=None), "starred example/repo"),
          (person("Devon Hazel"), ""), (person("Emery Maple"), "forked example/repo")]


class LiveCase(unittest.TestCase):
    def setUp(self):
        self.creds = Creds()
        self.addCleanup(self.creds.close)

    def writer(self, google, budget=None, settings=None, out=None, **kw):
        settings = settings or Settings(dry_run=False, sheet_id=SHEET,
                                        google_credentials=self.creds.path)
        self.out = out if out is not None else io.StringIO()
        self.budget = budget if budget is not None else CallBudget(50)
        return SheetWriter(settings, budget=self.budget, http=build_http(transport=google),
                           signer=FakeSigner(), out=self.out, clock=fixed_clock, run_id="run-1",
                           **kw)

    def raises(self, code, fn, *args):
        with self.assertRaises(ProviderError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.code, code)
        assert_no_sentinel(self, everything(cm.exception))
        return cm.exception

    def assertOnlyAllowed(self, google):
        for c in google.calls:
            self.assertIn(c["kind"], ALLOWED_KINDS, c["method"])
            self.assertIn(c["method"], ("GET", "POST", "PUT"))
            low = unquote(c["url"]).lower()
            for bad in (":clear", "batchclear", "batchupdate", "delete"):
                self.assertNotIn(bad, low)


# ---- auth ----

class Jwt(LiveCase):
    def auth(self, google=None, signer=None, budget=None, clock=None):
        self.google = google or FakeGoogle()
        self.signer = signer or FakeSigner()
        return ServiceAccountAuth(self.creds.path, http=build_http(transport=self.google),
                                  budget=budget or CallBudget(10), signer=self.signer,
                                  clock=clock or const_clock(1000))

    def test_well_formed_jwt(self):
        jwt = self.auth().build_assertion()
        segs = jwt.split(".")
        self.assertEqual(len(segs), 3)
        for seg in segs:
            self.assertRegex(seg, r"\A[A-Za-z0-9_-]+\Z")
            self.assertNotIn("=", seg)
        self.assertEqual(json.loads(b64decode(segs[0])), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64decode(segs[1])), {
            "iss": SA_EMAIL, "scope": "https://www.googleapis.com/auth/spreadsheets",
            "aud": g.TOKEN_URL, "iat": 1000, "exp": 4600})
        self.assertEqual(b64decode(segs[2]), SIG)
        message, key = self.signer.calls[0]
        self.assertEqual(message, (segs[0] + "." + segs[1]).encode())
        self.assertEqual(key, KEY)

    def test_b64url_has_no_padding(self):
        for n in range(1, 8):
            with self.subTest(n=n):
                enc = b64url(b"\xfb\xff" * n)
                self.assertNotIn("=", enc)
                self.assertNotIn("+", enc)
                self.assertNotIn("/", enc)
                self.assertEqual(b64decode(enc), b"\xfb\xff" * n)

    def test_bad_signature_refused(self):
        for result in (b"", "text", None):
            with self.subTest(result=result):
                self.raises("signing_failed", self.auth(signer=FakeSigner(result)).build_assertion)

    def test_token_exchange(self):
        a = self.auth()
        self.assertEqual(a.token(), TOKEN)
        call = self.google.calls[0]
        self.assertEqual((call["kind"], call["method"]), ("token", "POST"))
        self.assertEqual(call["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        form = parse_qs(call["body"].decode())
        self.assertEqual(form["grant_type"], ["urn:ietf:params:oauth:grant-type:jwt-bearer"])
        self.assertEqual(len(form["assertion"][0].split(".")), 3)
        self.assertNotIn(TOKEN, repr(a))
        self.assertNotIn("SENTINEL", repr(a) + str(a))

    def test_token_cached_in_memory_until_near_expiry(self):
        now = [1000]

        def clock():
            return now[0]

        a = self.auth(clock=clock)
        a.token()
        now[0] = 1000 + 3600 - 61
        a.token()
        self.assertEqual(len(self.google.calls), 1)
        now[0] = 1000 + 3600 - 59
        a.token()
        self.assertEqual(len(self.google.calls), 2)

    def test_budget_charged_before_token_request(self):
        seen = []
        budget = CallBudget(1)

        def record(kind):
            seen.append(budget.used)

        a = self.auth(google=FakeGoogle(on_call=record), budget=budget)
        a.token()
        self.assertEqual(seen, [1])
        b = CallBudget(1)
        b.consume(1)
        a = self.auth(budget=b)
        self.raises("budget_exhausted", a.token)
        self.assertEqual(self.google.calls, [])

    def test_token_errors(self):
        cases = [(RawResponse(400, {}, b'{"error": "invalid_grant SENTINELKEYMATERIAL"}'),
                  "token_rejected"),
                 (RawResponse(401, {}, b"{}"), "auth_failed"),
                 (RawResponse(403, {}, b"<html>SENTINELTOKEN</html>"), "auth_failed"),
                 (RawResponse(429, {"Retry-After": "9"}, b"{}"), "retry_after_9"),
                 (RawResponse(429, {}, b"{}"), "rate_limited"),
                 (RawResponse(503, {}, b"{}"), "server_error"),
                 (RawResponse(200, {}, b"{SENTINELTOKEN"), "malformed_json"),
                 (ok({"token_type": "Bearer"}), "unexpected_shape"),
                 (ok({"access_token": "has space"}), "unexpected_shape"),
                 (ok({"access_token": TOKEN, "expires_in": "3600"}), "unexpected_shape"),
                 (ok({"access_token": TOKEN, "expires_in": True}), "unexpected_shape"),
                 (ok({"access_token": TOKEN, "expires_in": 0}), "unexpected_shape"),
                 (ok([TOKEN]), "unexpected_shape"),
                 (ProviderError("timeout"), "timeout")]
        for response, code in cases:
            with self.subTest(code=code):
                a = self.auth(google=FakeGoogle(fail={"token": response}))
                self.raises(code, a.token)

    def test_token_lives_in_memory_only(self):
        a = self.auth()
        before = set(os.listdir(self.creds.dir.name))
        a.token()
        self.assertEqual(set(os.listdir(self.creds.dir.name)), before)
        with open(self.creds.path) as fh:
            self.assertNotIn(TOKEN, fh.read())


def const_clock(value):
    def clock():
        return value
    return clock


class Credentials(unittest.TestCase):
    def check_code(self, code, creds_or_path):
        path = creds_or_path.path if isinstance(creds_or_path, Creds) else creds_or_path
        with self.assertRaises(ProviderError) as cm:
            load_credentials(path)
        self.assertEqual(cm.exception.code, code)
        assert_no_sentinel(self, everything(cm.exception))

    def test_good(self):
        c = Creds()
        self.addCleanup(c.close)
        self.assertEqual(load_credentials(c.path), (SA_EMAIL, KEY))
        self.assertEqual(inspect_credentials(c.path), (None, (), False))

    def test_missing_and_unreadable(self):
        self.check_code("credentials_missing", None)
        self.check_code("credentials_missing", "")
        c = Creds()
        self.addCleanup(c.close)
        self.check_code("credentials_unreadable", c.path + ".nope")
        self.check_code("credentials_unreadable", c.dir.name)
        self.assertEqual(inspect_credentials(c.path + ".nope").problem, "unreadable")

    def test_malformed(self):
        good = {"type": "service_account", "client_email": SA_EMAIL, "private_key": KEY}
        cases = [(None, b"{SENTINELKEYMATERIAL", "not_json"), (None, b"[1, 2]", "not_json"),
                 (None, b"\xff\xfe", "not_json"),
                 (None, b" " * (auth_mod.MAX_CREDENTIALS_BYTES + 1), "too_large"),
                 ({"type": "service_account"}, None, "missing_keys"),
                 ({**good, "client_email": ""}, None, "missing_keys"),
                 ({**good, "type": "authorized_user"}, None, "wrong_type"),
                 ({**good, "private_key": "SENTINELKEYMATERIAL"}, None, None),
                 ({**good, "client_email": "no-at"}, None, None)]
        for data, raw, problem in cases:
            with self.subTest(problem=problem, raw=raw[:10] if raw else None):
                c = Creds(data=data, raw=raw)
                self.addCleanup(c.close)
                self.check_code("credentials_malformed", c)
                self.assertEqual(inspect_credentials(c.path).problem, problem)

    def test_missing_key_names_and_permissions(self):
        c = Creds(data={"type": "service_account"}, mode=0o644)
        self.addCleanup(c.close)
        check = inspect_credentials(c.path)
        self.assertEqual(check.missing_keys, ("client_email", "private_key"))
        self.assertTrue(check.loose_permissions)


# ---- the openssl signer (subprocess mocked: openssl never runs) ----

class FakeRun:
    def __init__(self, returncode=0, stdout=b"sig", raises=None):
        self.returncode, self.stdout, self.raises = returncode, stdout, raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        path = argv[-1]
        info = {"argv": list(argv), "kwargs": kwargs, "exists": os.path.exists(path)}
        if info["exists"]:
            info["mode"] = stat.S_IMODE(os.stat(path).st_mode)
            with open(path) as fh:
                info["content"] = fh.read()
        self.calls.append(info)
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, None)


def which_found(name):
    return "/opt/synthetic/bin/" + name


def which_missing(name):
    return None


class OpenSSL(unittest.TestCase):
    def test_argv_never_holds_the_key(self):
        run = FakeRun(stdout=b"\x01\x02sig")
        sig = OpenSSLSigner(which=which_found, run=run).sign(b"header.claims", KEY)
        self.assertEqual(sig, b"\x01\x02sig")
        call = run.calls[0]
        path = call["argv"][-1]
        self.assertEqual(call["argv"], ["/opt/synthetic/bin/openssl", "dgst", "-sha256", "-sign",
                                        path])
        for arg in call["argv"]:
            self.assertNotIn("SENTINELKEYMATERIAL", arg)
            self.assertNotIn("PRIVATE", arg)
        self.assertEqual(call["kwargs"]["input"], b"header.claims")
        self.assertIs(call["kwargs"].get("shell"), False)
        self.assertNotIn("SENTINELKEYMATERIAL", repr(call["kwargs"]))
        self.assertTrue(call["exists"])
        self.assertEqual(call["mode"], 0o600)
        self.assertEqual(call["content"], KEY)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(call["kwargs"]["stderr"], subprocess.DEVNULL)

    def test_temp_file_removed_on_every_error(self):
        cases = [(FakeRun(raises=subprocess.TimeoutExpired(["openssl"], 1)), "signing_timeout"),
                 (FakeRun(raises=OSError("SENTINELKEYMATERIAL")), "signing_failed"),
                 (FakeRun(returncode=1, stdout=b""), "signing_failed"),
                 (FakeRun(returncode=0, stdout=b""), "signing_failed"),
                 (FakeRun(raises=RuntimeError("boom")), None)]
        for run, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(Exception) as cm:
                    OpenSSLSigner(which=which_found, run=run).sign(b"m", KEY)
                if code:
                    self.assertIsInstance(cm.exception, ProviderError)
                    self.assertEqual(cm.exception.code, code)
                    self.assertNotIn("SENTINELKEYMATERIAL", everything(cm.exception))
                path = run.calls[0]["argv"][-1]
                self.assertTrue(run.calls[0]["exists"])
                self.assertFalse(os.path.exists(path))

    def test_temp_file_removed_when_writing_fails(self):
        made = []
        real_mkstemp = tempfile.mkstemp

        def mkstemp(*a, **kw):
            fd, path = real_mkstemp(*a, **kw)
            made.append(path)
            return fd, path

        run = FakeRun()
        with mock.patch("tempfile.mkstemp", mkstemp), \
                mock.patch("os.write", side_effect=OSError("disk full")):
            with self.assertRaises(ProviderError) as cm:
                OpenSSLSigner(which=which_found, run=run).sign(b"m", KEY)
        self.assertEqual(cm.exception.code, "signing_failed")
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))
        self.assertEqual(run.calls, [])

    def test_openssl_missing(self):
        run = FakeRun()
        with mock.patch("tempfile.mkstemp", side_effect=AssertionError("no temp file")):
            with self.assertRaises(ProviderError) as cm:
                OpenSSLSigner(which=which_missing, run=run).sign(b"m", KEY)
        self.assertEqual((cm.exception.code, run.calls), ("openssl_not_found", []))

    def test_bad_inputs(self):
        for args in ((b"m", ""), ("m", KEY), (b"m", None)):
            with self.subTest(args=args), self.assertRaises(ProviderError):
                OpenSSLSigner(which=which_found, run=FakeRun()).sign(*args)

    def test_default_is_the_openssl_signer(self):
        a = ServiceAccountAuth("x", http=build_http(), budget=CallBudget(1))
        self.assertIsInstance(a._signer, OpenSSLSigner)


# ---- the client ----

class Client(LiveCase):
    def client(self, google, budget=None):
        self.budget = budget if budget is not None else CallBudget(20)
        http = build_http(transport=google)
        a = ServiceAccountAuth(self.creds.path, http=http, budget=self.budget,
                               signer=FakeSigner())
        return SheetsClient(SHEET, auth=a, budget=self.budget, http=http)

    def test_exposes_no_delete_or_clear(self):
        for cls in (SheetsClient, HttpClient, SheetWriter):
            names = [n for n in dir(cls) if not n.startswith("__")]
            for n in names:
                for bad in ("delete", "clear", "remove", "drop", "batch", "truncate"):
                    self.assertNotIn(bad, n.lower(), f"{cls.__name__}.{n}")
        public = {n for n in dir(SheetsClient) if not n.startswith("_")}
        self.assertEqual(public, {"read_header", "read_rows", "append_rows", "update_cells"})
        for name in ("client.py", "writer.py", "google_api.py", "auth.py"):
            src = (ROOT / "src/repo_signals/sheets" / name).read_text()
            for bad in (":clear", "batchUpdate", "batchClear", "deleteDimension", "deleteSheet",
                        '"DELETE"', '"PATCH"'):
                self.assertNotIn(bad, src, name)

    def test_internal_call_refuses_other_methods(self):
        google = FakeGoogle()
        c = self.client(google)
        for method in ("DELETE", "PATCH", "CLEAR"):
            with self.subTest(method=method):
                self.raises("method_not_allowed", c._call, method, c._url("A1:K1"))
        self.assertEqual([k for k in google.kinds() if k != "token"], [])

    def test_allowlist_is_exactly_the_two_google_hosts(self):
        expected = frozenset({"sheets.googleapis.com", "oauth2.googleapis.com"})
        self.assertEqual(g.ALLOWED_HOSTS, expected)
        self.assertEqual(build_http().allowed_hosts, expected)
        with self.assertRaises(ValueError):
            SheetWriter(Settings(), http=HttpClient(("sheets.googleapis.com", "evil.example.com")))

    def test_requests_and_headers(self):
        google = FakeGoogle(header=COLUMNS, rows=[["a"]])
        c = self.client(google)
        self.assertEqual(c.read_header(), list(COLUMNS))
        self.assertEqual(c.read_rows(), [["a"]])
        c.append_rows([["x"] * len(COLUMNS)])
        c.update_cells(f"A1:{LAST_COLUMN}1", [list(COLUMNS)])
        self.assertEqual(google.kinds(), ["token", "header", "rows", "append", "put"])
        self.assertOnlyAllowed(google)
        for call in google.calls[1:]:
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertNotIn(TOKEN, call["url"])
        append = google.calls[3]
        self.assertEqual(parse_qs(urlsplit(append["url"]).query),
                         {"valueInputOption": ["RAW"], "insertDataOption": ["INSERT_ROWS"]})
        put = google.calls[4]
        self.assertEqual(parse_qs(urlsplit(put["url"]).query), {"valueInputOption": ["RAW"]})

    def test_header_trailing_blanks_dropped(self):
        c = self.client(FakeGoogle(header=["name", "email", "", " "]))
        self.assertEqual(c.read_header(), ["name", "email"])
        self.assertEqual(self.client(FakeGoogle()).read_header(), [])

    def test_update_cells_guards(self):
        google = FakeGoogle()
        c = self.client(google)
        for cells in ("H2:H2", "A2:K2", "I5:I9", "G2:J3", "A1:K2"):
            with self.subTest(cells=cells):
                width = ord(cells[3]) - ord(cells[0]) + 1
                height = int(cells.split(":")[1][1:]) - int(cells.split(":")[0][1:]) + 1
                self.raises("protected_cells", c.update_cells, cells, [["x"] * width] * height)
        for cells in ("L1:L1", "A1:Z1", "K1:A1", "A2:A1", "A1", "1:1", None, "a1:b1"):
            with self.subTest(cells=cells):
                self.raises("bad_range", c.update_cells, cells, [["x"]])
        self.raises("bad_rows", c.update_cells, "A1:B1", [["x"]])
        self.raises("bad_rows", c.append_rows, [["x"]])
        self.raises("bad_rows", c.append_rows, [])
        self.raises("bad_rows", c.append_rows, [[True] * len(COLUMNS)])
        self.assertEqual(google.calls, [])

    def test_error_mapping(self):
        cases = [(401, {}, "auth_failed"), (403, {}, "auth_failed"), (404, {}, "sheet_not_found"),
                 (429, {"Retry-After": "12"}, "retry_after_12"),
                 (429, {"Retry-After": "61"}, "rate_limited"), (429, {}, "rate_limited"),
                 (500, {}, "server_error"), (503, {}, "server_error"), (400, {}, "bad_request"),
                 (418, {}, "unexpected_status")]
        body = json.dumps({"error": {"message": f"SENTINELSHEETID {SHEET} {TOKEN}"}}).encode()
        for status, headers, code in cases:
            with self.subTest(status=status, headers=headers):
                c = self.client(FakeGoogle(fail={"header": RawResponse(status, headers, body)}))
                self.raises(code, c.read_header)
        for response, code in ((RawResponse(200, {}, b"{" + SHEET.encode()), "malformed_json"),
                               (ok([1]), "unexpected_shape"),
                               (ok({"values": "x"}), "unexpected_shape"),
                               (ok({"values": [["a", {"b": 1}]]}), "unexpected_shape"),
                               (ProviderError("timeout"), "timeout"),
                               (OSError(f"https://x/{SHEET}?{TOKEN}"), "network_error")):
            with self.subTest(code=code):
                c = self.client(FakeGoogle(fail={"rows": response}))
                self.raises(code, c.read_rows)

    def test_budget_before_each_call_and_cap(self):
        seen = []
        budget = CallBudget(20)

        def record(kind):
            seen.append((kind, budget.used))

        c = self.client(FakeGoogle(header=COLUMNS, on_call=record), budget=budget)
        c.read_header()
        c.read_rows()
        c.append_rows([["x"] * len(COLUMNS)])
        self.assertEqual(seen, [("token", 1), ("header", 2), ("rows", 3), ("append", 4)])
        google = FakeGoogle(header=COLUMNS)
        c = self.client(google, budget=CallBudget(2))
        c.read_header()
        self.raises("budget_exhausted", c.read_rows)
        self.assertEqual(google.kinds(), ["token", "header"])

    def test_constructor_validation(self):
        a = ServiceAccountAuth("x", http=build_http(), budget=CallBudget(1))
        for kw in ({"sheet_id": "short"}, {"sheet_id": SHEET, "tab": "Bad!Tab"},
                   {"sheet_id": SHEET, "tab": ""}):
            with self.subTest(kw=kw), self.assertRaises(ValueError) as cm:
                SheetsClient(**kw, auth=a, budget=CallBudget(1))
            self.assertNotIn("SENTINEL", str(cm.exception))
        with self.assertRaises(TypeError):
            SheetsClient(SHEET, auth=a, budget=5)
        self.assertNotIn("SENTINEL", repr(SheetsClient(SHEET, auth=a, budget=CallBudget(1))))


# ---- the writer ----

def rows_of(google):
    return [dict(zip(COLUMNS, r)) for r in google.rows]


class WriterHeaders(LiveCase):
    def test_empty_sheet_gets_headers_then_rows(self):
        google = FakeGoogle()
        result = self.writer(google).write(PEOPLE)
        self.assertEqual(result, WriteResult(5, 0, False))
        self.assertEqual(google.kinds(), ["token", "header", "rows", "put", "append"])
        self.assertEqual(google.header, list(COLUMNS))
        put = json.loads(google.calls[3]["body"])
        self.assertEqual(put["values"], [list(COLUMNS)])
        self.assertTrue(unquote(google.calls[3]["url"]).split("?")[0].endswith(
            f"!A1:{LAST_COLUMN}1"))
        self.assertOnlyAllowed(google)
        self.assertEqual(self.out.getvalue(), "sheets: appended 5 row(s), skipped 0 duplicate(s)\n")

    def test_matching_header_is_not_rewritten(self):
        google = FakeGoogle(header=COLUMNS)
        self.writer(google).write(PEOPLE[:2])
        self.assertEqual(google.kinds(), ["token", "header", "rows", "append"])

    def test_extra_columns_after_ours_are_allowed(self):
        google = FakeGoogle(header=list(COLUMNS) + ["my notes"])
        self.assertEqual(self.writer(google).write(PEOPLE[:1]).appended, 1)
        self.assertNotIn("put", google.kinds())

    def test_differing_header_refused_and_untouched(self):
        variants = [["Name"] + list(COLUMNS[1:]), list(COLUMNS[:-1]),
                    list(COLUMNS[1:]) + [COLUMNS[0]], ["order", "customer", "total"],
                    ["SENTINELSHEETID private"]]
        for header in variants:
            with self.subTest(header=header[:2]):
                google = FakeGoogle(header=header, rows=[["their", "data"]])
                self.raises("header_mismatch", self.writer(google).write, PEOPLE)
                self.assertEqual(google.kinds(), ["token", "header"])
                self.assertEqual(google.header, header)
                self.assertEqual(google.rows, [["their", "data"]])

    def test_data_without_header_refused(self):
        google = FakeGoogle(rows=[["", "someone's data"]])
        self.raises("header_missing", self.writer(google).write, PEOPLE)
        self.assertEqual(google.kinds(), ["token", "header", "rows"])
        blank = FakeGoogle(rows=[["", " "]])
        self.assertEqual(self.writer(blank).write(PEOPLE[:1]).appended, 1)


class WriterRows(LiveCase):
    def test_row_layout(self):
        google = FakeGoogle(header=COLUMNS)
        self.writer(google).write(PEOPLE[:1])
        appended = json.loads(google.calls[-1]["body"])["values"]
        self.assertEqual(appended, [["Avery Alder", addr("avery.alder"), "CTO", DOMAIN, "fake",
                                     0.8, "pushed 12 commits to example/repo", "", "", "run-1",
                                     "2026-10-21T12:00:00Z"]])

    def test_duplicates_skipped_never_overwritten(self):
        existing = [["Avery Alder", addr("AVERY.ALDER"), "CTO", DOMAIN, "fake", "0.8", "x",
                     "Hi Avery, (human draft)", "yes", "old", "2026-01-01T00:00:00Z"],
                    ["Casey Cedar", "", "CTO", DOMAIN.upper(), "fake", "0.8", "", "draft", ""]]
        google = FakeGoogle(header=COLUMNS, rows=existing)
        batch = PEOPLE + [(person("Avery Alder", email=addr("other")), "same name, new email"),
                          (person("Blake Birch", title="CTO"), "duplicate inside the batch")]
        result = self.writer(google).write(batch)
        self.assertEqual(result, WriteResult(4, 3, False))
        names = [r["name"] for r in rows_of(google)]
        self.assertEqual(names, ["Avery Alder", "Casey Cedar", "Blake Birch", "Devon Hazel",
                                 "Emery Maple", "Avery Alder"])
        self.assertEqual(google.rows[:2], existing)
        self.assertEqual(google.kinds(), ["token", "header", "rows", "append"])

    def test_all_duplicates_means_no_append(self):
        google = FakeGoogle(header=COLUMNS, rows=[["Avery Alder", addr("avery.alder")]])
        self.assertEqual(self.writer(google).write(PEOPLE[:1]), WriteResult(0, 1, False))
        self.assertEqual(google.kinds(), ["token", "header", "rows"])

    def test_human_edits_to_draft_and_reviewed_are_never_touched(self):
        existing = [["Avery Alder", addr("avery.alder"), "CTO", DOMAIN, "fake", "0.8", "e",
                     "Dear Avery, edited by a human", "TRUE", "r0", "t0"]]
        google = FakeGoogle(header=COLUMNS, rows=existing)
        w = self.writer(google)
        w.write(PEOPLE)
        w.write(PEOPLE + [(person("Avery Alder"), "new evidence")])
        self.assertEqual(google.rows[0], existing[0])
        for call in google.calls:
            self.assertNotEqual(call["kind"], "put")
        for row in rows_of(google)[1:]:
            self.assertEqual((row["email_draft"], row["reviewed"]), ("", ""))
        self.assertEqual(len(google.rows), 5)

    def test_formula_guard_on_every_external_field(self):
        evil = Person(name="=HYPERLINK(\"https://x.example.com\")", email="=cmd" + "@example.com",
                      title="+SUM(A1:A9)", company_domain="-2+3.example.com", source="@source",
                      confidence=0.5)
        google = FakeGoogle(header=COLUMNS)
        self.writer(google).write([(evil, "  =IMPORTXML(1)")])
        row = rows_of(google)[0]
        for col in ("name", "email", "title", "company_domain", "source", "signal_evidence"):
            with self.subTest(col=col):
                self.assertTrue(row[col].startswith("'"), row[col])
        self.assertEqual(row["signal_evidence"], "'  =IMPORTXML(1)")
        self.assertEqual(row["confidence"], 0.5)

    def test_guard_cell(self):
        for raw, want in (("=1+1", "'=1+1"), ("+cmd", "'+cmd"), ("-cmd", "'-cmd"),
                          ("@SUM(1)", "'@SUM(1)"), ("\t=1", "'\t=1"), ("\r=1", "'\r=1"),
                          (" =x", "' =x"), ("-5", "-5"), ("+1.5", "+1.5"), ("-1e3", "-1e3"),
                          ("-.5", "-.5"), ("CTO", "CTO"), ("", ""), ("a=b", "a=b"),
                          ("'=x", "'=x"), ("--5", "'--5"), ("-5 apples", "'-5 apples")):
            with self.subTest(raw=raw):
                self.assertEqual(guard_cell(raw), want)
        with self.assertRaises(TypeError):
            guard_cell(5)

    def test_bad_records(self):
        google = FakeGoogle(header=COLUMNS)
        for records in ([("not a person", "")], [(person("A B"), None)], [object()]):
            with self.subTest(records=records), self.assertRaises((ValueError, TypeError)):
                self.writer(google).write(records)
        self.assertEqual(google.calls, [])

    def test_bare_person_records_accepted(self):
        google = FakeGoogle(header=COLUMNS)
        self.writer(google).write([person("Avery Alder")])
        self.assertEqual(rows_of(google)[0]["signal_evidence"], "")


class WriterBudget(LiveCase):
    def test_charged_before_every_call(self):
        seen = []
        budget = CallBudget(10)

        def record(kind):
            seen.append((kind, budget.used))

        self.writer(FakeGoogle(on_call=record), budget=budget).write(PEOPLE)
        self.assertEqual(seen, [("token", 1), ("header", 2), ("rows", 3), ("put", 4),
                                ("append", 5)])

    def test_over_the_cap_never_reaches_transport(self):
        for cap, kinds in ((1, ["token"]), (3, ["token", "header", "rows"]),
                           (4, ["token", "header", "rows", "put"])):
            with self.subTest(cap=cap):
                google = FakeGoogle()
                self.raises("budget_exhausted", self.writer(google, budget=CallBudget(cap)).write,
                            PEOPLE)
                self.assertEqual(google.kinds(), kinds)
                self.assertEqual(google.rows, [])


class DryRun(LiveCase):
    def dry(self, google=None, settings=None, **kw):
        self.google = google or FakeGoogle()
        self.out = io.StringIO()
        return SheetWriter(settings or Settings(), http=build_http(transport=self.google),
                           out=self.out, clock=fixed_clock, run_id="run-1", **kw)

    def test_zero_transport_calls_and_masked_emails(self):
        settings = Settings(sheet_id=SHEET, google_credentials=self.creds.path)  # dry run on
        w = self.dry(settings=settings, budget=CallBudget(5), signer=FakeSigner())
        result = w.write(PEOPLE + [(person("Avery Alder"), "dup in batch")])
        self.assertEqual(result, WriteResult(0, 1, True))
        self.assertEqual(self.google.calls, [])
        out = self.out.getvalue().splitlines()
        self.assertEqual(out[0], "sheets: dry run, would write 5 row(s) (1 duplicate(s) in this "
                                 "batch skipped; the sheet itself is not read in a dry run); "
                                 "nothing written")
        self.assertEqual(out[1], "name | email | title | company_domain | source | confidence | "
                                 "signal_evidence")
        self.assertEqual(out[2], "Avery Alder | a***@example.invalid | CTO | " + DOMAIN +
                         " | fake | 0.8 | pushed 12 commits to example/repo")
        self.assertIn(" | b***@example.invalid | ", out[3])
        self.assertTrue(out[4].startswith("Casey Cedar |  | CTO"))
        self.assertEqual(out[5], "... and 2 more row(s)")
        self.assertEqual(len(out), 6)
        text = self.out.getvalue()
        for p, _ in PEOPLE:
            if p.email:
                self.assertNotIn(p.email, text)
        assert_no_sentinel(self, text)
        self.assertNotIn(self.creds.path, text)

    def test_dry_run_needs_nothing_and_builds_nothing(self):
        w = SheetWriter(Settings(), out=io.StringIO())
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=AssertionError("network")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket")):
            self.assertTrue(w.write(PEOPLE).dry_run)
        self.assertIsNone(w._client)

    def test_dry_run_output_is_sanitized(self):
        w = self.dry()
        w.write([(person("Avery Alder"), "line\x1b[31mred\x07" + "x" * 80)])
        self.assertNotIn("\x1b", self.out.getvalue())
        self.assertNotIn("\x07", self.out.getvalue())
        self.assertIn("...", self.out.getvalue())

    def test_empty_batch(self):
        w = self.dry()
        w.write([])
        self.assertEqual(self.out.getvalue().count("\n"), 1)

    def test_mask_email(self):
        for raw, want in ((addr("jo"), "j***@example.invalid"), ("", ""), (None, ""),
                          ("noat", "***"), ("@example.invalid", "***"), ("x@", "***")):
            with self.subTest(raw=raw):
                self.assertEqual(mask_email(raw), want)

    def test_stdout_is_the_default(self):
        w = SheetWriter(Settings())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            w.write(PEOPLE[:1])
        self.assertIn("a***@example.invalid", buf.getvalue())


class LivePreconditions(LiveCase):
    def test_codes_and_zero_calls(self):
        cases = [(Settings(dry_run=False, google_credentials=self.creds.path), "sheet_id_missing"),
                 (Settings(dry_run=False, sheet_id=SHEET), "credentials_missing")]
        for settings, code in cases:
            with self.subTest(code=code):
                google = FakeGoogle()
                self.raises(code, self.writer(google, settings=settings).write, PEOPLE)
                self.assertEqual(google.calls, [])
        google = FakeGoogle()
        w = SheetWriter(Settings(dry_run=False, sheet_id=SHEET,
                                 google_credentials=self.creds.path),
                        http=build_http(transport=google), signer=FakeSigner(),
                        out=io.StringIO())
        self.raises("budget_missing", w.write, PEOPLE)
        self.assertEqual(google.calls, [])

    def test_bad_constructor_args(self):
        with self.assertRaises(TypeError):
            SheetWriter({"dry_run": False})
        with self.assertRaises(TypeError):
            SheetWriter(Settings(), budget=5)
        for run_id in ("", "has space", "=x", "x" * 65):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                SheetWriter(Settings(), run_id=run_id)
        self.assertRegex(SheetWriter(Settings()).run_id, r"\A[0-9a-f]{12}\Z")


class NoSecretsAnywhere(LiveCase):
    def test_live_write_prints_no_secret(self):
        google = FakeGoogle()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            w = self.writer(google, out=out)
            w.write(PEOPLE)
            text = out.getvalue() + err.getvalue() + repr(w) + repr(w._client) \
                + repr(w._client._auth)
        assert_no_sentinel(self, text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(self.creds.path, text)

    def test_no_secret_in_any_failure(self):
        body = json.dumps({"error": f"{SHEET} {TOKEN} {SA_EMAIL} SENTINELPATH"}).encode()
        for kind in ALLOWED_KINDS:
            for status in (400, 401, 403, 404, 429, 500):
                with self.subTest(kind=kind, status=status):
                    google = FakeGoogle(fail={kind: RawResponse(status, {}, body)})
                    with self.assertRaises(ProviderError) as cm:
                        self.writer(google).write(PEOPLE)
                    text = everything(cm.exception) + self.out.getvalue()
                    assert_no_sentinel(self, text)
                    self.assertNotIn(self.creds.path, text)

    def test_missing_credentials_file_error_holds_no_path(self):
        settings = Settings(dry_run=False, sheet_id=SHEET,
                            google_credentials=self.creds.path + ".missing")
        e = self.raises("credentials_unreadable", self.writer(FakeGoogle(), settings=settings).write,
                        PEOPLE)
        self.assertIsNone(e.__cause__)
        self.assertNotIn(self.creds.path, everything(e))


class Source(unittest.TestCase):
    NEW_FILES = ("src/repo_signals/sheets/__init__.py", "src/repo_signals/sheets/auth.py",
                 "src/repo_signals/sheets/client.py", "src/repo_signals/sheets/errors.py",
                 "src/repo_signals/sheets/google_api.py", "src/repo_signals/sheets/layout.py",
                 "src/repo_signals/sheets/writer.py", "src/repo_signals/http.py",
                 "src/repo_signals/config.py", "src/repo_signals/doctor.py",
                 "tests/test_sheets.py", "tests/test_sheets_doctor.py", "tests/test_http.py",
                 "docs/google-sheets.md")

    def test_forbidden_keyword_absent(self):
        word = re.compile(r"\b" + "lam" + "bda" + r"\b", re.IGNORECASE)
        for name in self.NEW_FILES:
            with self.subTest(file=name):
                self.assertIsNone(word.search((ROOT / name).read_text()))

    def test_unverified_note_on_the_one_constants_block(self):
        text = (ROOT / "src/repo_signals/sheets/google_api.py").read_text()
        self.assertIn("verify against Google's current documentation before first live use", text)
        self.assertEqual(text.count("# ---"), 2)
        for name in ("auth.py", "client.py"):
            src = (ROOT / "src/repo_signals/sheets" / name).read_text()
            self.assertNotIn("googleapis.com", src, name)

    def test_writer_exports_the_layout(self):
        from repo_signals.sheets import writer
        self.assertEqual(writer.COLUMNS, (
            "name", "email", "title", "company_domain", "source", "confidence",
            "signal_evidence", "email_draft", "reviewed", "run_id", "written_at"))


if __name__ == "__main__":
    unittest.main()
