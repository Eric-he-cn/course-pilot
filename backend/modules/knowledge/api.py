from __future__ import annotations


class KnowledgeFeatureDisabledError(ValueError):
    """Returned when a Wiki build is requested before its course flag is enabled."""


class MaterialNotIndexedError(ValueError):
    """A Wiki build requires already searchable material; it must not imply indexing."""
