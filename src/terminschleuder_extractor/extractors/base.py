"""Extractor interface + registry.

An ``Extractor`` turns a ``DueSource`` into a list of ``ObservationSubmit``
without any page-specific knowledge. The registry lets platform-specific
extractors drop in later without touching the runner; today only the
LLM-based extractor ships (per the user: "I dont want to build page specific
extractors").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Type

from ..models import DueSource, ObservationSubmit


class Extractor(ABC):
    """Turn a due source into a list of pending event observations."""

    @abstractmethod
    def extract(self, source: DueSource) -> list[ObservationSubmit]:
        """Return observations for ``source`` (possibly empty). Raise on fatal errors."""


# --- registry ---

_REGISTRY: dict[str, Type[Extractor]] = {}
DEFAULT_NAME = "llm"


def register(name: str) -> Callable[[Type[Extractor]], Type[Extractor]]:
    """Class decorator: register an Extractor under ``name``."""

    def decorator(cls: Type[Extractor]) -> Type[Extractor]:
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_extractor_class(platform: str | None) -> Type[Extractor]:
    """Resolve the extractor class for a platform, falling back to the default."""
    if platform and platform in _REGISTRY:
        return _REGISTRY[platform]
    return _REGISTRY[DEFAULT_NAME]


def get_extractor(platform: str | None, **kwargs: object) -> Extractor:
    """Instantiate the resolved extractor class with ``kwargs``."""
    return get_extractor_class(platform)(**kwargs)  # type: ignore[arg-type]


def available() -> list[str]:
    return sorted(_REGISTRY)


# Import the LLM extractor so its @register("llm") side effect runs on import
# of the extractors package. Kept here to avoid a circular import (llm.py
# imports from base).
from . import llm as _llm  # noqa: E402,F401  (registration side effect)