"""A minimal Google Sheets client: read the header row, read rows, append rows, update cells.

There is NO delete, clear, or structural (batch) call here, by design, and the HTTP client has
no DELETE or PATCH. ``update_cells`` refuses any range outside the tool's columns and any
range that would touch email_draft or reviewed below the header row.

Every request (including each token request made by the auth) is charged to the CallBudget
BEFORE it is sent. A request body is measured before anything is charged: a body over the HTTP
client's MAX_REQUEST_BYTES raises ``payload_too_large`` with no call charged and nothing sent.
Failures are code-only ProviderErrors: never a URL, token, header, body, sheet id or
credentials path.
"""
import math
import re
from urllib.parse import quote

from ..budget import BudgetExceeded, CallBudget
from ..http import MAX_REQUEST_BYTES, HttpClient, json_body_size
from ..providers.base import ProviderError
from . import google_api as g
from .errors import status_code
from .layout import COLUMNS, LAST_COLUMN, LETTERS, PROTECTED_COLUMNS

DEFAULT_TAB = "Sheet1"
REQUEST_TIMEOUT_SECONDS = 15.0
_SHEET_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,128}")
_TAB_RE = re.compile(r"[A-Za-z0-9 _-]{1,100}")
_CELLS_RE = re.compile(r"([A-Z])([1-9][0-9]{0,6}):([A-Z])([1-9][0-9]{0,6})")


def build_http(transport=None) -> HttpClient:
    """The one HttpClient for Sheets work: https only, to the two Google hosts and no other."""
    return HttpClient(g.ALLOWED_HOSTS, transport=transport)


def _cell_ok(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, (str, int))


def _rows_ok(rows, width):
    return isinstance(rows, list) and rows != [] and all(
        isinstance(r, list) and len(r) == width and all(_cell_ok(c) for c in r) for r in rows)


class SheetsClient:
    def __init__(self, sheet_id, *, auth, budget, http=None, tab=DEFAULT_TAB):
        if not isinstance(sheet_id, str) or not _SHEET_ID_RE.fullmatch(sheet_id):
            raise ValueError("sheet_id is not a well-formed Google Sheet id")
        if not isinstance(tab, str) or not _TAB_RE.fullmatch(tab):
            raise ValueError("tab must be letters, digits, spaces, '-' or '_'")
        if not isinstance(budget, CallBudget):
            raise TypeError("budget must be a CallBudget")
        self._sheet_id = sheet_id
        self._tab = tab
        self._auth = auth
        self._budget = budget
        self._http = http if http is not None else build_http()

    def __repr__(self):
        return "SheetsClient()"

    def _url(self, cells, suffix=""):
        rng = f"'{self._tab}'!{cells}"
        return g.VALUES_URL.format(sheet_id=quote(self._sheet_id, safe=""),
                                   range=quote(rng, safe="")) + suffix

    def _call(self, method, url, payload=None, params=None):
        if payload is not None and json_body_size(payload) > MAX_REQUEST_BYTES:
            raise ProviderError("payload_too_large")  # measured first: nothing charged or sent
        token = self._auth.token()
        try:
            self._budget.consume(1)
        except BudgetExceeded:
            raise ProviderError("budget_exhausted") from None
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if method == "GET":
            resp = self._http.get_json(url, params, headers, REQUEST_TIMEOUT_SECONDS)
        elif method == "POST":
            resp = self._http.post_json(url, payload, headers, REQUEST_TIMEOUT_SECONDS, params)
        elif method == "PUT":
            resp = self._http.put_json(url, payload, headers, REQUEST_TIMEOUT_SECONDS, params)
        else:
            raise ProviderError("method_not_allowed")
        status, data = resp
        if not 200 <= status < 300:
            raise ProviderError(status_code(status, resp.headers))
        if not isinstance(data, dict):
            raise ProviderError("unexpected_shape")
        return data

    def _read(self, cells):
        data = self._call("GET", self._url(cells))
        values = data.get(g.RESP_VALUES, [])
        if not isinstance(values, list) or not all(
                isinstance(r, list) and all(isinstance(c, (str, int, float)) for c in r)
                for r in values):
            raise ProviderError("unexpected_shape")
        return [[str(c) for c in r] for r in values]

    def read_header(self) -> list[str]:
        """The first row, trailing empty cells dropped; [] for an empty row."""
        rows = self._read("1:1")
        row = list(rows[0]) if rows else []
        while row and row[-1].strip() == "":
            row.pop()
        return row

    def read_rows(self) -> list[list[str]]:
        """Every row below the header, in the tool's columns."""
        return self._read(f"A2:{LAST_COLUMN}")

    def append_rows(self, rows) -> None:
        """Append rows after the last row. New rows are inserted: nothing is overwritten."""
        if not _rows_ok(rows, len(COLUMNS)):
            raise ProviderError("bad_rows")
        self._call("POST", self._url(f"A1:{LAST_COLUMN}", g.APPEND_SUFFIX),
                   {g.BODY_VALUES: rows},
                   {g.PARAM_VALUE_INPUT_OPTION: g.VALUE_INPUT_RAW,
                    g.PARAM_INSERT_DATA_OPTION: g.INSERT_ROWS})

    def update_cells(self, cells, rows) -> None:
        """Write ``rows`` into the A1 range ``cells`` (for example ``"A1:K1"``).

        Refused: ranges outside the tool's columns, and any range that touches a protected
        column (email_draft, reviewed) below the header row.
        """
        m = _CELLS_RE.fullmatch(cells) if isinstance(cells, str) else None
        if not m:
            raise ProviderError("bad_range")
        c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if c1 not in LETTERS or c2 not in LETTERS or c1 > c2 or r1 > r2:
            raise ProviderError("bad_range")
        protected = {LETTERS[COLUMNS.index(c)] for c in PROTECTED_COLUMNS}
        if r2 >= 2 and any(c1 <= p <= c2 for p in protected):
            raise ProviderError("protected_cells")
        width = ord(c2) - ord(c1) + 1
        if not _rows_ok(rows, width) or len(rows) != r2 - r1 + 1:
            raise ProviderError("bad_rows")
        rng = f"'{self._tab}'!{cells}"
        self._call("PUT", self._url(cells),
                   {g.BODY_RANGE: rng, g.BODY_MAJOR_DIMENSION: g.ROWS_DIMENSION,
                    g.BODY_VALUES: rows},
                   {g.PARAM_VALUE_INPUT_OPTION: g.VALUE_INPUT_RAW})
