"""Data records shared by every stage."""
from dataclasses import dataclass


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
