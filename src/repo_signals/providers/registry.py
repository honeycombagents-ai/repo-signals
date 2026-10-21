"""Provider registry: register, get, list.

Built in: ``fake`` and ``hunter`` (registered as a dry run, with no budget: live use
needs a ``HunterProvider(budget, dry_run=False)``). Planned, not implemented yet: ``apollo``.
"""
import re

from .base import PeopleProvider
from .fake import FakeProvider
from .hunter import HunterProvider

PLANNED = ("apollo",)
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class RegistryError(Exception):
    pass


class DuplicateProviderError(RegistryError):
    pass


class UnknownProviderError(RegistryError, LookupError):
    pass


class Registry:
    def __init__(self):
        self._providers: dict[str, PeopleProvider] = {}

    def register(self, provider: PeopleProvider) -> None:
        name = getattr(provider, "name", None)
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise RegistryError("provider name must be lowercase letters, digits, '-' or '_'")
        env = getattr(provider, "required_env", None)
        if not isinstance(env, tuple) or not all(
                isinstance(e, str) and ENV_NAME_RE.fullmatch(e) for e in env):
            raise RegistryError("required_env must be a tuple of environment variable names")
        if not callable(getattr(provider, "find_people", None)):
            raise RegistryError("provider must define find_people")
        if name in self._providers:
            raise DuplicateProviderError("a provider with this name is already registered")
        self._providers[name] = provider

    def get(self, name: str) -> PeopleProvider:
        if name in self._providers:
            return self._providers[name]
        if name in PLANNED:
            raise UnknownProviderError(
                "this provider is planned but not implemented yet "
                f"(registered: {', '.join(self.names())})")
        raise UnknownProviderError(
            f"unknown provider (registered: {', '.join(self.names())}; "
            f"planned, not implemented yet: {', '.join(PLANNED)})")

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def __contains__(self, name) -> bool:
        return name in self._providers


_DEFAULT = Registry()
_DEFAULT.register(FakeProvider())
_DEFAULT.register(HunterProvider())


def default_registry() -> Registry:
    return _DEFAULT


def register(provider: PeopleProvider) -> None:
    _DEFAULT.register(provider)


def get(name: str) -> PeopleProvider:
    return _DEFAULT.get(name)


def list_providers() -> tuple[str, ...]:
    return _DEFAULT.names()
