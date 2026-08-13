"""Extractor package: interface, registry, and built-in LLM extractor.

Importing this package registers the ``llm`` extractor (via
``base.py``'s bottom import of ``llm``). The runner resolves an extractor by
platform with ``get_extractor``.
"""

from .base import Extractor, available, get_extractor, get_extractor_class, register
from .llm import LLMExtractor

__all__ = [
    "Extractor",
    "LLMExtractor",
    "available",
    "get_extractor",
    "get_extractor_class",
    "register",
]