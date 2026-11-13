"""Ingest: from an extracted game folder on disk to a :class:`bea.model.GameInput`."""

from __future__ import annotations

from bea.ingest.extracted import MODULE_ORDER, PACKAGE_EXTENSIONS, open_input

__all__ = ["MODULE_ORDER", "PACKAGE_EXTENSIONS", "open_input"]
