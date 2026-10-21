"""Data records shared by every stage."""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Person:
    """A person found for a company. ``email`` is None when the provider found none."""

    name: str
    email: str | None
    title: str
    company_domain: str
    source: str
    confidence: float

    def __post_init__(self):
        for field in ("name", "title", "company_domain", "source"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Person.{field} must be a non-empty string")
        if self.email is not None:
            if (not isinstance(self.email, str) or self.email.count("@") != 1
                    or any(c.isspace() for c in self.email)):
                raise ValueError("Person.email must be None or a single address")
        c = self.confidence
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0.0 <= c <= 1.0:
            raise ValueError("Person.confidence must be a number from 0 to 1")
        object.__setattr__(self, "confidence", float(c))


EVIDENCE_SUMMARY_MAX = 300
EVIDENCE_URL_MAX = 2048
_KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                        r"[a-z][a-z0-9-]{0,61}[a-z0-9]", re.IGNORECASE)


@dataclass(frozen=True)
class Evidence:
    """One public repository signal about a company.

    ``summary`` is a short, factual, single-line sentence (for example "the org publishes a
    repo named X that uses library Y"); drafts quote it verbatim. ``url`` is an https link to
    the public evidence. ``kind`` is a short lowercase label such as ``repo`` or ``release``.
    """

    company_domain: str
    summary: str
    url: str
    kind: str

    def __post_init__(self):
        if not isinstance(self.company_domain, str) or not _DOMAIN_RE.fullmatch(self.company_domain):
            raise ValueError("Evidence.company_domain must be a domain name")
        s = self.summary
        if (not isinstance(s, str) or not s.strip() or s != s.strip()
                or len(s) > EVIDENCE_SUMMARY_MAX or not s.isprintable()):
            raise ValueError("Evidence.summary must be one printable line of at most "
                             f"{EVIDENCE_SUMMARY_MAX} characters")
        u = self.url
        if (not isinstance(u, str) or not u.startswith("https://") or len(u) > EVIDENCE_URL_MAX
                or any(c.isspace() or not c.isprintable() for c in u)
                or not urlsplit(u).hostname):
            raise ValueError("Evidence.url must be an https link")
        if not isinstance(self.kind, str) or not _KIND_RE.fullmatch(self.kind):
            raise ValueError("Evidence.kind must be a short lowercase label")
