"""Google service-account auth with the standard library only.

The service-account JSON key is read from the FILE named by REPO_SIGNALS_GOOGLE_CREDENTIALS
(never from JSON in an environment variable). A JWT for the spreadsheets scope is signed RS256
and exchanged for an access token, which lives in memory only.

The standard library has no RSA signer, so signing sits behind the ``Signer`` interface: any
object with ``sign(message: bytes, private_key_pem: str) -> bytes`` returning an RS256
(RSASSA-PKCS1-v1_5 with SHA-256) signature. ``OpenSSLSigner`` calls the system ``openssl``.
It writes the key to a 0600 temp file that is deleted in a ``finally``, so the key is never on
the command line or in a log. You may supply your own Signer instead.

Google endpoint, claim and field names live in ``google_api.py`` (one constants block,
unverified).
"""
import base64
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from typing import NamedTuple, Protocol

from ..budget import BudgetExceeded, CallBudget
from ..providers.base import ProviderError
from . import google_api as g
from .errors import status_code

MAX_CREDENTIALS_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
SIGN_TIMEOUT_SECONDS = 10.0
TOKEN_REFRESH_MARGIN_SECONDS = 60
MAX_TOKEN_LENGTH = 4096


class Signer(Protocol):
    def sign(self, message: bytes, private_key_pem: str) -> bytes:
        ...


