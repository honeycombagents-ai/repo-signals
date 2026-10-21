"""A deterministic provider with synthetic people. The default for dry runs and tests.

It never touches the network. Every email it returns is on ``example.invalid``,
a reserved domain that can never receive mail.
"""
import hashlib

from ..models import Person

FAKE_EMAIL_DOMAIN = "example.invalid"
_FIRST = ("Avery", "Blake", "Casey", "Devon", "Emery", "Finley", "Harper", "Jordan")
_LAST = ("Alder", "Birch", "Cedar", "Hazel", "Linden", "Maple", "Rowan", "Willow")
_MAX_PER_COMPANY = 3


class FakeProvider:
    name = "fake"
    required_env: tuple[str, ...] = ()

    def find_people(self, company_domain: str, titles: tuple[str, ...],
                    limit: int) -> list[Person]:
        if not isinstance(company_domain, str) or not company_domain.strip():
            raise ValueError("company_domain must be a non-empty string")
        if not isinstance(titles, tuple) or not all(isinstance(t, str) and t for t in titles):
            raise ValueError("titles must be a tuple of non-empty strings")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive whole number")
        people = []
        for i in range(min(limit, _MAX_PER_COMPANY)):
            title = titles[i % len(titles)] if titles else "Engineer"
            digest = hashlib.sha256(f"{company_domain}|{i}|{title}".encode()).digest()
            first, last = _FIRST[digest[0] % len(_FIRST)], _LAST[digest[1] % len(_LAST)]
            people.append(Person(
                name=f"{first} {last}",
                email=f"{first}.{last}".lower() + "@" + FAKE_EMAIL_DOMAIN,
                title=title,
                company_domain=company_domain,
                source=self.name,
                confidence=0.5 + (digest[2] % 50) / 100,
            ))
        return people
