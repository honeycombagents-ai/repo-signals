"""Cell-level text safety shared by the sheet writer and the drafter. No I/O, no network."""
import re

FORMULA_PREFIXES = ("=", "+", "-", "@")
CONTROL_PREFIXES = ("\t", "\r", "\n")
MAX_CELL_CHARS = 50000
_PLAIN_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def guard_cell(value: str) -> str:
    """Quote a value that a spreadsheet could read as a formula. Plain numbers pass."""
    if not isinstance(value, str):
        raise TypeError("cell values are strings")
    value = value[:MAX_CELL_CHARS]
    if value.startswith(CONTROL_PREFIXES) or (
            value.lstrip().startswith(FORMULA_PREFIXES)
            and not _PLAIN_NUMBER.fullmatch(value.strip())):
        return "'" + value
    return value


def mask_email(email: str) -> str:
    """``j***@example.invalid`` for display. Empty stays empty."""
    if not email:
        return ""
    local, at, domain = email.partition("@")
    if not at or not local or not domain:
        return "***"
    return local[0] + "***@" + domain
