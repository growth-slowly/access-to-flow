"""Read-only, fully offline Microsoft Access database analysis.

Nothing in this package invokes Access, COM, mdbtools, subprocesses, or any
network or AI service; databases are parsed directly from their bytes.
"""

from .jet_catalog import JetCatalogError, read_catalog
from .translation import translate_access_file

__all__ = ["JetCatalogError", "read_catalog", "translate_access_file"]