def b64url(data: bytes) -> str:
    """base64url without padding, as JWTs use."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class OpenSSLSigner:
    """Signs with ``openssl dgst -sha256 -sign <0600 temp key file>``; the message goes on stdin.

    ``which`` and ``run`` are injectable so tests never run openssl.
    """

    def __init__(self, *, which=shutil.which, run=subprocess.run,
                 timeout=SIGN_TIMEOUT_SECONDS):
        self._which = which
        self._run = run
        self._timeout = timeout

    def __repr__(self):
        return "OpenSSLSigner()"

    def sign(self, message: bytes, private_key_pem: str) -> bytes:
        if not isinstance(message, bytes) or not isinstance(private_key_pem, str) \
                or not private_key_pem:
            raise ProviderError("signing_failed")
        openssl = self._which("openssl")
        if not openssl:
            raise ProviderError("openssl_not_found")
        fd, path = tempfile.mkstemp(prefix="repo-signals-", suffix=".pem")
        try:
            try:
                os.fchmod(fd, 0o600)
                data = private_key_pem.encode("utf-8")
                while data:
                    data = data[os.write(fd, data):]
            except (OSError, UnicodeError):
                raise ProviderError("signing_failed") from None
            finally:
                os.close(fd)
            try:
                result = self._run([openssl, "dgst", "-sha256", "-sign", path], input=message,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=self._timeout, check=False, shell=False)
            except subprocess.TimeoutExpired:
                raise ProviderError("signing_timeout") from None
            except (OSError, ValueError):
                raise ProviderError("signing_failed") from None
            if result.returncode != 0 or not isinstance(result.stdout, bytes) \
                    or not result.stdout:
                raise ProviderError("signing_failed")
            return result.stdout
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


class CredentialsCheck(NamedTuple):
    """The offline result of checking a credentials file. Holds names, never values.

    ``problem`` is None, "unreadable", "too_large", "not_json", "missing_keys", "wrong_type" or
    "malformed_field". ``fields`` names the keys involved (missing or malformed).
    """

    problem: str | None
    fields: tuple[str, ...] = ()
    loose_permissions: bool = False

    @property
    def missing_keys(self) -> tuple[str, ...]:
        return self.fields if self.problem == "missing_keys" else ()

    @property
    def code(self) -> str | None:
        """The ProviderError code the run raises for this problem (None when there is none)."""
        if self.problem is None:
            return None
        return "credentials_unreadable" if self.problem == "unreadable" else \
            "credentials_malformed"


def _read_json(path):
    """(problem, data, loose_permissions)."""
    try:
        with open(path, "rb") as fh:
            mode = os.fstat(fh.fileno()).st_mode
            if not stat.S_ISREG(mode):
                return "unreadable", None, False
            raw = fh.read(MAX_CREDENTIALS_BYTES + 1)
    except (OSError, ValueError, TypeError):
        return "unreadable", None, False
    loose = bool(mode & 0o077)
    if len(raw) > MAX_CREDENTIALS_BYTES:
        return "too_large", None, loose
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "not_json", None, loose
    if not isinstance(data, dict):
        return "not_json", None, loose
    return None, data, loose


def _check(path):
    """``(CredentialsCheck, data)``: the ONE key-file check, shared by ``load_credentials`` (the
    run), ``inspect_credentials`` (doctor) and the run --live gates. Reads the file; never
    signs, runs openssl or touches the network."""
    problem, data, loose = _read_json(path)
    if problem:
        return CredentialsCheck(problem, (), loose), None
    missing = tuple(k for k in g.REQUIRED_CRED_KEYS
                    if not isinstance(data.get(k), str) or not data.get(k).strip())
    if missing:
        return CredentialsCheck("missing_keys", missing, loose), None
    if data[g.CRED_TYPE] != g.CRED_TYPE_SERVICE_ACCOUNT:
        return CredentialsCheck("wrong_type", (), loose), None
    email, key = data[g.CRED_CLIENT_EMAIL], data[g.CRED_PRIVATE_KEY]
    bad = []
    if email.count("@") != 1 or any(c.isspace() for c in email):
        bad.append(g.CRED_CLIENT_EMAIL)
    if not key.lstrip().startswith("-----BEGIN ") or "PRIVATE KEY-----" not in key:
        bad.append(g.CRED_PRIVATE_KEY)
    if bad:
        return CredentialsCheck("malformed_field", tuple(bad), loose), None
    return CredentialsCheck(None, (), loose), data


def inspect_credentials(path) -> CredentialsCheck:
    """The shared key-file check, as names and codes only (for doctor and the gates)."""
    return _check(path)[0]


def load_credentials(path):
    """``(client_email, private_key_pem)`` or a code-only ProviderError. Never cached."""
    if not isinstance(path, str) or not path:
        raise ProviderError("credentials_missing")
    check, data = _check(path)
    if check.problem is not None:
        raise ProviderError(check.code)
    return data[g.CRED_CLIENT_EMAIL], data[g.CRED_PRIVATE_KEY]


class ServiceAccountAuth:
    """Access tokens for the spreadsheets scope. Each token request is charged to ``budget``
    BEFORE it is sent. The token is cached in memory until shortly before it expires."""

    def __init__(self, credentials_path, *, http, budget, signer=None, clock=time.time):
        if not isinstance(budget, CallBudget):
            raise TypeError("budget must be a CallBudget")
        self._path = credentials_path
        self._http = http
        self._budget = budget
        self._signer = signer if signer is not None else OpenSSLSigner()
        self._clock = clock
        self._token = None
        self._expires_at = 0.0

    def __repr__(self):
        return "ServiceAccountAuth()"

    def build_assertion(self, now=None) -> str:
        """The signed JWT (header.claims.signature, base64url without padding)."""
        email, key = load_credentials(self._path)
        now = int(self._clock() if now is None else now)
        claims = {g.CLAIM_ISSUER: email, g.CLAIM_SCOPE: g.SCOPE, g.CLAIM_AUDIENCE: g.TOKEN_URL,
                  g.CLAIM_ISSUED_AT: now, g.CLAIM_EXPIRES_AT: now + g.JWT_LIFETIME_SECONDS}
        signing_input = ".".join(
            b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("utf-8"))
            for part in (g.JWT_HEADER, claims))
        signature = self._signer.sign(signing_input.encode("ascii"), key)
        if not isinstance(signature, bytes) or not signature:
            raise ProviderError("signing_failed")
        return signing_input + "." + b64url(signature)

    def token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        assertion = self.build_assertion(now)
        try:
            self._budget.consume(1)
        except BudgetExceeded:
            raise ProviderError("budget_exhausted") from None
        status, data = resp = self._http.post_form(
            g.TOKEN_URL, {g.FORM_GRANT_TYPE: g.GRANT_TYPE_JWT_BEARER, g.FORM_ASSERTION: assertion},
            {"Accept": "application/json"}, REQUEST_TIMEOUT_SECONDS)
        if not 200 <= status < 300:
            raise ProviderError(status_code(status, resp.headers, not_found="unexpected_status",
                                            bad_request="token_rejected"))
        token = data.get(g.RESP_ACCESS_TOKEN) if isinstance(data, dict) else None
        expires_in = data.get(g.RESP_EXPIRES_IN, g.JWT_LIFETIME_SECONDS) \
            if isinstance(data, dict) else None
        if (not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_LENGTH
                or not all(0x20 < ord(c) < 0x7f for c in token)
                or isinstance(expires_in, bool) or not isinstance(expires_in, int)
                or not 0 < expires_in <= 86400):
            raise ProviderError("unexpected_shape")
        self._token = token
        self._expires_at = now + expires_in
        return token
