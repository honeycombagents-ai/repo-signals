"""People providers: the adapter interface, the registry and the built-in fake."""
from .base import PeopleProvider, ProviderError, find_people_within
from .registry import (PLANNED, DuplicateProviderError, Registry, RegistryError,
                       UnknownProviderError, default_registry, get, list_providers, register)

__all__ = [
    "PLANNED", "DuplicateProviderError", "PeopleProvider", "ProviderError", "Registry",
    "RegistryError", "UnknownProviderError", "default_registry", "find_people_within", "get",
    "list_providers", "register",
]
