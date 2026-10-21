"""Map Google HTTP statuses to code-only ProviderError codes."""
import re

RETRY_AFTER_MAX_SECONDS = 60
_RETRY_AFTER_RE = re.compile(r"[0-9]{1,5}")


def status_code(status, headers, *, not_found="sheet_not_found", bad_request="bad_request"):
    """The code for a non-2xx status. Never sleeps: a Retry-After becomes ``retry_after_N``."""
    if status in (401, 403):
        return "auth_failed"
    if status == 404:
        return not_found
    if status == 429:
        raw = (headers or {}).get("retry-after", "")
        raw = raw.strip() if isinstance(raw, str) else ""
        if _RETRY_AFTER_RE.fullmatch(raw) and int(raw) <= RETRY_AFTER_MAX_SECONDS:
            return f"retry_after_{int(raw)}"
        return "rate_limited"
    if status in (400, 422):
        return bad_request
    if 500 <= status < 600:
        return "server_error"
    return "unexpected_status"
