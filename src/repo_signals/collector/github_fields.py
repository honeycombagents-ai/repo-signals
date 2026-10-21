"""Parse one GitHub repository record into a RepoFact, reading ONLY an allowlist of keys.

Everything else in the record (description, README text, topics, homepage, commit authors,
contributors, the owner's name or email, and any other field) is ignored: it is never read, so
it can never reach Evidence. A malformed record gives None. This function never raises.
"""
import re
from dataclasses import dataclass
from datetime import date

# UNVERIFIED general knowledge about GitHub's repository records and web host:
# verify against GitHub's current documentation before first live use.
WEB_HOST = "github.com"
REPO_KEYS = ("name", "full_name", "html_url", "language", "pushed_at", "fork", "archived",
             "private", "owner")
OWNER_KEYS = ("login", "type")
ORGANIZATION = "Organization"

_PUSHED_AT_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_MAX = {"login": 39, "type": 32, "name": 100, "full_name": 140, "html_url": 300, "language": 64}


@dataclass(frozen=True)
class RepoFact:
    owner_login: str
    owner_type: str
    name: str
    full_name: str
    html_url: str
    language: str | None
    pushed_at: date
    fork: bool
    archived: bool
    private: bool


def _text(value, key):
    if not isinstance(value, str) or not value or len(value) > _MAX[key]:
        raise ValueError(key)
    return value


def _flag(value):
    if not isinstance(value, bool):
        raise ValueError("flag")
    return value


def parse_repo(raw) -> RepoFact | None:
    """A RepoFact from the allowlisted keys of ``raw``, or None if any is missing or malformed."""
    try:
        if not isinstance(raw, dict):
            return None
        owner = raw.get("owner")
        if not isinstance(owner, dict):
            return None
        language = raw.get("language")
        if language is not None:
            language = _text(language, "language")
        pushed = raw.get("pushed_at")
        m = _PUSHED_AT_RE.fullmatch(pushed) if isinstance(pushed, str) else None
        if not m:
            return None
        return RepoFact(
            owner_login=_text(owner.get("login"), "login"),
            owner_type=_text(owner.get("type"), "type"),
            name=_text(raw.get("name"), "name"),
            full_name=_text(raw.get("full_name"), "full_name"),
            html_url=_text(raw.get("html_url"), "html_url"),
            language=language,
            pushed_at=date(int(m.group(1)), int(m.group(2)), int(m.group(3))),
            fork=_flag(raw.get("fork")),
            archived=_flag(raw.get("archived")),
            private=_flag(raw.get("private")),
        )
    except Exception:  # never raise for a malformed record, whatever its shape
        return None
